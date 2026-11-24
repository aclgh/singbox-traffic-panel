"""sing-box native experimental.clash_api WebSocket /connections incremental collector."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

import websockets

from ..config import AppConfig, ProtocolConfig
from ..db import DeviceDelta, ProtocolDelta
from .base import BaseCollector

logger = logging.getLogger(__name__)


@dataclass
class _ConnTrack:
    upload: int
    download: int
    device_name: str
    protocol_key: str | None
    source_tag: str


class ClashApiCollector(BaseCollector):
    """Streams sing-box experimental.clash_api /connections and aggregates incremental deltas."""

    source_name = "clash_api"

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.ws_url = config.collector.clash_api.ws_url
        self.secret = config.collector.clash_api.secret
        self.reconnect_interval = config.collector.clash_api.reconnect_interval_seconds

        self._lock = threading.Lock()
        self._active_conns: dict[str, _ConnTrack] = {}
        self._pending_devices: dict[tuple[str, str], list[int]] = {}
        self._pending_protocols: dict[str, tuple[str, float, list[int]]] = {}

        self._ws_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._ws_connected: bool = False
        self._ws_last_error: str | None = None
        self._messages_received: int = 0
        self._live_connections: int = 0
        self._missing_user_samples: int = 0

    def _ws_headers(self) -> dict[str, str]:
        if self.secret:
            return {"Authorization": f"Bearer {self.secret}"}
        return {}

    async def probe(self) -> bool:
        try:
            async with websockets.connect(
                self.ws_url,
                additional_headers=self._ws_headers(),
                open_timeout=3.0,
            ) as ws:
                await asyncio.wait_for(ws.recv(), timeout=3.0)
                return True
        except Exception:
            return False

    async def start(self) -> None:
        self._stop_event.clear()
        self._ws_task = asyncio.create_task(self._ws_loop(), name="clash-api-ws-reader")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        self._ws_connected = False

    async def _ws_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(
                    self.ws_url,
                    additional_headers=self._ws_headers(),
                    open_timeout=5.0,
                    ping_interval=20.0,
                    ping_timeout=20.0,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    self._ws_connected = True
                    self._ws_last_error = None
                    logger.info("Connected to sing-box clash_api WebSocket at %s", self.ws_url)
                    async for raw_msg in ws:
                        if self._stop_event.is_set():
                            break
                        try:
                            payload = json.loads(raw_msg)
                            if isinstance(payload, dict):
                                self.process_snapshot(payload)
                        except json.JSONDecodeError:
                            continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ws_connected = False
                self._ws_last_error = str(exc)
                logger.debug("clash_api WebSocket error (%s), retrying in %.1fs", exc, self.reconnect_interval)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.reconnect_interval)
                except asyncio.TimeoutError:
                    pass

    @staticmethod
    def _extract_user(conn: dict[str, Any]) -> str | None:
        meta = conn.get("metadata") or {}
        for candidate in (meta.get("user"), meta.get("inboundUser"), conn.get("user")):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    def _resolve_protocol(self, conn: dict[str, Any]) -> tuple[ProtocolConfig | None, str]:
        meta = conn.get("metadata") or {}
        raw_type = str(meta.get("type") or "")
        inbound_tag = str(meta.get("inbound") or "")
        proto_type_hint = ""
        if "/" in raw_type:
            proto_type_hint, _, parsed_tag = raw_type.partition("/")
            if not inbound_tag:
                inbound_tag = parsed_tag
        else:
            proto_type_hint = raw_type.lower()

        chains = [str(c) for c in (conn.get("chains") or [])]
        outbound_meta = str(meta.get("outbound") or "")
        if outbound_meta and outbound_meta not in chains:
            chains.append(outbound_meta)

        # 1. Exact preferred dimension match
        for proto in self.config.protocols:
            if proto.stats_dimension == "outbound":
                for c in chains:
                    if c in proto.outbound_tags:
                        return proto, c
            elif inbound_tag and inbound_tag == proto.inbound_tag:
                return proto, inbound_tag

        # 2. Secondary exact tag match across inbound/outbound
        for proto in self.config.protocols:
            if inbound_tag and inbound_tag == proto.inbound_tag:
                return proto, inbound_tag
            for c in chains:
                if c in proto.outbound_tags:
                    return proto, c

        # 3. Fallback by protocol type prefix (vless / hysteria2)
        low_hint = proto_type_hint.lower()
        for proto in self.config.protocols:
            if low_hint and low_hint == proto.protocol_type.lower():
                return proto, inbound_tag or low_hint

        return None, inbound_tag or (chains[0] if chains else "other")

    def process_snapshot(self, payload: dict[str, Any]) -> None:
        """Process a single /connections JSON payload and buffer incremental byte deltas."""
        connections = payload.get("connections") or []
        seen_ids: set[str] = set()

        with self._lock:
            self._messages_received += 1
            self._live_connections = len(connections)

            for conn in connections:
                if not isinstance(conn, dict):
                    continue
                cid = str(conn.get("id") or "")
                if not cid:
                    continue
                seen_ids.add(cid)

                cur_up = max(0, int(conn.get("upload") or 0))
                cur_down = max(0, int(conn.get("download") or 0))

                prev = self._active_conns.get(cid)
                if prev is None:
                    user = self._extract_user(conn)
                    if user is None:
                        self._missing_user_samples += 1
                        device_name = "unknown"
                    else:
                        device_name = user
                    proto_cfg, source_tag = self._resolve_protocol(conn)
                    proto_key = proto_cfg.key if proto_cfg else None
                    d_up = cur_up
                    d_down = cur_down
                    self._active_conns[cid] = _ConnTrack(
                        upload=cur_up,
                        download=cur_down,
                        device_name=device_name,
                        protocol_key=proto_key,
                        source_tag=source_tag,
                    )
                else:
                    d_up = max(0, cur_up - prev.upload)
                    d_down = max(0, cur_down - prev.download)
                    prev.upload = cur_up
                    prev.download = cur_down
                    device_name = prev.device_name
                    proto_key = prev.protocol_key
                    source_tag = prev.source_tag
                    proto_cfg = self.config.protocol_by_key(proto_key) if proto_key else None

                if d_up > 0 or d_down > 0:
                    dev_bucket = self._pending_devices.setdefault(
                        (device_name, proto_key or "all"), [0, 0]
                    )
                    dev_bucket[0] += d_up
                    dev_bucket[1] += d_down

                    if proto_cfg is not None:
                        existing = self._pending_protocols.get(proto_cfg.key)
                        if existing is None:
                            self._pending_protocols[proto_cfg.key] = (
                                source_tag,
                                proto_cfg.oci_multiplier,
                                [d_up, d_down],
                            )
                        else:
                            existing[2][0] += d_up
                            existing[2][1] += d_down

            # Evict closed connections so memory stays bounded
            closed_ids = [cid for cid in self._active_conns if cid not in seen_ids]
            for cid in closed_ids:
                del self._active_conns[cid]

    async def collect_deltas(self) -> tuple[list[DeviceDelta], list[ProtocolDelta]]:
        if not self._ws_connected and self._ws_last_error:
            raise ConnectionError(f"clash_api WebSocket disconnected: {self._ws_last_error}")

        with self._lock:
            device_deltas = [
                DeviceDelta(
                    device_name=dev,
                    protocol=proto,
                    upload_bytes=vals[0],
                    download_bytes=vals[1],
                )
                for (dev, proto), vals in self._pending_devices.items()
                if vals[0] > 0 or vals[1] > 0
            ]
            protocol_deltas = [
                ProtocolDelta(
                    protocol_key=pkey,
                    source_tag=src_tag,
                    upload_bytes=vals[0],
                    download_bytes=vals[1],
                    multiplier=mult,
                )
                for pkey, (src_tag, mult, vals) in self._pending_protocols.items()
                if vals[0] > 0 or vals[1] > 0
            ]
            self._pending_devices.clear()
            self._pending_protocols.clear()

        return device_deltas, protocol_deltas

    def diagnostics(self) -> dict[str, Any]:
        return {
            "source": self.source_name,
            "ws_url": self.ws_url,
            "ws_connected": self._ws_connected,
            "ws_last_error": self._ws_last_error,
            "messages_received": self._messages_received,
            "live_connections": self._live_connections,
            "tracked_connections": len(self._active_conns),
            "missing_user_samples": self._missing_user_samples,
            "hint": (
                "检测到部分连接缺少 metadata.user，建议升级 sing-box 或切换到 v2ray_api 精确统计"
                if self._missing_user_samples > 0
                else ""
            ),
        }
