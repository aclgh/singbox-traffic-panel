import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from singbox_panel.share_links import (
    build_device_share_bundle,
    render_qr_png_bytes,
    render_qr_svg,
)
from tests.test_singbox_config import _make_manager


def test_share_links_and_offline_qr_generation(tmp_path: Path) -> None:
    cfg, db, mgr, _ = _make_manager(tmp_path, dry_run=True)
    cfg.protocols[1].client.insecure = True
    cfg.protocols[1].client.pin_sha256 = "AA:BB:CC:DD"
    mgr.sync_devices_from_config()

    device = db.get_device("owrt-router")
    assert device is not None
    bundle = build_device_share_bundle(config=cfg, cfg_manager=mgr, device=device)

    assert len(bundle["links"]) == 2
    by_key = {item["protocol_key"]: item for item in bundle["links"]}

    # Verify VLESS Reality URI including auto-derived X25519 pbk
    vless = by_key["tokyo_vless"]
    parsed_v = urlparse(vless["uri"])
    assert parsed_v.scheme == "vless"
    assert parsed_v.username == "11111111-1111-4111-8111-111111111111"
    assert parsed_v.hostname == "sg.example.com"
    assert parsed_v.port == 443
    qs = parse_qs(parsed_v.query)
    assert qs["security"] == ["reality"]
    assert qs["flow"] == ["xtls-rprx-vision"]
    assert qs["sni"] == ["www.microsoft.com"]
    assert qs["sid"] == ["abcd1234"]
    assert qs["pbk"] == ["hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo"]  # RFC 7748 Alice pubkey!
    assert vless["qr_svg_data_uri"].startswith("data:image/svg+xml;base64,")

    # Verify Hysteria2 URI
    hy2 = by_key["sg_hysteria2"]
    parsed_h = urlparse(hy2["uri"])
    assert parsed_h.scheme == "hysteria2"
    assert parsed_h.username == "pw-owrt"
    assert parsed_h.port == 8443
    hy2_qs = parse_qs(parsed_h.query)
    assert hy2_qs["sni"] == ["sg.example.com"]
    assert hy2_qs["insecure"] == ["1"]
    assert hy2_qs["pinSHA256"] == ["AA:BB:CC:DD"]

    # Verify sing-box client JSON snippet parses cleanly
    client_json = json.loads(bundle["singbox_client_json"])
    assert len(client_json["outbounds"]) == 2

    # Verify raw SVG & PNG QR generators produce valid offline image payloads
    svg = render_qr_svg(vless["uri"])
    assert "<svg" in svg and "</svg>" in svg

    png = render_qr_png_bytes(hy2["uri"])
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 200


def test_multi_vless_reality_and_cloudflare_ws_share_uuid_and_omit_ws_flow(tmp_path: Path) -> None:
    from singbox_panel.config import ClientShareConfig, ProtocolConfig

    cfg, db, mgr, sb_path = _make_manager(tmp_path, dry_run=True)

    # Add a 3rd inbound (local plaintext VLESS+WS behind Cloudflare Tunnel)
    raw = json.loads(sb_path.read_text(encoding="utf-8"))
    raw["inbounds"].append(
        {
            "type": "vless",
            "tag": "vless-ws-cf",
            "listen": "127.0.0.1",
            "listen_port": 10443,
            "users": [
                {"name": "owrt-router", "uuid": "11111111-1111-4111-8111-111111111111"},
            ],
            "transport": {
                "type": "ws",
                "path": "/sync-abc123",
                "max_early_data": 2048,
                "early_data_header_name": "Sec-WebSocket-Protocol",
            },
        }
    )
    sb_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    cfg.protocols.append(
        ProtocolConfig(
            key="cf_vless_ws",
            label="Cloudflare CDN (WS+TLS)",
            protocol_type="vless",
            inbound_tag="vless-ws-cf",
            oci_multiplier=1.0,
            color="#34d399",
            client=ClientShareConfig(
                server_host="cdn.007218.xyz",
                server_port=443,
                sni="cdn.007218.xyz",
                tls_enabled=True,
                ws_host="cdn.007218.xyz",
                flow="",
                remark_prefix="CF-VLESS",
            ),
        )
    )

    # 1. Sync preserves xtls-rprx-vision on device despite vless-ws-cf having no flow
    mgr.sync_devices_from_config()
    owrt = db.get_device("owrt-router")
    assert owrt is not None
    assert owrt["vless_flow"] == "xtls-rprx-vision"
    assert set(owrt["enabled_protocols"]) == {"tokyo_vless", "sg_hysteria2", "cf_vless_ws"}

    # 2. Adding a new device uses ONE shared UUID across both VLESS inbounds and omits flow on WS
    added = mgr.add_device(name="cf-laptop")["device"]
    after = json.loads(sb_path.read_text(encoding="utf-8"))
    reality_user = next(u for u in after["inbounds"][0]["users"] if u["name"] == "cf-laptop")
    ws_user = next(u for u in after["inbounds"][2]["users"] if u["name"] == "cf-laptop")
    assert reality_user["uuid"] == ws_user["uuid"] == added["vless_uuid"]
    assert reality_user["flow"] == "xtls-rprx-vision"
    assert "flow" not in ws_user

    # 3. Share bundle generates valid REALITY (with flow) AND valid CF WS+TLS (without flow, with host+path+transport)
    bundle = build_device_share_bundle(config=cfg, cfg_manager=mgr, device=added)
    by_key = {item["protocol_key"]: item for item in bundle["links"]}
    assert set(by_key) == {"tokyo_vless", "sg_hysteria2", "cf_vless_ws"}

    cf = by_key["cf_vless_ws"]
    assert cf["security"] == "WS + TLS"
    parsed_cf = urlparse(cf["uri"])
    assert parsed_cf.hostname == "cdn.007218.xyz"
    assert parsed_cf.port == 443
    cf_qs = parse_qs(parsed_cf.query)
    assert cf_qs["type"] == ["ws"]
    assert cf_qs["security"] == ["tls"]
    assert cf_qs["sni"] == ["cdn.007218.xyz"]
    assert cf_qs["host"] == ["cdn.007218.xyz"]
    assert cf_qs["path"] == ["/sync-abc123"]
    assert "flow" not in cf_qs

    # Verify sing-box client outbound JSON for CF WS+TLS
    cf_ob = cf["singbox_outbound"]
    assert "flow" not in cf_ob
    assert cf_ob["transport"]["type"] == "ws"
    assert cf_ob["transport"]["path"] == "/sync-abc123"
    assert cf_ob["transport"]["headers"] == {"Host": "cdn.007218.xyz"}
    assert cf_ob["transport"]["max_early_data"] == 2048
    assert cf_ob["tls"]["enabled"] is True
    assert cf_ob["tls"]["server_name"] == "cdn.007218.xyz"

    # 4. Revoke removes user from ALL 3 inbounds cleanly
    rev = mgr.revoke_device("cf-laptop")
    assert set(rev["removed_from"]) == {"tokyo_vless", "sg_hysteria2", "cf_vless_ws"}
