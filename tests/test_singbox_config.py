import base64
import json
from pathlib import Path

import pytest

from singbox_panel.config import AppConfig, default_protocols
from singbox_panel.db import DeviceDelta, TrafficDatabase
from singbox_panel.singbox_config import SingBoxConfigError, SingBoxConfigManager


def _write_sample_singbox(path: Path) -> None:
    sample_priv = base64.urlsafe_b64encode(
        bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    ).rstrip(b"=").decode("ascii")
    cfg = {
        "log": {"level": "info"},
        "custom_preserved_section": {"keep_me": True},
        "inbounds": [
            {
                "type": "vless",
                "tag": "vless-tokyo-in",
                "listen": "::",
                "listen_port": 443,
                "users": [
                    {"name": "owrt-router", "uuid": "11111111-1111-4111-8111-111111111111", "flow": "xtls-rprx-vision"},
                    {"name": "mac", "uuid": "22222222-2222-4222-8222-222222222222", "flow": "xtls-rprx-vision"},
                ],
                "tls": {
                    "enabled": True,
                    "server_name": "www.microsoft.com",
                    "reality": {
                        "enabled": True,
                        "handshake": {"server": "www.microsoft.com", "server_port": 443},
                        "private_key": sample_priv,
                        "short_id": ["abcd1234"],
                    },
                },
            },
            {
                "type": "hysteria2",
                "tag": "hy2-sg-in",
                "listen": "::",
                "listen_port": 8443,
                "users": [
                    {"name": "owrt-router", "password": "pw-owrt"},
                    {"name": "mac", "password": "pw-mac"},
                ],
                "tls": {"enabled": True, "server_name": "sg.example.com"},
            },
        ],
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "experimental": {
            "v2ray_api": {
                "listen": "127.0.0.1:10085",
                "stats": {
                    "enabled": True,
                    "inbounds": ["vless-tokyo-in", "hy2-sg-in"],
                    "users": ["owrt-router", "mac"],
                },
            }
        },
    }
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _make_manager(tmp_path: Path, *, dry_run: bool = True) -> tuple[AppConfig, TrafficDatabase, SingBoxConfigManager, Path]:
    sb_path = tmp_path / "singbox-config.json"
    _write_sample_singbox(sb_path)
    cfg = AppConfig(protocols=default_protocols())
    cfg.storage.db_path = str(tmp_path / "traffic.db")
    cfg.storage.backup_dir = str(tmp_path / "backups")
    cfg.singbox.config_path = str(sb_path)
    cfg.singbox.dry_run = dry_run
    db = TrafficDatabase(cfg.storage.db_path, config=cfg)
    mgr = SingBoxConfigManager(cfg, db)
    return cfg, db, mgr, sb_path


def test_sync_add_revoke_restore_preserves_config_and_history(tmp_path: Path) -> None:
    _, db, mgr, sb_path = _make_manager(tmp_path, dry_run=True)

    # 1. Startup sync imports existing devices
    sync_res = mgr.sync_devices_from_config()
    assert sync_res["synced"] == 2
    assert set(sync_res["devices"]) == {"owrt-router", "mac"}

    # 2. Add new device 'ipad'
    add_res = mgr.add_device(name="ipad", note="Travel iPad")
    ipad = add_res["device"]
    assert ipad["status"] == "active"
    assert ipad["vless_uuid"]
    assert ipad["hy2_password"]
    assert len(mgr.list_backups()) == 1

    # Verify sing-box config updated cleanly AND untouched sections preserved
    raw = json.loads(sb_path.read_text(encoding="utf-8"))
    assert raw["custom_preserved_section"] == {"keep_me": True}
    assert any(u["name"] == "ipad" for u in raw["inbounds"][0]["users"])
    assert any(u["name"] == "ipad" for u in raw["inbounds"][1]["users"])
    assert "ipad" in raw["experimental"]["v2ray_api"]["stats"]["users"]

    # Record some traffic for ipad
    db.record_deltas([DeviceDelta("ipad", upload_bytes=500, download_bytes=1500)], [])

    # 3. Revoke 'ipad'
    rev_res = mgr.revoke_device("ipad")
    assert rev_res["device"]["status"] == "revoked"
    assert set(rev_res["removed_from"]) == {"tokyo_vless", "sg_hysteria2"}

    raw_after_rev = json.loads(sb_path.read_text(encoding="utf-8"))
    assert not any(u["name"] == "ipad" for u in raw_after_rev["inbounds"][0]["users"])
    assert not any(u["name"] == "ipad" for u in raw_after_rev["inbounds"][1]["users"])
    assert "ipad" not in raw_after_rev["experimental"]["v2ray_api"]["stats"]["users"]

    # Historical traffic is preserved!
    summary = {d["name"]: d for d in db.get_device_summary("month")}
    assert summary["ipad"]["total_bytes"] == 2000
    assert summary["ipad"]["status"] == "revoked"

    # 4. Restore 'ipad' rotates credentials by default
    restored = mgr.restore_device("ipad", rotate_credentials=True)["device"]
    assert restored["status"] == "active"
    assert restored["vless_uuid"] != ipad["vless_uuid"]
    assert restored["hy2_password"] != ipad["hy2_password"]


def test_singbox_check_failure_aborts_without_modifying_live_config(tmp_path: Path) -> None:
    cfg, _, mgr, sb_path = _make_manager(tmp_path, dry_run=False)
    # Point binary_path to /usr/bin/false so `check` fails with exit 1
    cfg.singbox.binary_path = "/usr/bin/false"
    cfg.singbox.validate_before_reload = True
    cfg.singbox.reload_command = "/usr/bin/true"

    before_text = sb_path.read_text(encoding="utf-8")
    with pytest.raises(SingBoxConfigError, match="校验未通过"):
        mgr.add_device(name="should-not-write")

    # Live config MUST remain completely untouched!
    assert sb_path.read_text(encoding="utf-8") == before_text


def test_reload_failure_rolls_back_config_from_backup(tmp_path: Path) -> None:
    cfg, _, mgr, sb_path = _make_manager(tmp_path, dry_run=False)
    cfg.singbox.binary_path = "/usr/bin/true"  # check succeeds
    cfg.singbox.validate_before_reload = True
    cfg.singbox.reload_command = "/usr/bin/false"  # reload fails

    before_text = sb_path.read_text(encoding="utf-8")
    with pytest.raises(SingBoxConfigError, match="重载失败"):
        mgr.add_device(name="rollback-test")

    # Live config MUST be rolled back to exact pre-edit contents!
    assert sb_path.read_text(encoding="utf-8") == before_text


def test_apply_and_rollback_preserve_file_ownership_and_revoke_uses_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    cfg, _, mgr, sb_path = _make_manager(tmp_path, dry_run=False)
    cfg.singbox.binary_path = "/usr/bin/true"
    cfg.singbox.validate_before_reload = True
    cfg.singbox.reload_command = "/usr/bin/true"
    cfg.singbox.revoke_reload_command = "/usr/bin/false"

    chown_calls: list[tuple[str, int, int]] = []
    real_stat = sb_path.stat()

    def fake_chown(path, uid, gid):
        chown_calls.append((str(path), uid, gid))

    monkeypatch.setattr(os, "chown", fake_chown)

    # 1. Add device succeeds and preserves uid:gid via os.chown
    mgr.add_device(name="owner-check")
    assert any(uid == real_stat.st_uid and gid == real_stat.st_gid for _, uid, gid in chown_calls)

    # 2. Revoke uses revoke_reload_command (/usr/bin/false), rolls back config, and restores uid:gid
    chown_calls.clear()
    with pytest.raises(SingBoxConfigError, match="重载失败"):
        mgr.revoke_device("owner-check")
    assert any(p == str(sb_path) and uid == real_stat.st_uid and gid == real_stat.st_gid for p, uid, gid in chown_calls)
    # Rolled back: owner-check is still in config.json
    assert "owner-check" in sb_path.read_text(encoding="utf-8")
