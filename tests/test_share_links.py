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
