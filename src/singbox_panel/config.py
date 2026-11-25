"""Typed configuration loader with loopback-only safety enforcement."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback_host(host: str) -> bool:
    """Return True if host binds strictly to a loopback interface."""
    h = host.strip().lower()
    if h in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8088
    allow_non_loopback: bool = False
    api_token: str = ""
    timezone: str = "Asia/Singapore"
    ssh_host_hint: str = "root@your-sg-vps"

    def validate_bind(self) -> None:
        if not is_loopback_host(self.host):
            if not self.allow_non_loopback:
                raise ValueError(
                    f"Refusing to bind to non-loopback address {self.host!r}. "
                    "Keep host='127.0.0.1' and access via SSH tunnel "
                    f"(ssh -L {self.port}:127.0.0.1:{self.port} <vps>), "
                    "or explicitly set server.allow_non_loopback=true with server.api_token."
                )
            if not self.api_token.strip():
                raise ValueError(
                    "server.api_token is required when server.allow_non_loopback=true."
                )
        # Validate timezone name early
        ZoneInfo(self.timezone)


@dataclass
class StorageConfig:
    db_path: str = "./data/traffic.db"
    backup_dir: str = "./data/backups"
    max_backups: int = 20


@dataclass
class V2RayApiConfig:
    address: str = "127.0.0.1:10085"
    reset_on_poll: bool = True
    timeout_seconds: float = 5.0


@dataclass
class ClashApiConfig:
    ws_url: str = "ws://127.0.0.1:9090/connections"
    secret: str = ""
    reconnect_interval_seconds: float = 3.0


@dataclass
class CollectorConfig:
    source: str = "auto"  # v2ray_api | clash_api | auto | mock
    flush_interval_seconds: int = 15  # recommended 10-30s
    v2ray_api: V2RayApiConfig = field(default_factory=V2RayApiConfig)
    clash_api: ClashApiConfig = field(default_factory=ClashApiConfig)


@dataclass
class SingBoxConfig:
    config_path: str = "/etc/sing-box/config.json"
    binary_path: str = "sing-box"
    validate_before_reload: bool = True
    reload_command: str = "systemctl reload-or-restart sing-box"
    revoke_reload_command: str = ""  # empty = fallback to reload_command; set e.g. 'systemctl restart sing-box' to immediately drop revoked connections
    sync_v2ray_api_users: bool = True
    dry_run: bool = False  # if True, skip external sing-box check/reload (useful for demo/tests)


@dataclass
class ClientShareConfig:
    server_host: str = "sg.example.com"
    server_port: int | None = None
    sni: str | None = None
    tls_enabled: bool | None = None
    transport_type: str = ""
    ws_path: str = ""
    ws_host: str = ""
    public_key: str = ""
    short_id: str = ""
    fingerprint: str = "chrome"
    flow: str = "xtls-rprx-vision"
    insecure: bool = False
    pin_sha256: str = ""
    alpn: str = ""
    remark_prefix: str = ""


@dataclass
class ProtocolConfig:
    key: str
    label: str
    protocol_type: str  # vless | hysteria2
    inbound_tag: str
    outbound_tags: list[str] = field(default_factory=list)
    stats_dimension: str = "inbound"  # inbound | outbound
    oci_multiplier: float = 1.0
    color: str = "#38bdf8"
    client: ClientShareConfig = field(default_factory=ClientShareConfig)


@dataclass
class OciQuotaConfig:
    monthly_limit_tb: float = 10.0
    unit_base: int = 1000  # 1000 = decimal TB (OCI billing), 1024 = TiB
    billing_cycle_day: int = 1
    count_mode: str = "total"  # total | download | upload
    warn_percent: float = 70.0
    danger_percent: float = 85.0

    @property
    def monthly_limit_bytes(self) -> int:
        return int(self.monthly_limit_tb * (self.unit_base**4))


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    singbox: SingBoxConfig = field(default_factory=SingBoxConfig)
    oci_quota: OciQuotaConfig = field(default_factory=OciQuotaConfig)
    protocols: list[ProtocolConfig] = field(default_factory=list)
    default_devices: list[str] = field(
        default_factory=lambda: ["owrt-router", "mac", "mobile"]
    )
    config_file_path: str | None = None

    def protocol_by_key(self, key: str) -> ProtocolConfig | None:
        for proto in self.protocols:
            if proto.key == key:
                return proto
        return None

    def protocol_by_type(self, protocol_type: str) -> ProtocolConfig | None:
        for proto in self.protocols:
            if proto.protocol_type == protocol_type:
                return proto
        return None


def default_protocols() -> list[ProtocolConfig]:
    return [
        ProtocolConfig(
            key="tokyo_vless",
            label="东京 VLESS (中转)",
            protocol_type="vless",
            inbound_tag="vless-tokyo-in",
            outbound_tags=["tokyo-vless-out", "tokyo-out"],
            stats_dimension="inbound",
            oci_multiplier=2.0,
            color="#38bdf8",
            client=ClientShareConfig(
                server_host="sg.example.com",
                fingerprint="chrome",
                flow="xtls-rprx-vision",
                remark_prefix="Tokyo-VLESS",
            ),
        ),
        ProtocolConfig(
            key="sg_hysteria2",
            label="新加坡 Hysteria2 (直连)",
            protocol_type="hysteria2",
            inbound_tag="hy2-sg-in",
            outbound_tags=["direct"],
            stats_dimension="inbound",
            oci_multiplier=1.0,
            color="#a78bfa",
            client=ClientShareConfig(
                server_host="sg.example.com",
                insecure=False,
                remark_prefix="SG-Hy2",
            ),
        ),
    ]


def _build_client_config(raw: dict[str, Any] | None) -> ClientShareConfig:
    raw = raw or {}
    return ClientShareConfig(
        server_host=str(raw.get("server_host", "sg.example.com")),
        server_port=int(raw["server_port"]) if raw.get("server_port") is not None else None,
        sni=str(raw["sni"]) if raw.get("sni") else None,
        tls_enabled=bool(raw["tls_enabled"]) if raw.get("tls_enabled") is not None else None,
        transport_type=str(raw.get("transport_type", "") or ""),
        ws_path=str(raw.get("ws_path", "") or ""),
        ws_host=str(raw.get("ws_host", "") or ""),
        public_key=str(raw.get("public_key", "") or ""),
        short_id=str(raw.get("short_id", "") or ""),
        fingerprint=str(raw.get("fingerprint", "chrome") or "chrome"),
        flow=str(raw.get("flow", "xtls-rprx-vision") or ""),
        insecure=bool(raw.get("insecure", False)),
        pin_sha256=str(raw.get("pin_sha256", "") or ""),
        alpn=str(raw.get("alpn", "") or ""),
        remark_prefix=str(raw.get("remark_prefix", "") or ""),
    )


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load AppConfig from YAML file with environment variable overrides."""
    raw: dict[str, Any] = {}
    resolved_path: str | None = None

    env_path = os.environ.get("SINGBOX_PANEL_CONFIG")
    target = config_path or env_path
    if target is not None:
        p = Path(target).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"Configuration file not found: {p}")
        with p.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
            if loaded is not None:
                if not isinstance(loaded, dict):
                    raise ValueError(f"Configuration root must be a mapping: {p}")
                raw = loaded
        resolved_path = str(p)

    srv_raw = raw.get("server") or {}
    server = ServerConfig(
        host=os.environ.get("SINGBOX_PANEL_HOST", str(srv_raw.get("host", "127.0.0.1"))),
        port=int(os.environ.get("SINGBOX_PANEL_PORT", srv_raw.get("port", 8088))),
        allow_non_loopback=bool(srv_raw.get("allow_non_loopback", False)),
        api_token=os.environ.get("SINGBOX_PANEL_TOKEN", str(srv_raw.get("api_token", "") or "")),
        timezone=str(srv_raw.get("timezone", "Asia/Singapore")),
        ssh_host_hint=str(srv_raw.get("ssh_host_hint", "root@your-sg-vps")),
    )

    stg_raw = raw.get("storage") or {}
    storage = StorageConfig(
        db_path=os.environ.get("SINGBOX_PANEL_DB", str(stg_raw.get("db_path", "./data/traffic.db"))),
        backup_dir=str(stg_raw.get("backup_dir", "./data/backups")),
        max_backups=max(1, int(stg_raw.get("max_backups", 20))),
    )

    col_raw = raw.get("collector") or {}
    v2_raw = col_raw.get("v2ray_api") or {}
    cl_raw = col_raw.get("clash_api") or {}
    flush_interval = int(
        os.environ.get(
            "SINGBOX_PANEL_FLUSH_INTERVAL",
            col_raw.get("flush_interval_seconds", 15),
        )
    )
    collector = CollectorConfig(
        source=os.environ.get("SINGBOX_PANEL_SOURCE", str(col_raw.get("source", "auto"))),
        flush_interval_seconds=max(5, min(300, flush_interval)),
        v2ray_api=V2RayApiConfig(
            address=str(v2_raw.get("address", "127.0.0.1:10085")),
            reset_on_poll=bool(v2_raw.get("reset_on_poll", True)),
            timeout_seconds=float(v2_raw.get("timeout_seconds", 5.0)),
        ),
        clash_api=ClashApiConfig(
            ws_url=str(cl_raw.get("ws_url", "ws://127.0.0.1:9090/connections")),
            secret=str(cl_raw.get("secret", "") or ""),
            reconnect_interval_seconds=float(cl_raw.get("reconnect_interval_seconds", 3.0)),
        ),
    )

    sb_raw = raw.get("singbox") or {}
    singbox = SingBoxConfig(
        config_path=os.environ.get(
            "SINGBOX_CONFIG_PATH",
            str(sb_raw.get("config_path", "/etc/sing-box/config.json")),
        ),
        binary_path=str(sb_raw.get("binary_path", "sing-box")),
        validate_before_reload=bool(sb_raw.get("validate_before_reload", True)),
        reload_command=str(sb_raw.get("reload_command", "systemctl reload-or-restart sing-box")),
        revoke_reload_command=str(sb_raw.get("revoke_reload_command", "") or ""),
        sync_v2ray_api_users=bool(sb_raw.get("sync_v2ray_api_users", True)),
        dry_run=bool(sb_raw.get("dry_run", False)),
    )

    oci_raw = raw.get("oci_quota") or {}
    oci_quota = OciQuotaConfig(
        monthly_limit_tb=float(oci_raw.get("monthly_limit_tb", 10.0)),
        unit_base=int(oci_raw.get("unit_base", 1000)),
        billing_cycle_day=max(1, min(28, int(oci_raw.get("billing_cycle_day", 1)))),
        count_mode=str(oci_raw.get("count_mode", "total")),
        warn_percent=float(oci_raw.get("warn_percent", 70.0)),
        danger_percent=float(oci_raw.get("danger_percent", 85.0)),
    )

    protos_raw = raw.get("protocols")
    if protos_raw:
        protocols = [
            ProtocolConfig(
                key=str(p["key"]),
                label=str(p.get("label", p["key"])),
                protocol_type=str(p.get("protocol_type", "vless")),
                inbound_tag=str(p.get("inbound_tag", "")),
                outbound_tags=[str(t) for t in (p.get("outbound_tags") or [])],
                stats_dimension=str(p.get("stats_dimension", "inbound")),
                oci_multiplier=float(p.get("oci_multiplier", 1.0)),
                color=str(p.get("color", "#38bdf8")),
                client=_build_client_config(p.get("client")),
            )
            for p in protos_raw
        ]
    else:
        protocols = default_protocols()

    default_devices = [
        str(d) for d in (raw.get("default_devices") or ["owrt-router", "mac", "mobile"])
    ]

    cfg = AppConfig(
        server=server,
        storage=storage,
        collector=collector,
        singbox=singbox,
        oci_quota=oci_quota,
        protocols=protocols,
        default_devices=default_devices,
        config_file_path=resolved_path,
    )
    cfg.server.validate_bind()
    return cfg
