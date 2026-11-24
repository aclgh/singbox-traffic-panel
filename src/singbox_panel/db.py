"""SQLite WAL storage engine for sing-box traffic persistence, OCI quota estimation, and devices."""

from __future__ import annotations

import calendar
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import AppConfig, OciQuotaConfig, ProtocolConfig


@dataclass
class DeviceDelta:
    device_name: str
    upload_bytes: int = 0
    download_bytes: int = 0
    protocol: str = "all"


@dataclass
class ProtocolDelta:
    protocol_key: str
    source_tag: str
    upload_bytes: int = 0
    download_bytes: int = 0
    multiplier: float = 1.0


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS device_traffic_hourly (
    hour_ts INTEGER NOT NULL,
    device_name TEXT NOT NULL,
    protocol TEXT NOT NULL DEFAULT 'all',
    upload_bytes INTEGER NOT NULL DEFAULT 0,
    download_bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_ts, device_name, protocol)
);

CREATE INDEX IF NOT EXISTS idx_device_traffic_hour
    ON device_traffic_hourly (hour_ts);

CREATE INDEX IF NOT EXISTS idx_device_traffic_name
    ON device_traffic_hourly (device_name, hour_ts);

CREATE TABLE IF NOT EXISTS protocol_traffic_hourly (
    hour_ts INTEGER NOT NULL,
    protocol_key TEXT NOT NULL,
    source_tag TEXT NOT NULL DEFAULT '',
    upload_bytes INTEGER NOT NULL DEFAULT 0,
    download_bytes INTEGER NOT NULL DEFAULT 0,
    multiplier REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (hour_ts, protocol_key)
);

CREATE INDEX IF NOT EXISTS idx_protocol_traffic_hour
    ON protocol_traffic_hourly (hour_ts);

CREATE TABLE IF NOT EXISTS devices (
    name TEXT PRIMARY KEY,
    note TEXT NOT NULL DEFAULT '',
    vless_uuid TEXT,
    vless_flow TEXT NOT NULL DEFAULT 'xtls-rprx-vision',
    hy2_password TEXT,
    enabled_protocols TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    detail TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS collector_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _floor_hour_ts(ts: float | int) -> int:
    return (int(ts) // 3600) * 3600


def _compute_billing_cycle(now_local: datetime, cycle_day: int) -> tuple[datetime, datetime]:
    """Compute [cycle_start, cycle_end) in local timezone for OCI monthly billing."""
    day = max(1, min(28, cycle_day))
    if now_local.day >= day:
        start = now_local.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
        if now_local.month == 12:
            end = start.replace(year=now_local.year + 1, month=1, day=day)
        else:
            end = start.replace(month=now_local.month + 1, day=day)
    else:
        if now_local.month == 1:
            prev_year, prev_month = now_local.year - 1, 12
        else:
            prev_year, prev_month = now_local.year, now_local.month - 1
        max_prev = calendar.monthrange(prev_year, prev_month)[1]
        start = now_local.replace(
            year=prev_year,
            month=prev_month,
            day=min(day, max_prev),
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        end = now_local.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    return start, end


class TrafficDatabase:
    """Thread-safe SQLite storage with WAL persistence and time-range analytics."""

    def __init__(self, db_path: str | Path, config: AppConfig | None = None) -> None:
        self.db_path = Path(db_path).expanduser()
        self.config = config
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(SCHEMA_SQL)
                conn.commit()
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass

    def _tz(self) -> ZoneInfo:
        tz_name = self.config.server.timezone if self.config else "Asia/Singapore"
        return ZoneInfo(tz_name)

    def _oci_config(self) -> OciQuotaConfig:
        return self.config.oci_quota if self.config else OciQuotaConfig()

    def _protocols(self) -> list[ProtocolConfig]:
        return self.config.protocols if self.config else []

    def record_deltas(
        self,
        device_deltas: list[DeviceDelta],
        protocol_deltas: list[ProtocolDelta],
        timestamp: float | None = None,
    ) -> int:
        """Atomically persist incremental traffic deltas into hourly SQLite buckets.

        Returns the number of non-zero rows updated.
        """
        ts = time.time() if timestamp is None else timestamp
        hour_ts = _floor_hour_ts(ts)
        updated = 0

        dev_rows = [
            (hour_ts, d.device_name, d.protocol or "all", int(d.upload_bytes), int(d.download_bytes))
            for d in device_deltas
            if d.device_name and (d.upload_bytes > 0 or d.download_bytes > 0)
        ]
        proto_rows = [
            (
                hour_ts,
                p.protocol_key,
                p.source_tag or "",
                int(p.upload_bytes),
                int(p.download_bytes),
                float(p.multiplier),
            )
            for p in protocol_deltas
            if p.protocol_key and (p.upload_bytes > 0 or p.download_bytes > 0)
        ]

        if not dev_rows and not proto_rows:
            return 0

        with self._lock, self._connect() as conn:
            if dev_rows:
                conn.executemany(
                    """
                    INSERT INTO device_traffic_hourly
                        (hour_ts, device_name, protocol, upload_bytes, download_bytes)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(hour_ts, device_name, protocol) DO UPDATE SET
                        upload_bytes = upload_bytes + excluded.upload_bytes,
                        download_bytes = download_bytes + excluded.download_bytes
                    """,
                    dev_rows,
                )
                updated += len(dev_rows)
            if proto_rows:
                conn.executemany(
                    """
                    INSERT INTO protocol_traffic_hourly
                        (hour_ts, protocol_key, source_tag, upload_bytes, download_bytes, multiplier)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(hour_ts, protocol_key) DO UPDATE SET
                        source_tag = CASE
                            WHEN excluded.source_tag != '' THEN excluded.source_tag
                            ELSE protocol_traffic_hourly.source_tag
                        END,
                        upload_bytes = upload_bytes + excluded.upload_bytes,
                        download_bytes = download_bytes + excluded.download_bytes,
                        multiplier = excluded.multiplier
                    """,
                    proto_rows,
                )
                updated += len(proto_rows)
            conn.commit()
        return updated

    def has_any_traffic(self) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM device_traffic_hourly) AS a, "
                "EXISTS(SELECT 1 FROM protocol_traffic_hourly) AS b"
            ).fetchone()
            return bool(row["a"] or row["b"])

    def _window_bounds(self, range_name: str, now: datetime | None = None) -> tuple[int, int]:
        tz = self._tz()
        now_local = (now or datetime.now(tz)).astimezone(tz)
        if range_name == "today":
            start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            return int(start.timestamp()), int(end.timestamp())
        if range_name == "month":
            start, end = _compute_billing_cycle(now_local, self._oci_config().billing_cycle_day)
            return int(start.timestamp()), int(end.timestamp())
        if range_name == "total":
            return 0, int((now_local + timedelta(hours=2)).timestamp())
        raise ValueError(f"Unsupported time range: {range_name!r}. Use 'today', 'month', or 'total'.")

    def get_device_summary(
        self,
        range_name: str = "month",
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate upload/download bytes and share per device for the selected window."""
        start_ts, end_ts = self._window_bounds(range_name, now)
        with self._lock, self._connect() as conn:
            traffic_rows = conn.execute(
                """
                SELECT
                    device_name,
                    SUM(upload_bytes) AS upload_bytes,
                    SUM(download_bytes) AS download_bytes
                FROM device_traffic_hourly
                WHERE hour_ts >= ? AND hour_ts < ?
                GROUP BY device_name
                """,
                (start_ts, end_ts),
            ).fetchall()

            device_rows = conn.execute(
                "SELECT * FROM devices ORDER BY status ASC, name ASC"
            ).fetchall()

        traffic_map: dict[str, tuple[int, int]] = {
            r["device_name"]: (int(r["upload_bytes"] or 0), int(r["download_bytes"] or 0))
            for r in traffic_rows
        }
        registry_map: dict[str, dict[str, Any]] = {
            r["name"]: self._row_to_device(r) for r in device_rows
        }

        all_names = list(registry_map.keys())
        for name in traffic_map:
            if name not in registry_map:
                all_names.append(name)

        grand_total = sum(up + down for up, down in traffic_map.values())
        items: list[dict[str, Any]] = []

        for name in all_names:
            up, down = traffic_map.get(name, (0, 0))
            total = up + down
            reg = registry_map.get(name)
            items.append(
                {
                    "name": name,
                    "note": reg["note"] if reg else "",
                    "status": reg["status"] if reg else "observed",
                    "enabled_protocols": reg["enabled_protocols"] if reg else [],
                    "has_vless": bool(reg and reg.get("vless_uuid")),
                    "has_hysteria2": bool(reg and reg.get("hy2_password")),
                    "created_at": reg["created_at"] if reg else None,
                    "revoked_at": reg["revoked_at"] if reg else None,
                    "upload_bytes": up,
                    "download_bytes": down,
                    "total_bytes": total,
                    "share_percent": round((total / grand_total) * 100, 2) if grand_total > 0 else 0.0,
                }
            )

        items.sort(key=lambda x: (-x["total_bytes"], x["status"] != "active", x["name"]))
        return items

    def get_protocol_summary(
        self,
        range_name: str = "month",
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate traffic per protocol (e.g., Tokyo VLESS vs SG Hysteria2)."""
        start_ts, end_ts = self._window_bounds(range_name, now)
        oci_cfg = self._oci_config()
        proto_cfg_map = {p.key: p for p in self._protocols()}

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    protocol_key,
                    MAX(source_tag) AS source_tag,
                    SUM(upload_bytes) AS upload_bytes,
                    SUM(download_bytes) AS download_bytes,
                    SUM(
                        CASE ?
                            WHEN 'download' THEN download_bytes * multiplier
                            WHEN 'upload' THEN upload_bytes * multiplier
                            ELSE (upload_bytes + download_bytes) * multiplier
                        END
                    ) AS weighted_bytes,
                    AVG(multiplier) AS avg_multiplier
                FROM protocol_traffic_hourly
                WHERE hour_ts >= ? AND hour_ts < ?
                GROUP BY protocol_key
                """,
                (oci_cfg.count_mode, start_ts, end_ts),
            ).fetchall()

        row_map = {r["protocol_key"]: r for r in rows}
        ordered_keys = [p.key for p in self._protocols()]
        for k in row_map:
            if k not in ordered_keys:
                ordered_keys.append(k)

        raw_grand = 0
        weighted_grand = 0
        raw_items: list[dict[str, Any]] = []

        for key in ordered_keys:
            r = row_map.get(key)
            cfg = proto_cfg_map.get(key)
            up = int(r["upload_bytes"] or 0) if r else 0
            down = int(r["download_bytes"] or 0) if r else 0
            total = up + down
            multiplier = cfg.oci_multiplier if cfg else (float(r["avg_multiplier"]) if r else 1.0)
            weighted = int(round(float(r["weighted_bytes"] or 0))) if r else 0
            raw_grand += total
            weighted_grand += weighted
            raw_items.append(
                {
                    "key": key,
                    "label": cfg.label if cfg else key,
                    "protocol_type": cfg.protocol_type if cfg else "unknown",
                    "inbound_tag": cfg.inbound_tag if cfg else (r["source_tag"] if r else ""),
                    "color": cfg.color if cfg else "#94a3b8",
                    "multiplier": multiplier,
                    "upload_bytes": up,
                    "download_bytes": down,
                    "total_bytes": total,
                    "oci_weighted_bytes": weighted,
                }
            )

        for item in raw_items:
            item["share_percent"] = (
                round((item["total_bytes"] / raw_grand) * 100, 2) if raw_grand > 0 else 0.0
            )
            item["oci_share_percent"] = (
                round((item["oci_weighted_bytes"] / weighted_grand) * 100, 2)
                if weighted_grand > 0
                else 0.0
            )

        return raw_items

    def get_oci_quota_status(self, now: datetime | None = None) -> dict[str, Any]:
        """Compute current billing-cycle OCI 10 TB Free Tier usage, breakdown, and projection."""
        tz = self._tz()
        now_local = (now or datetime.now(tz)).astimezone(tz)
        oci_cfg = self._oci_config()
        cycle_start, cycle_end = _compute_billing_cycle(now_local, oci_cfg.billing_cycle_day)

        protocols = self.get_protocol_summary("month", now=now_local)
        used_bytes = sum(p["oci_weighted_bytes"] for p in protocols)
        raw_total_bytes = sum(p["total_bytes"] for p in protocols)
        limit_bytes = oci_cfg.monthly_limit_bytes
        remaining_bytes = max(0, limit_bytes - used_bytes)
        percent = round((used_bytes / limit_bytes) * 100, 2) if limit_bytes > 0 else 0.0

        total_seconds = max(1.0, (cycle_end - cycle_start).total_seconds())
        elapsed_seconds = max(3600.0, (now_local - cycle_start).total_seconds())
        days_total = round(total_seconds / 86400.0, 1)
        days_elapsed = elapsed_seconds / 86400.0
        days_remaining = max(0.0, (cycle_end - now_local).total_seconds() / 86400.0)

        daily_burn_bytes = int(used_bytes / days_elapsed) if used_bytes > 0 else 0
        projected_bytes = int(daily_burn_bytes * (total_seconds / 86400.0))
        projected_percent = (
            round((projected_bytes / limit_bytes) * 100, 2) if limit_bytes > 0 else 0.0
        )

        if daily_burn_bytes > 0 and remaining_bytes > 0:
            est_days_to_limit: float | None = round(remaining_bytes / daily_burn_bytes, 1)
        elif remaining_bytes == 0 and used_bytes > 0:
            est_days_to_limit = 0.0
        else:
            est_days_to_limit = None

        if percent >= oci_cfg.danger_percent:
            alert_level = "danger"
        elif percent >= oci_cfg.warn_percent or projected_percent >= 100.0:
            alert_level = "warning"
        else:
            alert_level = "ok"

        return {
            "cycle_start": cycle_start.isoformat(),
            "cycle_end": cycle_end.isoformat(),
            "days_total": days_total,
            "days_elapsed": round(days_elapsed, 1),
            "days_remaining": round(days_remaining, 1),
            "monthly_limit_tb": oci_cfg.monthly_limit_tb,
            "unit_base": oci_cfg.unit_base,
            "count_mode": oci_cfg.count_mode,
            "limit_bytes": limit_bytes,
            "used_bytes": used_bytes,
            "raw_total_bytes": raw_total_bytes,
            "remaining_bytes": remaining_bytes,
            "percent": percent,
            "warn_percent": oci_cfg.warn_percent,
            "danger_percent": oci_cfg.danger_percent,
            "alert_level": alert_level,
            "daily_burn_bytes": daily_burn_bytes,
            "projected_month_end_bytes": projected_bytes,
            "projected_percent": projected_percent,
            "estimated_days_to_limit": est_days_to_limit,
            "breakdown": protocols,
        }

    def get_traffic_trend(
        self,
        range_name: str = "month",
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return time-series buckets for charts (hourly for today, daily for month/total)."""
        tz = self._tz()
        now_local = (now or datetime.now(tz)).astimezone(tz)
        oci_cfg = self._oci_config()

        if range_name == "today":
            start_dt = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            start_ts = int(start_dt.timestamp())
            end_ts = int((start_dt + timedelta(days=1)).timestamp())

            with self._lock, self._connect() as conn:
                dev_rows = conn.execute(
                    """
                    SELECT hour_ts, SUM(upload_bytes) AS up, SUM(download_bytes) AS down
                    FROM device_traffic_hourly
                    WHERE hour_ts >= ? AND hour_ts < ?
                    GROUP BY hour_ts
                    """,
                    (start_ts, end_ts),
                ).fetchall()
                proto_rows = conn.execute(
                    """
                    SELECT hour_ts,
                           SUM(upload_bytes) AS up,
                           SUM(download_bytes) AS down,
                           SUM(
                               CASE ?
                                   WHEN 'download' THEN download_bytes * multiplier
                                   WHEN 'upload' THEN upload_bytes * multiplier
                                   ELSE (upload_bytes + download_bytes) * multiplier
                               END
                           ) AS weighted
                    FROM protocol_traffic_hourly
                    WHERE hour_ts >= ? AND hour_ts < ?
                    GROUP BY hour_ts
                    """,
                    (oci_cfg.count_mode, start_ts, end_ts),
                ).fetchall()

            dev_map = {int(r["hour_ts"]): (int(r["up"] or 0), int(r["down"] or 0)) for r in dev_rows}
            proto_map = {
                int(r["hour_ts"]): (
                    int(r["up"] or 0),
                    int(r["down"] or 0),
                    int(round(float(r["weighted"] or 0))),
                )
                for r in proto_rows
            }

            points: list[dict[str, Any]] = []
            for h in range(24):
                slot_dt = start_dt + timedelta(hours=h)
                slot_ts = int(slot_dt.timestamp())
                d_up, d_down = dev_map.get(slot_ts, (0, 0))
                p_up, p_down, p_weighted = proto_map.get(slot_ts, (0, 0, 0))
                up = d_up if (d_up + d_down) > 0 else p_up
                down = d_down if (d_up + d_down) > 0 else p_down
                points.append(
                    {
                        "label": slot_dt.strftime("%H:00"),
                        "ts": slot_ts,
                        "upload_bytes": up,
                        "download_bytes": down,
                        "total_bytes": up + down,
                        "oci_weighted_bytes": p_weighted,
                    }
                )
            return points

        # Daily aggregation for 'month' or 'total' (last 30 days for total)
        if range_name == "month":
            cycle_start, _ = _compute_billing_cycle(now_local, oci_cfg.billing_cycle_day)
            start_day = cycle_start
        else:
            start_day = (now_local - timedelta(days=29)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        end_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        start_ts = int(start_day.timestamp())
        end_ts = int(end_day.timestamp())

        with self._lock, self._connect() as conn:
            dev_rows = conn.execute(
                """
                SELECT hour_ts, SUM(upload_bytes) AS up, SUM(download_bytes) AS down
                FROM device_traffic_hourly
                WHERE hour_ts >= ? AND hour_ts < ?
                GROUP BY hour_ts
                """,
                (start_ts, end_ts),
            ).fetchall()
            proto_rows = conn.execute(
                """
                SELECT hour_ts,
                       SUM(upload_bytes) AS up,
                       SUM(download_bytes) AS down,
                       SUM(
                           CASE ?
                               WHEN 'download' THEN download_bytes * multiplier
                               WHEN 'upload' THEN upload_bytes * multiplier
                               ELSE (upload_bytes + download_bytes) * multiplier
                           END
                       ) AS weighted
                FROM protocol_traffic_hourly
                WHERE hour_ts >= ? AND hour_ts < ?
                GROUP BY hour_ts
                """,
                (oci_cfg.count_mode, start_ts, end_ts),
            ).fetchall()

        daily_dev: dict[str, list[int]] = {}
        for r in dev_rows:
            day_key = datetime.fromtimestamp(int(r["hour_ts"]), tz).strftime("%Y-%m-%d")
            bucket = daily_dev.setdefault(day_key, [0, 0])
            bucket[0] += int(r["up"] or 0)
            bucket[1] += int(r["down"] or 0)

        daily_proto: dict[str, list[int]] = {}
        for r in proto_rows:
            day_key = datetime.fromtimestamp(int(r["hour_ts"]), tz).strftime("%Y-%m-%d")
            bucket = daily_proto.setdefault(day_key, [0, 0, 0])
            bucket[0] += int(r["up"] or 0)
            bucket[1] += int(r["down"] or 0)
            bucket[2] += int(round(float(r["weighted"] or 0)))

        points = []
        cursor = start_day
        while cursor < end_day:
            day_key = cursor.strftime("%Y-%m-%d")
            d_up, d_down = daily_dev.get(day_key, [0, 0])
            p_up, p_down, p_weighted = daily_proto.get(day_key, [0, 0, 0])
            up = d_up if (d_up + d_down) > 0 else p_up
            down = d_down if (d_up + d_down) > 0 else p_down
            points.append(
                {
                    "label": cursor.strftime("%m-%d"),
                    "date": day_key,
                    "ts": int(cursor.timestamp()),
                    "upload_bytes": up,
                    "download_bytes": down,
                    "total_bytes": up + down,
                    "oci_weighted_bytes": p_weighted,
                }
            )
            cursor += timedelta(days=1)
        return points

    # --- Device Registry CRUD ---

    @staticmethod
    def _row_to_device(row: sqlite3.Row) -> dict[str, Any]:
        try:
            protocols = json.loads(row["enabled_protocols"] or "[]")
        except json.JSONDecodeError:
            protocols = []
        return {
            "name": row["name"],
            "note": row["note"],
            "vless_uuid": row["vless_uuid"],
            "vless_flow": row["vless_flow"],
            "hy2_password": row["hy2_password"],
            "enabled_protocols": protocols,
            "status": row["status"],
            "created_at": row["created_at"],
            "revoked_at": row["revoked_at"],
        }

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM devices ORDER BY status ASC, name ASC"
            ).fetchall()
        return [self._row_to_device(r) for r in rows]

    def get_device(self, name: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM devices WHERE name = ?", (name,)).fetchone()
        return self._row_to_device(row) if row else None

    def upsert_device(
        self,
        *,
        name: str,
        note: str = "",
        vless_uuid: str | None = None,
        vless_flow: str = "xtls-rprx-vision",
        hy2_password: str | None = None,
        enabled_protocols: list[str] | None = None,
        status: str = "active",
        revoked_at: str | None = None,
    ) -> dict[str, Any]:
        now_iso = _utc_now_iso()
        protos = enabled_protocols if enabled_protocols is not None else []
        with self._lock, self._connect() as conn:
            existing = conn.execute("SELECT created_at, note FROM devices WHERE name = ?", (name,)).fetchone()
            created_at = existing["created_at"] if existing else now_iso
            final_note = note if note or not existing else existing["note"]
            conn.execute(
                """
                INSERT INTO devices
                    (name, note, vless_uuid, vless_flow, hy2_password,
                     enabled_protocols, status, created_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    note = excluded.note,
                    vless_uuid = excluded.vless_uuid,
                    vless_flow = excluded.vless_flow,
                    hy2_password = excluded.hy2_password,
                    enabled_protocols = excluded.enabled_protocols,
                    status = excluded.status,
                    revoked_at = excluded.revoked_at
                """,
                (
                    name,
                    final_note,
                    vless_uuid,
                    vless_flow,
                    hy2_password,
                    json.dumps(protos),
                    status,
                    created_at,
                    revoked_at,
                ),
            )
            conn.commit()
        dev = self.get_device(name)
        assert dev is not None
        return dev

    def mark_device_revoked(self, name: str) -> dict[str, Any] | None:
        now_iso = _utc_now_iso()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE devices
                SET status = 'revoked', revoked_at = ?
                WHERE name = ?
                """,
                (now_iso, name),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_device(name)

    def delete_device_record(self, name: str) -> bool:
        """Delete a revoked device record from registry (traffic history stays intact)."""
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM devices WHERE name = ?", (name,))
            conn.commit()
            return cur.rowcount > 0

    # --- Audit Logs & State ---

    def append_audit_log(
        self,
        action: str,
        target: str,
        detail: str,
        success: bool = True,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs (created_at, action, target, detail, success)
                VALUES (?, ?, ?, ?, ?)
                """,
                (_utc_now_iso(), action, target, detail, 1 if success else 0),
            )
            conn.commit()

    def list_audit_logs(self, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, action, target, detail, success "
                "FROM audit_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "action": r["action"],
                "target": r["target"],
                "detail": r["detail"],
                "success": bool(r["success"]),
            }
            for r in rows
        ]

    def set_state(self, key: str, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO collector_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, encoded, _utc_now_iso()),
            )
            conn.commit()

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM collector_state WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default
