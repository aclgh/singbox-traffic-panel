"""sing-box native experimental.v2ray_api collector (exact stats, zero short-connection loss)."""

from __future__ import annotations

from typing import Any

from ..config import AppConfig
from ..db import DeviceDelta, ProtocolDelta
from .base import BaseCollector
from .grpc_v2ray import V2RayStat, V2RayStatsClient


def parse_v2ray_stat_name(name: str) -> tuple[str, str, str] | None:
    """Parse `kind>>>tag>>>traffic>>>direction` -> (kind, tag, direction)."""
    parts = name.split(">>>")
    if len(parts) != 4 or parts[2] != "traffic":
        return None
    kind, tag, _, direction = parts
    if kind not in {"user", "inbound", "outbound"}:
        return None
    if direction not in {"uplink", "downlink"}:
        return None
    return kind, tag, direction


class V2RayApiCollector(BaseCollector):
    """Polls sing-box experimental.v2ray_api via h2c gRPC StatsService/QueryStats."""

    source_name = "v2ray_api"

    def __init__(self, config: AppConfig, client: V2RayStatsClient | None = None) -> None:
        self.config = config
        v2_cfg = config.collector.v2ray_api
        self.reset_on_poll = v2_cfg.reset_on_poll
        self.client = client or V2RayStatsClient(
            address=v2_cfg.address,
            timeout=v2_cfg.timeout_seconds,
        )
        self._prev_counters: dict[str, int] = {}
        self._last_stat_count: int = 0
        self._seen_users: set[str] = set()
        self._seen_inbounds: set[str] = set()
        self._seen_outbounds: set[str] = set()

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def probe(self) -> bool:
        return await self.client.probe()

    def _compute_delta_value(self, stat: V2RayStat) -> int:
        val = max(0, int(stat.value))
        if self.reset_on_poll:
            return val
        prev = self._prev_counters.get(stat.name)
        self._prev_counters[stat.name] = val
        if prev is None:
            # First poll without reset: avoid attributing historical uptime bytes to current hour
            return 0
        if val >= prev:
            return val - prev
        # sing-box restarted and counter reset to 0
        return val

    def parse_stats_to_deltas(
        self, stats: list[V2RayStat]
    ) -> tuple[list[DeviceDelta], list[ProtocolDelta]]:
        self._last_stat_count = len(stats)
        user_acc: dict[str, list[int]] = {}
        inbound_acc: dict[str, list[int]] = {}
        outbound_acc: dict[str, list[int]] = {}

        for st in stats:
            parsed = parse_v2ray_stat_name(st.name)
            if not parsed:
                continue
            kind, tag, direction = parsed
            delta_val = self._compute_delta_value(st)
            if kind == "user":
                self._seen_users.add(tag)
                bucket = user_acc.setdefault(tag, [0, 0])
            elif kind == "inbound":
                self._seen_inbounds.add(tag)
                bucket = inbound_acc.setdefault(tag, [0, 0])
            else:
                self._seen_outbounds.add(tag)
                bucket = outbound_acc.setdefault(tag, [0, 0])

            if delta_val > 0:
                if direction == "uplink":
                    bucket[0] += delta_val
                else:
                    bucket[1] += delta_val

        device_deltas = [
            DeviceDelta(device_name=user, upload_bytes=vals[0], download_bytes=vals[1], protocol="all")
            for user, vals in user_acc.items()
            if vals[0] > 0 or vals[1] > 0
        ]

        protocol_deltas: list[ProtocolDelta] = []
        matched_inbounds: set[str] = set()
        matched_outbounds: set[str] = set()

        for proto in self.config.protocols:
            up = 0
            down = 0
            source_tag = proto.inbound_tag

            if proto.stats_dimension == "outbound":
                for tag in proto.outbound_tags:
                    if tag in outbound_acc:
                        up += outbound_acc[tag][0]
                        down += outbound_acc[tag][1]
                        matched_outbounds.add(tag)
                        source_tag = tag
                # Fallback to inbound if outbound stats weren't enabled in sing-box config
                if up == 0 and down == 0 and proto.inbound_tag in inbound_acc:
                    up, down = inbound_acc[proto.inbound_tag]
                    matched_inbounds.add(proto.inbound_tag)
                    source_tag = proto.inbound_tag
            else:
                if proto.inbound_tag in inbound_acc:
                    up, down = inbound_acc[proto.inbound_tag]
                    matched_inbounds.add(proto.inbound_tag)
                    source_tag = proto.inbound_tag
                # Fallback to outbound if inbound stats weren't enabled
                if up == 0 and down == 0:
                    for tag in proto.outbound_tags:
                        if tag in outbound_acc:
                            up += outbound_acc[tag][0]
                            down += outbound_acc[tag][1]
                            matched_outbounds.add(tag)
                            source_tag = tag

            if up > 0 or down > 0:
                protocol_deltas.append(
                    ProtocolDelta(
                        protocol_key=proto.key,
                        source_tag=source_tag,
                        upload_bytes=up,
                        download_bytes=down,
                        multiplier=proto.oci_multiplier,
                    )
                )

        return device_deltas, protocol_deltas

    async def collect_deltas(self) -> tuple[list[DeviceDelta], list[ProtocolDelta]]:
        stats = await self.client.query_stats(pattern="", reset=self.reset_on_poll)
        return self.parse_stats_to_deltas(stats)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "source": self.source_name,
            "address": self.client.address,
            "reset_on_poll": self.reset_on_poll,
            "last_stat_count": self._last_stat_count,
            "seen_users": sorted(self._seen_users),
            "seen_inbounds": sorted(self._seen_inbounds),
            "seen_outbounds": sorted(self._seen_outbounds),
        }
