from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from singbox_panel.config import AppConfig, default_protocols
from singbox_panel.db import DeviceDelta, ProtocolDelta, TrafficDatabase


def test_sqlite_persistence_windows_and_oci_redline(tmp_path: Path) -> None:
    cfg = AppConfig(protocols=default_protocols())
    cfg.server.timezone = "Asia/Singapore"
    db_file = tmp_path / "traffic.db"
    db = TrafficDatabase(db_file, config=cfg)

    tz = ZoneInfo("Asia/Singapore")
    now = datetime(2026, 11, 20, 14, 30, 0, tzinfo=tz)
    tb = 1000**4

    # 1. Previous month traffic (only counts in 'total', NOT 'today' or 'month')
    prev_month = datetime(2026, 10, 15, 10, 0, 0, tzinfo=tz)
    db.record_deltas(
        [DeviceDelta("owrt-router", upload_bytes=100_000, download_bytes=900_000)],
        [ProtocolDelta("sg_hysteria2", "hy2-sg-in", upload_bytes=100_000, download_bytes=900_000, multiplier=1.0)],
        timestamp=prev_month.timestamp(),
    )

    # 2. Earlier this month: 3 TB Tokyo VLESS (x2 = 6 TB OCI) + 2.6 TB SG Hy2 (x1 = 2.6 TB OCI)
    earlier_this_month = datetime(2026, 11, 10, 12, 0, 0, tzinfo=tz)
    db.record_deltas(
        [
            DeviceDelta("owrt-router", upload_bytes=int(0.5 * tb), download_bytes=int(3.5 * tb)),
            DeviceDelta("mac", upload_bytes=int(0.2 * tb), download_bytes=int(1.4 * tb)),
        ],
        [
            ProtocolDelta("tokyo_vless", "vless-tokyo-in", upload_bytes=int(0.5 * tb), download_bytes=int(2.5 * tb), multiplier=2.0),
            ProtocolDelta("sg_hysteria2", "hy2-sg-in", upload_bytes=int(0.2 * tb), download_bytes=int(2.4 * tb), multiplier=1.0),
        ],
        timestamp=earlier_this_month.timestamp(),
    )

    # 3. Today traffic: 10 GB mobile on SG Hy2
    gb = 1000**3
    db.record_deltas(
        [DeviceDelta("mobile", upload_bytes=2 * gb, download_bytes=8 * gb)],
        [ProtocolDelta("sg_hysteria2", "hy2-sg-in", upload_bytes=2 * gb, download_bytes=8 * gb, multiplier=1.0)],
        timestamp=now.timestamp(),
    )

    # Re-open fresh DB instance to verify disk persistence across restarts
    db2 = TrafficDatabase(db_file, config=cfg)

    today_devs = db2.get_device_summary("today", now=now)
    assert len(today_devs) == 1
    assert today_devs[0]["name"] == "mobile"
    assert today_devs[0]["total_bytes"] == 10 * gb
    assert today_devs[0]["share_percent"] == 100.0

    month_devs = {d["name"]: d for d in db2.get_device_summary("month", now=now)}
    assert month_devs["owrt-router"]["total_bytes"] == int(4.0 * tb)
    assert month_devs["mac"]["total_bytes"] == int(1.6 * tb)
    assert month_devs["mobile"]["total_bytes"] == 10 * gb

    total_devs = {d["name"]: d for d in db2.get_device_summary("total", now=now)}
    assert total_devs["owrt-router"]["total_bytes"] == int(4.0 * tb) + 1_000_000

    # OCI quota: 3 TB * 2 + (2.6 TB + 0.01 TB) * 1 = 8.61 TB / 10 TB = 86.1% -> danger!
    oci = db2.get_oci_quota_status(now=now)
    assert oci["limit_bytes"] == 10 * tb
    assert oci["used_bytes"] == 8_610_000_000_000
    assert oci["percent"] == 86.1
    assert oci["alert_level"] == "danger"
    assert oci["remaining_bytes"] == 1_390_000_000_000
    assert oci["projected_month_end_bytes"] > oci["used_bytes"]

    # Trend checks
    today_trend = db2.get_traffic_trend("today", now=now)
    assert len(today_trend) == 24
    assert today_trend[14]["total_bytes"] == 10 * gb

    month_trend = db2.get_traffic_trend("month", now=now)
    assert len(month_trend) == 20  # Nov 1 through Nov 20
    assert month_trend[9]["total_bytes"] == int(5.6 * tb)  # Nov 10
    assert month_trend[19]["total_bytes"] == 10 * gb  # Nov 20
