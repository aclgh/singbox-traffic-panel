from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from singbox_panel.app import create_app
from singbox_panel.config import AppConfig, ServerConfig, default_protocols, load_config
from tests.test_singbox_config import _write_sample_singbox


def test_example_yaml_loads_cleanly() -> None:
    example_path = Path(__file__).resolve().parents[1] / "config" / "panel.example.yaml"
    cfg = load_config(example_path)
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8088
    assert cfg.oci_quota.monthly_limit_tb == 10.0
    assert [p.key for p in cfg.protocols] == ["tokyo_vless", "sg_hysteria2"]
    assert cfg.protocols[0].oci_multiplier == 2.0
    assert cfg.protocols[1].oci_multiplier == 1.0


def test_api_token_protection_when_enabled(tmp_path: Path) -> None:
    sb_path = tmp_path / "singbox.json"
    _write_sample_singbox(sb_path)
    cfg = AppConfig(protocols=default_protocols())
    cfg.server.api_token = "my-secret-token"
    cfg.storage.db_path = str(tmp_path / "panel.db")
    cfg.singbox.config_path = str(sb_path)
    cfg.singbox.dry_run = True

    app = create_app(cfg, start_collector=False)
    with TestClient(app) as client:
        assert client.get("/api/dashboard").status_code == 401
        assert (
            client.get("/api/dashboard", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        assert (
            client.get(
                "/api/dashboard", headers={"Authorization": "Bearer my-secret-token"}
            ).status_code
            == 200
        )


def test_loopback_security_guard_rejects_public_bind() -> None:
    srv = ServerConfig(host="0.0.0.0", port=8088, allow_non_loopback=False)
    with pytest.raises(ValueError, match="Refusing to bind to non-loopback"):
        srv.validate_bind()

    srv_no_tok = ServerConfig(host="0.0.0.0", port=8088, allow_non_loopback=True, api_token="")
    with pytest.raises(ValueError, match="api_token is required"):
        srv_no_tok.validate_bind()

    srv_ok = ServerConfig(host="127.0.0.1", port=8088)
    srv_ok.validate_bind()


def test_end_to_end_dashboard_and_device_lifecycle_api(tmp_path: Path) -> None:
    sb_path = tmp_path / "singbox.json"
    _write_sample_singbox(sb_path)

    cfg = AppConfig(protocols=default_protocols())
    cfg.collector.source = "mock"
    cfg.storage.db_path = str(tmp_path / "panel.db")
    cfg.storage.backup_dir = str(tmp_path / "backups")
    cfg.singbox.config_path = str(sb_path)
    cfg.singbox.dry_run = True

    app = create_app(cfg, start_collector=True)

    with TestClient(app) as client:
        # 1. Index HTML served locally
        home = client.get("/")
        assert home.status_code == 200
        assert "sing-box Traffic Panel" in home.text

        # 2. Health check
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert health["loopback_only"] is True
        assert health["collector_source"] == "mock"

        # 3. Dashboard renders Today / Month / Total cleanly
        for r in ("today", "month", "total"):
            dash = client.get(f"/api/dashboard?range={r}")
            assert dash.status_code == 200
            body = dash.json()
            assert body["range"] == r
            assert body["oci_quota"]["monthly_limit_tb"] == 10.0
            assert len(body["protocols"]) == 2

        # 4. Create a new device and verify immediate QR bundle returned
        create_resp = client.post(
            "/api/devices",
            json={
                "name": "apple-tv",
                "note": "Living room",
                "protocols": ["tokyo_vless", "sg_hysteria2"],
            },
        )
        assert create_resp.status_code == 201
        created = create_resp.json()
        assert created["device"]["name"] == "apple-tv"
        assert len(created["share"]["links"]) == 2
        orig_vless_uri = created["share"]["links"][0]["uri"]

        # 5. Download offline PNG & SVG QR codes
        png_resp = client.get("/api/devices/apple-tv/qrcode?protocol=tokyo_vless&format=png")
        assert png_resp.status_code == 200
        assert png_resp.headers["content-type"] == "image/png"
        assert png_resp.content.startswith(b"\x89PNG\r\n\x1a\n")

        svg_resp = client.get("/api/devices/apple-tv/qrcode?protocol=sg_hysteria2&format=svg")
        assert svg_resp.status_code == 200
        assert "image/svg+xml" in svg_resp.headers["content-type"]
        assert "<svg" in svg_resp.text

        # 6. Revoke device -> share endpoint blocked with 409
        rev = client.post("/api/devices/apple-tv/revoke")
        assert rev.status_code == 200
        assert rev.json()["device"]["status"] == "revoked"
        assert client.get("/api/devices/apple-tv/share").status_code == 409

        # 7. Restore device -> rotates keys so URI changes!
        rest = client.post(
            "/api/devices/apple-tv/restore",
            json={"rotate_credentials": True},
        )
        assert rest.status_code == 200
        new_vless_uri = rest.json()["share"]["links"][0]["uri"]
        assert new_vless_uri != orig_vless_uri

        # 8. Active device cannot be deleted directly without revoking first
        assert client.delete("/api/devices/apple-tv").status_code == 409
        assert client.post("/api/devices/apple-tv/revoke").status_code == 200
        assert client.delete("/api/devices/apple-tv").status_code == 200

        # 9. Audit log contains full history
        audit = client.get("/api/audit").json()
        actions = [item["action"] for item in audit["logs"]]
        assert "device.create" in actions
        assert "device.revoke" in actions
        assert "device.restore" in actions
        assert len(audit["backups"]) >= 3
