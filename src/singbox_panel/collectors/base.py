"""Base collector contract and periodic persistence manager."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from ..config import AppConfig
from ..db import DeviceDelta, ProtocolDelta, TrafficDatabase

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    source_name: str = "base"

    @abstractmethod
    async def start(self) -> None:
        """Start any background readers (e.g., WebSocket stream)."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop background readers cleanly."""

    @abstractmethod
    async def probe(self) -> bool:
        """Return True if the upstream sing-box API is currently reachable."""

    @abstractmethod
    async def collect_deltas(self) -> tuple[list[DeviceDelta], list[ProtocolDelta]]:
        """Return incremental (device_deltas, protocol_deltas) since the previous call."""

    def diagnostics(self) -> dict[str, Any]:
        return {"source": self.source_name}


class CollectorManager:
    """Manages collector lifecycle, auto-source selection, and 10-30s SQLite flushing."""

    def __init__(
        self,
        config: AppConfig,
        db: TrafficDatabase,
        collector: BaseCollector | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self._explicit_collector = collector
        self.active_collector: BaseCollector | None = collector
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._flush_lock = asyncio.Lock()

        self.active_source: str = collector.source_name if collector else config.collector.source
        self.connected: bool = False
        self.last_flush_at: str | None = None
        self.last_error: str | None = None
        self.total_flushes: int = 0
        self.last_rows_written: int = 0

    async def _resolve_collector(self) -> BaseCollector:
        if self._explicit_collector is not None:
            return self._explicit_collector

        from .clash_api import ClashApiCollector
        from .mock import MockTrafficCollector
        from .v2ray_api import V2RayApiCollector

        mode = self.config.collector.source.lower().strip()
        if mode == "mock":
            return MockTrafficCollector(self.config, self.db)
        if mode == "v2ray_api":
            return V2RayApiCollector(self.config)
        if mode == "clash_api":
            return ClashApiCollector(self.config)
        if mode == "auto":
            v2 = V2RayApiCollector(self.config)
            if await v2.probe():
                logger.info("Auto-detected sing-box v2ray_api at %s", self.config.collector.v2ray_api.address)
                return v2
            logger.info(
                "v2ray_api at %s unreachable during startup probe; falling back to clash_api at %s",
                self.config.collector.v2ray_api.address,
                self.config.collector.clash_api.ws_url,
            )
            return ClashApiCollector(self.config)
        raise ValueError(
            f"Unsupported collector.source={mode!r}. Expected 'v2ray_api', 'clash_api', 'auto', or 'mock'."
        )

    async def start(self) -> None:
        self._stop_event.clear()
        self.active_collector = await self._resolve_collector()
        self.active_source = self.active_collector.source_name
        await self.active_collector.start()
        self._task = asyncio.create_task(self._run_loop(), name="traffic-collector-loop")
        self._save_state()

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Final flush so restart never drops in-flight deltas
        if self.active_collector is not None:
            try:
                await self.flush_now()
            except Exception as exc:
                logger.warning("Final traffic flush failed on shutdown: %s", exc)
            await self.active_collector.stop()

    async def flush_now(self) -> int:
        """Immediately collect and persist pending traffic deltas into SQLite."""
        if self.active_collector is None:
            return 0
        async with self._flush_lock:
            try:
                dev_deltas, proto_deltas = await self.active_collector.collect_deltas()
                rows = self.db.record_deltas(dev_deltas, proto_deltas)
                self.connected = True
                self.last_error = None
                self.last_flush_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.total_flushes += 1
                self.last_rows_written = rows
                self._save_state()
                return rows
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                self._save_state()
                logger.warning("Traffic collector (%s) flush error: %s", self.active_source, exc)
                return 0

    async def _run_loop(self) -> None:
        interval = self.config.collector.flush_interval_seconds
        # Immediate first collect shortly after startup
        await self.flush_now()
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                await self.flush_now()

    def _save_state(self) -> None:
        try:
            self.db.set_state(
                "collector",
                {
                    "configured_source": self.config.collector.source,
                    "active_source": self.active_source,
                    "connected": self.connected,
                    "last_flush_at": self.last_flush_at,
                    "last_error": self.last_error,
                    "total_flushes": self.total_flushes,
                    "flush_interval_seconds": self.config.collector.flush_interval_seconds,
                },
            )
        except Exception:
            pass

    def status(self) -> dict[str, Any]:
        diag = self.active_collector.diagnostics() if self.active_collector else {}
        return {
            "configured_source": self.config.collector.source,
            "active_source": self.active_source,
            "connected": self.connected,
            "flush_interval_seconds": self.config.collector.flush_interval_seconds,
            "last_flush_at": self.last_flush_at,
            "last_error": self.last_error,
            "total_flushes": self.total_flushes,
            "last_rows_written": self.last_rows_written,
            "diagnostics": diag,
        }
