"""Share link (VLESS / Hysteria2) builder, sing-box client snippet generator, and offline QR encoder."""

from __future__ import annotations

import base64
import io
import json
from typing import Any
from urllib.parse import quote, urlencode

import segno

from .config import AppConfig, ProtocolConfig
from .singbox_config import SingBoxConfigManager


def _format_host_for_uri(host: str) -> str:
    h = host.strip()
    if ":" in h and not (h.startswith("[") and h.endswith("]")):
        return f"[{h}]"
    return h


def build_vless_uri(
    *,
    uuid_str: str,
    device_name: str,
    params: dict[str, Any],
    flow: str | None = None,
) -> str:
    """Build standard VLESS Reality / TLS share URI compatible with Shadowrocket, v2rayN, Nekobox, sing-box."""
    host = _format_host_for_uri(params["server_host"])
    port = int(params["server_port"])
    query: list[tuple[str, str]] = [("encryption", "none")]

    transport_type = str(params.get("transport_type") or "tcp")
    effective_flow = ""
    if transport_type == "tcp":
        effective_flow = flow if flow is not None else str(params.get("flow") or "")
    if effective_flow:
        query.append(("flow", effective_flow))

    query.append(("type", transport_type))
    if transport_type == "ws":
        if params.get("ws_host") or params.get("sni"):
            query.append(("host", str(params.get("ws_host") or params.get("sni"))))
        if params.get("transport_path"):
            query.append(("path", str(params["transport_path"])))
    elif transport_type == "grpc" and params.get("transport_service_name"):
        query.append(("serviceName", str(params["transport_service_name"])))

    if params.get("reality_enabled"):
        query.append(("security", "reality"))
        if params.get("sni"):
            query.append(("sni", str(params["sni"])))
        if params.get("fingerprint"):
            query.append(("fp", str(params["fingerprint"])))
        if params.get("public_key"):
            query.append(("pbk", str(params["public_key"])))
        if params.get("short_id"):
            query.append(("sid", str(params["short_id"])))
    elif params.get("tls_enabled", True):
        query.append(("security", "tls"))
        if params.get("sni"):
            query.append(("sni", str(params["sni"])))
        if params.get("fingerprint"):
            query.append(("fp", str(params["fingerprint"])))
        if params.get("insecure"):
            query.append(("allowInsecure", "1"))
    else:
        query.append(("security", "none"))

    remark = f"{params.get('remark_prefix') or 'VLESS'}-{device_name}"
    return f"vless://{quote(uuid_str, safe='')}@{host}:{port}?{urlencode(query)}#{quote(remark, safe='')}"


def build_hysteria2_uri(
    *,
    password: str,
    device_name: str,
    params: dict[str, Any],
) -> str:
    """Build official Hysteria2 share URI compatible with Shadowrocket, v2rayN, Nekobox, sing-box."""
    host = _format_host_for_uri(params["server_host"])
    port = int(params["server_port"])
    query: list[tuple[str, str]] = []

    if params.get("sni"):
        query.append(("sni", str(params["sni"])))
    if params.get("insecure"):
        query.append(("insecure", "1"))
    if params.get("pin_sha256"):
        query.append(("pinSHA256", str(params["pin_sha256"])))
    if params.get("alpn"):
        query.append(("alpn", str(params["alpn"])))
    if params.get("obfs_type"):
        query.append(("obfs", str(params["obfs_type"])))
        if params.get("obfs_password"):
            query.append(("obfs-password", str(params["obfs_password"])))

    remark = f"{params.get('remark_prefix') or 'Hysteria2'}-{device_name}"
    qs = f"?{urlencode(query)}" if query else ""
    return f"hysteria2://{quote(password, safe='')}@{host}:{port}{qs}#{quote(remark, safe='')}"


def build_singbox_outbound_snippet(
    *,
    proto: ProtocolConfig,
    device: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    """Generate a ready-to-paste sing-box client outbound JSON object."""
    if proto.protocol_type == "vless":
        transport_type = str(params.get("transport_type") or "tcp")
        outbound: dict[str, Any] = {
            "type": "vless",
            "tag": f"{proto.key}-{device['name']}",
            "server": params["server_host"],
            "server_port": int(params["server_port"]),
            "uuid": device.get("vless_uuid") or "",
        }
        if transport_type == "tcp":
            flow = device.get("vless_flow") or params.get("flow") or ""
            if flow:
                outbound["flow"] = flow
        if params.get("reality_enabled"):
            outbound["tls"] = {
                "enabled": True,
                "server_name": params.get("sni") or params["server_host"],
                "utls": {"enabled": True, "fingerprint": params.get("fingerprint") or "chrome"},
                "reality": {
                    "enabled": True,
                    "public_key": params.get("public_key") or "",
                    "short_id": params.get("short_id") or "",
                },
            }
        elif params.get("tls_enabled", True):
            tls_obj: dict[str, Any] = {
                "enabled": True,
                "server_name": params.get("sni") or params["server_host"],
                "utls": {"enabled": True, "fingerprint": params.get("fingerprint") or "chrome"},
            }
            if params.get("insecure"):
                tls_obj["insecure"] = True
            outbound["tls"] = tls_obj

        if transport_type == "ws":
            ws_obj: dict[str, Any] = {
                "type": "ws",
                "path": params.get("transport_path") or "/",
                "headers": {"Host": params.get("ws_host") or params.get("sni") or params["server_host"]},
            }
            if params.get("max_early_data"):
                ws_obj["max_early_data"] = int(params["max_early_data"])
                ws_obj["early_data_header_name"] = params.get("early_data_header_name") or "Sec-WebSocket-Protocol"
            outbound["transport"] = ws_obj
        elif transport_type == "grpc" and params.get("transport_service_name"):
            outbound["transport"] = {
                "type": "grpc",
                "service_name": str(params["transport_service_name"]),
            }
        return outbound

    # Hysteria2
    outbound = {
        "type": "hysteria2",
        "tag": f"{proto.key}-{device['name']}",
        "server": params["server_host"],
        "server_port": int(params["server_port"]),
        "password": device.get("hy2_password") or "",
        "tls": {
            "enabled": True,
            "server_name": params.get("sni") or params["server_host"],
            "insecure": bool(params.get("insecure", False)),
        },
    }
    if params.get("obfs_type"):
        outbound["obfs"] = {
            "type": params["obfs_type"],
            "password": params.get("obfs_password") or "",
        }
    return outbound


def render_qr_svg(content: str, scale: int = 6, border: int = 2) -> str:
    """Render an offline crisp SVG QR code string (zero external APIs)."""
    qr = segno.make(content, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=scale, border=border, dark="#0f172a", light="#ffffff", xmldecl=False)
    return buf.getvalue().decode("utf-8")


def render_qr_png_bytes(content: str, scale: int = 8, border: int = 2) -> bytes:
    """Render an offline PNG QR code bytes payload (pure Python via segno)."""
    qr = segno.make(content, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=border, dark="#0f172a", light="#ffffff")
    return buf.getvalue()


def render_qr_svg_data_uri(content: str) -> str:
    svg = render_qr_svg(content)
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def build_device_share_bundle(
    *,
    config: AppConfig,
    cfg_manager: SingBoxConfigManager,
    device: dict[str, Any],
) -> dict[str, Any]:
    """Build complete share URIs, offline QR SVG data URIs, and sing-box client JSON for a device."""
    raw_cfg: dict[str, Any] = {}
    if cfg_manager.config_exists():
        try:
            raw_cfg = cfg_manager.read_raw_config()
        except Exception:
            raw_cfg = {}

    enabled_set = set(device.get("enabled_protocols") or [])
    items: list[dict[str, Any]] = []
    client_outbounds: list[dict[str, Any]] = []

    for proto in config.protocols:
        if enabled_set and proto.key not in enabled_set:
            continue
        params = cfg_manager.extract_inbound_share_params(raw_cfg, proto)

        if proto.protocol_type == "vless" and device.get("vless_uuid"):
            uri = build_vless_uri(
                uuid_str=device["vless_uuid"],
                device_name=device["name"],
                params=params,
                flow=device.get("vless_flow"),
            )
            outbound = build_singbox_outbound_snippet(proto=proto, device=device, params=params)
            client_outbounds.append(outbound)
            warnings = []
            if params.get("reality_enabled") and not params.get("public_key"):
                warnings.append("未能自动推导出 REALITY public_key (pbk)，请在 panel.yaml 中填写。")
            items.append(
                {
                    "protocol_key": proto.key,
                    "label": proto.label,
                    "protocol_type": proto.protocol_type,
                    "color": proto.color,
                    "uri": uri,
                    "qr_svg_data_uri": render_qr_svg_data_uri(uri),
                    "server": f"{params['server_host']}:{params['server_port']}",
                    "sni": params.get("sni") or "",
                    "security": (
                        "REALITY"
                        if params.get("reality_enabled")
                        else ("WS + TLS" if params.get("transport_type") == "ws" else "TLS")
                    ),
                    "credential_preview": device["vless_uuid"],
                    "warnings": warnings,
                    "singbox_outbound": outbound,
                }
            )
        elif proto.protocol_type == "hysteria2" and device.get("hy2_password"):
            uri = build_hysteria2_uri(
                password=device["hy2_password"],
                device_name=device["name"],
                params=params,
            )
            outbound = build_singbox_outbound_snippet(proto=proto, device=device, params=params)
            client_outbounds.append(outbound)
            items.append(
                {
                    "protocol_key": proto.key,
                    "label": proto.label,
                    "protocol_type": proto.protocol_type,
                    "color": proto.color,
                    "uri": uri,
                    "qr_svg_data_uri": render_qr_svg_data_uri(uri),
                    "server": f"{params['server_host']}:{params['server_port']}",
                    "sni": params.get("sni") or "",
                    "security": "TLS/QUIC",
                    "credential_preview": device["hy2_password"],
                    "warnings": [],
                    "singbox_outbound": outbound,
                }
            )

    return {
        "device": {
            "name": device["name"],
            "note": device.get("note", ""),
            "status": device["status"],
            "created_at": device.get("created_at"),
            "revoked_at": device.get("revoked_at"),
        },
        "links": items,
        "singbox_client_json": json.dumps({"outbounds": client_outbounds}, indent=2, ensure_ascii=False),
    }
