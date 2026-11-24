"""FastAPI backend for sing-box loopback traffic monitor & credential manager."""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .collectors.base import BaseCollector, CollectorManager
from .config import AppConfig, is_loopback_host
from .db import TrafficDatabase
from .share_links import build_device_share_bundle, render_qr_png_bytes, render_qr_svg
from .singbox_config import SingBoxConfigError, SingBoxConfigManager, validate_device_name

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class CreateDeviceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=48)
    note: str = Field(default="", max_length=120)
    protocols: list[str] | None = Field(default=None)
    vless_uuid: str | None = Field(default=None)
    vless_flow: str | None = Field(default=None)
    hy2_password: str | None = Field(default=None)


class RestoreDeviceRequest(BaseModel):
    rotate_credentials: bool = True
    protocols: list[str] | None = None


class PanelRuntime:
    """Container holding live DB, sing-box config manager, and collector manager."""

    def __init__(
        self,
        config: AppConfig,
        *,
        db: TrafficDatabase | None = None,
        collector_override: BaseCollector | None = None,
    ) -> None:
        self.config = config
        self.db = db or TrafficDatabase(config.storage.db_path, config=config)
        self.cfg_manager = SingBoxConfigManager(config, self.db)
        self.collector_manager = CollectorManager(config, self.db, collector=collector_override)


def create_app(
    config: AppConfig,
    *,
    db: TrafficDatabase | None = None,
    collector_override: BaseCollector | None = None,
    start_collector: bool = True,
) -> FastAPI:
    config.server.validate_bind()
    runtime = PanelRuntime(config, db=db, collector_override=collector_override)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = runtime
        # Auto-discover existing devices from sing-box config.json on startup
        try:
            sync_res = runtime.cfg_manager.sync_devices_from_config()
            if not sync_res.get("skipped"):
                logger.info("Synced %d existing devices from sing-box config", sync_res["synced"])
        except Exception as exc:
            logger.warning("Initial sing-box config sync failed: %s", exc)

        if start_collector:
            await runtime.collector_manager.start()
        try:
            yield
        finally:
            if start_collector:
                await runtime.collector_manager.stop()

    app = FastAPI(
        title="sing-box Traffic Panel",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime

    async def verify_auth(request: Request) -> None:
        token = config.server.api_token.strip()
        if not token:
            return
        auth = request.headers.get("authorization", "")
        header_token = request.headers.get("x-panel-token", "")
        candidate = auth[7:].strip() if auth.lower().startswith("bearer ") else header_token.strip()
        if not candidate or not hmac.compare_digest(candidate, token):
            raise HTTPException(status_code=401, detail="Unauthorized: invalid API token")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        col_status = runtime.collector_manager.status()
        return {
            "status": "ok",
            "version": __version__,
            "bind_host": config.server.host,
            "bind_port": config.server.port,
            "loopback_only": is_loopback_host(config.server.host),
            "collector_source": col_status["active_source"],
            "collector_connected": col_status["connected"],
        }

    @app.get("/api/dashboard", dependencies=[Depends(verify_auth)])
    async def dashboard(
        range_name: Literal["today", "month", "total"] = Query(default="month", alias="range"),
    ) -> dict[str, Any]:
        devices = runtime.db.get_device_summary(range_name)
        protocols = runtime.db.get_protocol_summary(range_name)
        oci_quota = runtime.db.get_oci_quota_status()
        trend = runtime.db.get_traffic_trend(range_name)

        dev_up = sum(d["upload_bytes"] for d in devices)
        dev_down = sum(d["download_bytes"] for d in devices)
        proto_up = sum(p["upload_bytes"] for p in protocols)
        proto_down = sum(p["download_bytes"] for p in protocols)

        # Prefer device sum when present, fallback to protocol sum
        total_up = dev_up if (dev_up + dev_down) > 0 else proto_up
        total_down = dev_down if (dev_up + dev_down) > 0 else proto_down

        active_count = sum(1 for d in devices if d["status"] == "active")

        return {
            "range": range_name,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "timezone": config.server.timezone,
            "version": __version__,
            "server_meta": {
                "bind_host": config.server.host,
                "bind_port": config.server.port,
                "loopback_only": is_loopback_host(config.server.host),
                "ssh_tunnel_cmd": (
                    f"ssh -L {config.server.port}:127.0.0.1:{config.server.port} "
                    f"{config.server.ssh_host_hint}"
                ),
            },
            "summary": {
                "upload_bytes": total_up,
                "download_bytes": total_down,
                "total_bytes": total_up + total_down,
                "active_devices": active_count,
                "total_devices": len(devices),
            },
            "oci_quota": oci_quota,
            "devices": devices,
            "protocols": protocols,
            "trend": trend,
            "collector": runtime.collector_manager.status(),
            "singbox_health": runtime.cfg_manager.inspect_config_health(),
            "configured_protocols": [
                {
                    "key": p.key,
                    "label": p.label,
                    "protocol_type": p.protocol_type,
                    "inbound_tag": p.inbound_tag,
                    "oci_multiplier": p.oci_multiplier,
                    "color": p.color,
                    "default_flow": p.client.flow,
                }
                for p in config.protocols
            ],
        }

    @app.post("/api/collector/flush", dependencies=[Depends(verify_auth)])
    async def flush_collector() -> dict[str, Any]:
        rows = await runtime.collector_manager.flush_now()
        return {
            "flushed_rows": rows,
            "collector": runtime.collector_manager.status(),
        }

    @app.get("/api/devices", dependencies=[Depends(verify_auth)])
    async def list_devices() -> dict[str, Any]:
        devices = runtime.db.list_devices()
        sanitized = [
            {
                "name": d["name"],
                "note": d["note"],
                "status": d["status"],
                "enabled_protocols": d["enabled_protocols"],
                "has_vless": bool(d["vless_uuid"]),
                "has_hysteria2": bool(d["hy2_password"]),
                "created_at": d["created_at"],
                "revoked_at": d["revoked_at"],
            }
            for d in devices
        ]
        return {"devices": sanitized}

    @app.post("/api/devices", status_code=201, dependencies=[Depends(verify_auth)])
    async def create_device(body: CreateDeviceRequest) -> dict[str, Any]:
        # Flush in-flight traffic stats right before sing-box reload so zero bytes are lost
        await runtime.collector_manager.flush_now()
        try:
            result = runtime.cfg_manager.add_device(
                name=body.name,
                note=body.note,
                protocols=body.protocols,
                vless_uuid=body.vless_uuid,
                vless_flow=body.vless_flow,
                hy2_password=body.hy2_password,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SingBoxConfigError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        share = build_device_share_bundle(
            config=config,
            cfg_manager=runtime.cfg_manager,
            device=result["device"],
        )
        return {
            "device": {
                "name": result["device"]["name"],
                "note": result["device"]["note"],
                "status": result["device"]["status"],
                "enabled_protocols": result["device"]["enabled_protocols"],
                "created_at": result["device"]["created_at"],
            },
            "backup": result["backup"],
            "reload": result["reload"],
            "share": share,
        }

    @app.get("/api/devices/{name}/share", dependencies=[Depends(verify_auth)])
    async def get_device_share(name: str) -> dict[str, Any]:
        try:
            clean_name = validate_device_name(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        device = runtime.db.get_device(clean_name)
        if not device:
            raise HTTPException(status_code=404, detail=f"未找到设备 '{clean_name}'")
        if device["status"] != "active":
            raise HTTPException(
                status_code=409,
                detail=f"设备 '{clean_name}' 已被吊销，请先重新启用后再获取凭据二维码。",
            )
        return build_device_share_bundle(
            config=config,
            cfg_manager=runtime.cfg_manager,
            device=device,
        )

    @app.get("/api/devices/{name}/qrcode", dependencies=[Depends(verify_auth)])
    async def download_device_qrcode(
        name: str,
        protocol: str = Query(..., description="Protocol key, e.g. tokyo_vless or sg_hysteria2"),
        fmt: Literal["svg", "png"] = Query(default="svg", alias="format"),
    ) -> Response:
        try:
            clean_name = validate_device_name(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        device = runtime.db.get_device(clean_name)
        if not device or device["status"] != "active":
            raise HTTPException(status_code=404, detail="设备不存在或已吊销")

        bundle = build_device_share_bundle(
            config=config,
            cfg_manager=runtime.cfg_manager,
            device=device,
        )
        link = next((item for item in bundle["links"] if item["protocol_key"] == protocol), None)
        if not link:
            raise HTTPException(status_code=404, detail=f"该设备未启用协议 '{protocol}'")

        filename = f"{clean_name}-{protocol}.{fmt}"
        if fmt == "png":
            png_bytes = render_qr_png_bytes(link["uri"])
            return Response(
                content=png_bytes,
                media_type="image/png",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        svg_str = render_qr_svg(link["uri"])
        return Response(
            content=svg_str.encode("utf-8"),
            media_type="image/svg+xml",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/devices/{name}/revoke", dependencies=[Depends(verify_auth)])
    async def revoke_device(name: str) -> dict[str, Any]:
        await runtime.collector_manager.flush_now()
        try:
            result = runtime.cfg_manager.revoke_device(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SingBoxConfigError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "device": {
                "name": result["device"]["name"],
                "status": result["device"]["status"],
                "revoked_at": result["device"]["revoked_at"],
            },
            "removed_from": result["removed_from"],
            "backup": result["backup"],
            "reload": result["reload"],
        }

    @app.post("/api/devices/{name}/restore", dependencies=[Depends(verify_auth)])
    async def restore_device(name: str, body: RestoreDeviceRequest) -> dict[str, Any]:
        await runtime.collector_manager.flush_now()
        try:
            result = runtime.cfg_manager.restore_device(
                name,
                rotate_credentials=body.rotate_credentials,
                protocols=body.protocols,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SingBoxConfigError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        share = build_device_share_bundle(
            config=config,
            cfg_manager=runtime.cfg_manager,
            device=result["device"],
        )
        return {
            "device": {
                "name": result["device"]["name"],
                "status": result["device"]["status"],
                "enabled_protocols": result["device"]["enabled_protocols"],
            },
            "backup": result["backup"],
            "reload": result["reload"],
            "share": share,
        }

    @app.delete("/api/devices/{name}", dependencies=[Depends(verify_auth)])
    async def delete_revoked_device(name: str) -> dict[str, Any]:
        try:
            clean_name = validate_device_name(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        existing = runtime.db.get_device(clean_name)
        if not existing:
            raise HTTPException(status_code=404, detail=f"未找到设备 '{clean_name}'")
        if existing["status"] == "active":
            raise HTTPException(
                status_code=409,
                detail="请先吊销该设备凭据，再删除设备记录（历史流量统计会继续保留）。",
            )
        runtime.db.delete_device_record(clean_name)
        runtime.db.append_audit_log("device.delete_record", clean_name, "deleted revoked device entry")
        return {"deleted": clean_name}

    @app.post("/api/singbox/sync", dependencies=[Depends(verify_auth)])
    async def sync_singbox_devices() -> dict[str, Any]:
        try:
            res = runtime.cfg_manager.sync_devices_from_config()
        except SingBoxConfigError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        runtime.db.append_audit_log(
            "config.sync",
            "sing-box",
            f"synced={res.get('synced', 0)}, devices={res.get('devices', [])}",
        )
        return res

    @app.get("/api/audit", dependencies=[Depends(verify_auth)])
    async def get_audit() -> dict[str, Any]:
        return {
            "logs": runtime.db.list_audit_logs(limit=50),
            "backups": runtime.cfg_manager.list_backups(),
            "singbox_health": runtime.cfg_manager.inspect_config_health(),
        }

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app
