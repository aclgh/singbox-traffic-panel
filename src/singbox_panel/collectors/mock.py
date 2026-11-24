"""Built-in realistic traffic simulator for local preview and testing (--demo)."""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import AppConfig
from ..db import DeviceDelta, ProtocolDelta, TrafficDatabase
from .base import BaseCollector


class MockTrafficCollector(BaseCollector):
    """Seeds realistic Singapore VPS history (once) and emits live incremental deltas."""

    source_name = "mock"

    def __init__(self, config: AppConfig, db: TrafficDatabase) -> None:
        self.config = config
        self.db = db
        self._rng = random.Random(42)

    async def start(self) -> None:
        if not self.db.has_any_traffic():
            self._seed_demo_history()

    async def stop(self) -> None:
        pass

    async def probe(self) -> bool:
        return True

    def _seed_demo_history(self) -> None:
        """Seed 14 days of realistic hourly traffic for owrt-router / mac / mobile."""
        tz = ZoneInfo(self.config.server.timezone)
        now = datetime.now(tz).replace(minute=0, second=0, microsecond=0)

        proto_vless = self.config.protocol_by_key("tokyo_vless") or (
            self.config.protocols[0] if self.config.protocols else None
        )
        proto_hy2 = self.config.protocol_by_key("sg_hysteria2") or (
            self.config.protocols[1] if len(self.config.protocols) > 1 else None
        )

        # Realistic weights: owrt-router ~58%, mac ~28%, mobile ~14%
        device_profiles = [
            ("owrt-router", 0.58, 0.14),  # (name, share, upload_ratio)
            ("mac", 0.28, 0.22),
            ("mobile", 0.14, 0.18),
        ]

        days_back = 14
        for day_offset in range(days_back, -1, -1):
            day_dt = now - timedelta(days=day_offset)
            max_hour = now.hour if day_offset == 0 else 23
            for hour in range(0, max_hour + 1):
                slot = day_dt.replace(hour=hour)
                # Diurnal curve: quiet 02:00-07:00, busy evening 19:00-23:00
                if 2 <= hour <= 7:
                    base_mb = self._rng.uniform(450, 1400)
                elif 19 <= hour <= 23:
                    base_mb = self._rng.uniform(4200, 9800)
                else:
                    base_mb = self._rng.uniform(1600, 4800)

                total_bytes = int(base_mb * 1000 * 1000)
                # ~42% Tokyo VLESS (2x), ~58% SG Hysteria2 (1x)
                vless_ratio = self._rng.uniform(0.36, 0.48)
                vless_bytes = int(total_bytes * vless_ratio)
                hy2_bytes = total_bytes - vless_bytes

                dev_deltas: list[DeviceDelta] = []
                for dev_name, share, up_ratio in device_profiles:
                    dev_total = int(total_bytes * share * self._rng.uniform(0.82, 1.18))
                    dev_up = int(dev_total * up_ratio)
                    dev_down = max(0, dev_total - dev_up)
                    dev_deltas.append(
                        DeviceDelta(
                            device_name=dev_name,
                            upload_bytes=dev_up,
                            download_bytes=dev_down,
                            protocol="all",
                        )
                    )

                proto_deltas: list[ProtocolDelta] = []
                if proto_vless is not None:
                    v_up = int(vless_bytes * 0.16)
                    proto_deltas.append(
                        ProtocolDelta(
                            protocol_key=proto_vless.key,
                            source_tag=proto_vless.inbound_tag,
                            upload_bytes=v_up,
                            download_bytes=vless_bytes - v_up,
                            multiplier=proto_vless.oci_multiplier,
                        )
                    )
                if proto_hy2 is not None:
                    h_up = int(hy2_bytes * 0.17)
                    proto_deltas.append(
                        ProtocolDelta(
                            protocol_key=proto_hy2.key,
                            source_tag=proto_hy2.inbound_tag,
                            upload_bytes=h_up,
                            download_bytes=hy2_bytes - h_up,
                            multiplier=proto_hy2.oci_multiplier,
                        )
                    )

                self.db.record_deltas(dev_deltas, proto_deltas, timestamp=slot.timestamp())

    async def collect_deltas(self) -> tuple[list[DeviceDelta], list[ProtocolDelta]]:
        """Emit small realistic live deltas on every poll interval."""
        active_devices = [
            d["name"] for d in self.db.list_devices() if d["status"] == "active"
        ]
        if not active_devices:
            active_devices = list(self.config.default_devices)

        dev_deltas: list[DeviceDelta] = []
        total_up = 0
        total_down = 0

        for name in active_devices:
            weight = 2.4 if name == "owrt-router" else (1.3 if name == "mac" else 0.7)
            down = int(self._rng.uniform(1_500_000, 12_000_000) * weight)
            up = int(down * self._rng.uniform(0.10, 0.25))
            total_up += up
            total_down += down
            dev_deltas.append(
                DeviceDelta(device_name=name, upload_bytes=up, download_bytes=down, protocol="all")
            )

        proto_deltas: list[ProtocolDelta] = []
        if len(self.config.protocols) >= 2:
            v_ratio = self._rng.uniform(0.38, 0.46)
            p0, p1 = self.config.protocols[0], self.config.protocols[1]
            p0_up, p0_down = int(total_up * v_ratio), int(total_down * v_ratio)
            protocol_deltas_pair = [
                ProtocolDelta(
                    protocol_key=p0.key,
                    source_tag=p0.inbound_tag,
                    upload_bytes=p0_up,
                    download_bytes=p0_down,
                    multiplier=p0.oci_multiplier,
                ),
                ProtocolDelta(
                    protocol_key=p1.key,
                    source_tag=p1.inbound_tag,
                    upload_bytes=total_up - p0_up,
                    download_bytes=total_down - p0_down,
                    multiplier=p1.oci_multiplier,
                ),
            ]
            proto_deltas.extend(protocol_deltas_pair)
        elif len(self.config.protocols) == 1:
            p0 = self.config.protocols[0]
            proto_deltas.append(
                ProtocolDelta(
                    protocol_key=p0.key,
                    source_tag=p0.inbound_tag,
                    upload_bytes=total_up,
                    download_bytes=total_down,
                    multiplier=p0.oci_multiplier,
                )
            )

        return dev_deltas, proto_deltas

    def diagnostics(self) -> dict[str, Any]:
        return {
            "source": self.source_name,
            "mode": "demo_simulator",
            "timestamp": int(time.time()),
            "hint": "当前处于演示模拟模式，不会连接真实 sing-box API。",
        }
