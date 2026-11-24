"""Non-destructive sing-box config.json credential manager with backup, check, and rollback."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig, ProtocolConfig
from .crypto_x25519 import reality_public_key_from_private
from .db import TrafficDatabase

DEVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,47}$")


class SingBoxConfigError(RuntimeError):
    """Raised when sing-box configuration parsing, validation, or reload fails."""


def validate_device_name(name: str) -> str:
    cleaned = name.strip()
    if not DEVICE_NAME_RE.match(cleaned):
        raise ValueError(
            "设备名称仅允许字母、数字、点、下划线和短横线，须以字母或数字开头，长度 1-48 字符。"
        )
    return cleaned


class SingBoxConfigManager:
    """Safely reads and edits sing-box config.json without disturbing existing routing/TLS setup."""

    def __init__(self, config: AppConfig, db: TrafficDatabase) -> None:
        self.config = config
        self.db = db
        self._lock = threading.RLock()

    @property
    def config_path(self) -> Path:
        return Path(self.config.singbox.config_path).expanduser()

    @property
    def backup_dir(self) -> Path:
        return Path(self.config.storage.backup_dir).expanduser()

    def config_exists(self) -> bool:
        return self.config_path.is_file()

    def read_raw_config(self) -> dict[str, Any]:
        if not self.config_exists():
            raise SingBoxConfigError(f"sing-box 配置文件不存在: {self.config_path}")
        try:
            with self.config_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise SingBoxConfigError(f"sing-box 配置文件 JSON 解析失败: {exc}") from exc
        if not isinstance(data, dict):
            raise SingBoxConfigError("sing-box 配置文件根节点必须为 JSON Object")
        return data

    def find_inbound(
        self, raw_cfg: dict[str, Any], proto: ProtocolConfig
    ) -> dict[str, Any] | None:
        inbounds = raw_cfg.get("inbounds")
        if not isinstance(inbounds, list):
            return None

        # 1. Exact tag match
        if proto.inbound_tag:
            for item in inbounds:
                if isinstance(item, dict) and item.get("tag") == proto.inbound_tag:
                    return item

        # 2. Unambiguous protocol type fallback
        same_type = [
            item
            for item in inbounds
            if isinstance(item, dict) and item.get("type") == proto.protocol_type
        ]
        if len(same_type) == 1:
            return same_type[0]
        return None

    def extract_inbound_share_params(
        self, raw_cfg: dict[str, Any], proto: ProtocolConfig
    ) -> dict[str, Any]:
        """Extract live listen_port, TLS/REALITY parameters, obfs, etc. from sing-box inbound."""
        inbound = self.find_inbound(raw_cfg, proto) or {}
        tls = inbound.get("tls") if isinstance(inbound.get("tls"), dict) else {}
        reality = tls.get("reality") if isinstance(tls.get("reality"), dict) else {}
        transport = inbound.get("transport") if isinstance(inbound.get("transport"), dict) else {}
        obfs = inbound.get("obfs") if isinstance(inbound.get("obfs"), dict) else {}

        port = proto.client.server_port or inbound.get("listen_port")
        sni = proto.client.sni or tls.get("server_name")
        if not sni and isinstance(reality.get("handshake"), dict):
            sni = reality["handshake"].get("server")

        reality_enabled = bool(reality.get("enabled", False))
        public_key = proto.client.public_key.strip()
        if reality_enabled and not public_key and reality.get("private_key"):
            try:
                public_key = reality_public_key_from_private(str(reality["private_key"]))
            except Exception:
                public_key = ""

        short_id = proto.client.short_id.strip()
        if reality_enabled and not short_id:
            sids = reality.get("short_id")
            if isinstance(sids, list) and sids:
                short_id = str(sids[0])
            elif isinstance(sids, str):
                short_id = sids

        # Detect default flow from existing VLESS users if present
        detected_flow = proto.client.flow
        if proto.protocol_type == "vless" and not detected_flow:
            for u in inbound.get("users") or []:
                if isinstance(u, dict) and u.get("flow"):
                    detected_flow = str(u["flow"])
                    break

        return {
            "inbound_found": bool(inbound),
            "inbound_tag": inbound.get("tag") or proto.inbound_tag,
            "server_host": proto.client.server_host,
            "server_port": int(port) if port else 443,
            "sni": sni or proto.client.server_host,
            "tls_enabled": bool(tls.get("enabled", True)),
            "reality_enabled": reality_enabled,
            "public_key": public_key,
            "short_id": short_id,
            "fingerprint": proto.client.fingerprint or "chrome",
            "flow": detected_flow,
            "transport_type": str(transport.get("type") or "tcp"),
            "transport_path": str(transport.get("path") or ""),
            "transport_service_name": str(transport.get("service_name") or ""),
            "obfs_type": str(obfs.get("type") or ""),
            "obfs_password": str(obfs.get("password") or ""),
            "insecure": proto.client.insecure,
            "alpn": proto.client.alpn,
            "remark_prefix": proto.client.remark_prefix or proto.label,
        }

    def inspect_config_health(self) -> dict[str, Any]:
        """Return safe read-only summary of sing-box config compatibility."""
        if not self.config_exists():
            return {
                "config_path": str(self.config_path),
                "exists": False,
                "error": f"配置文件不存在: {self.config_path}",
                "v2ray_api_enabled": False,
                "clash_api_enabled": False,
                "matched_protocols": [],
                "backups_count": len(self.list_backups()),
            }
        try:
            raw = self.read_raw_config()
        except SingBoxConfigError as exc:
            return {
                "config_path": str(self.config_path),
                "exists": True,
                "error": str(exc),
                "v2ray_api_enabled": False,
                "clash_api_enabled": False,
                "matched_protocols": [],
                "backups_count": len(self.list_backups()),
            }

        exp = raw.get("experimental") if isinstance(raw.get("experimental"), dict) else {}
        v2 = exp.get("v2ray_api") if isinstance(exp.get("v2ray_api"), dict) else {}
        v2_stats = v2.get("stats") if isinstance(v2.get("stats"), dict) else {}
        cl = exp.get("clash_api") if isinstance(exp.get("clash_api"), dict) else {}

        matched = []
        for proto in self.config.protocols:
            inb = self.find_inbound(raw, proto)
            matched.append(
                {
                    "key": proto.key,
                    "label": proto.label,
                    "protocol_type": proto.protocol_type,
                    "configured_inbound_tag": proto.inbound_tag,
                    "matched_inbound_tag": inb.get("tag") if inb else None,
                    "listen_port": inb.get("listen_port") if inb else None,
                    "users_count": len(inb.get("users") or []) if inb else 0,
                }
            )

        return {
            "config_path": str(self.config_path),
            "exists": True,
            "error": None,
            "dry_run": self.config.singbox.dry_run,
            "v2ray_api_enabled": bool(v2.get("listen") and v2_stats.get("enabled")),
            "v2ray_api_listen": v2.get("listen"),
            "v2ray_api_users": list(v2_stats.get("users") or []),
            "clash_api_enabled": bool(cl.get("external_controller")),
            "clash_api_controller": cl.get("external_controller"),
            "matched_protocols": matched,
            "backups_count": len(self.list_backups()),
        }

    def sync_devices_from_config(self) -> dict[str, Any]:
        """Discover existing devices in sing-box config.json and import them into SQLite."""
        with self._lock:
            if not self.config_exists():
                return {"synced": 0, "skipped": True, "reason": "config_not_found"}

            raw = self.read_raw_config()
            discovered: dict[str, dict[str, Any]] = {}

            for proto in self.config.protocols:
                inbound = self.find_inbound(raw, proto)
                if not inbound:
                    continue
                users = inbound.get("users")
                if not isinstance(users, list):
                    continue
                for idx, user in enumerate(users):
                    if not isinstance(user, dict):
                        continue
                    if proto.protocol_type == "vless":
                        uid = str(user.get("uuid") or "").strip()
                        if not uid:
                            continue
                        name = str(user.get("name") or f"vless-{uid[:8]}").strip()
                        entry = discovered.setdefault(
                            name,
                            {
                                "vless_uuid": None,
                                "vless_flow": proto.client.flow,
                                "hy2_password": None,
                                "enabled_protocols": [],
                            },
                        )
                        entry["vless_uuid"] = uid
                        entry["vless_flow"] = str(user.get("flow") or "")
                        if proto.key not in entry["enabled_protocols"]:
                            entry["enabled_protocols"].append(proto.key)
                    elif proto.protocol_type == "hysteria2":
                        pwd = str(user.get("password") or "").strip()
                        if not pwd:
                            continue
                        name = str(user.get("name") or f"hy2-user-{idx + 1}").strip()
                        entry = discovered.setdefault(
                            name,
                            {
                                "vless_uuid": None,
                                "vless_flow": "",
                                "hy2_password": None,
                                "enabled_protocols": [],
                            },
                        )
                        entry["hy2_password"] = pwd
                        if proto.key not in entry["enabled_protocols"]:
                            entry["enabled_protocols"].append(proto.key)

            synced_names: list[str] = []
            for name, info in discovered.items():
                existing = self.db.get_device(name)
                self.db.upsert_device(
                    name=name,
                    note=existing["note"] if existing else "",
                    vless_uuid=info["vless_uuid"],
                    vless_flow=info["vless_flow"],
                    hy2_password=info["hy2_password"],
                    enabled_protocols=info["enabled_protocols"],
                    status="active",
                    revoked_at=None,
                )
                synced_names.append(name)

            return {"synced": len(synced_names), "devices": sorted(synced_names), "skipped": False}

    def list_backups(self) -> list[dict[str, Any]]:
        if not self.backup_dir.exists():
            return []
        files = sorted(
            self.backup_dir.glob("config.json.*.bak"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [
            {
                "filename": p.name,
                "size_bytes": p.stat().st_size,
                "modified_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(
                    timespec="seconds"
                ),
            }
            for p in files
        ]

    def _create_backup(self) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        backup_path = self.backup_dir / f"config.json.{stamp}.bak"
        shutil.copy2(self.config_path, backup_path)
        try:
            os.chmod(backup_path, 0o600)
        except OSError:
            pass
        # Retention pruning
        backups = sorted(
            self.backup_dir.glob("config.json.*.bak"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in backups[self.config.storage.max_backups :]:
            try:
                old.unlink()
            except OSError:
                pass
        return backup_path

    def _validate_candidate_file(self, candidate_path: Path) -> None:
        if self.config.singbox.dry_run or not self.config.singbox.validate_before_reload:
            return
        cmd = [self.config.singbox.binary_path, "check", "-c", str(candidate_path)]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SingBoxConfigError(
                f"未找到 sing-box 可执行文件 ({self.config.singbox.binary_path})，"
                "请检查 panel.yaml 中 singbox.binary_path 配置。"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SingBoxConfigError("sing-box check 校验超时（>15s）") from exc

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise SingBoxConfigError(f"sing-box check 校验未通过，已中止写入: {err}")

    def _reload_singbox(self) -> str:
        if self.config.singbox.dry_run:
            return "dry-run (skipped reload)"
        reload_cmd = self.config.singbox.reload_command.strip()
        if not reload_cmd:
            return "no reload_command configured"
        argv = shlex.split(reload_cmd)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SingBoxConfigError(f"重载命令不存在: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise SingBoxConfigError("sing-box 重载超时（>20s）") from exc

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise SingBoxConfigError(f"sing-box 重载失败 (exit={proc.returncode}): {err}")
        return (proc.stdout or "reloaded").strip() or "reloaded"

    def _apply_config_safely(self, updated_cfg: dict[str, Any]) -> tuple[Path, str]:
        """Backup -> write temp -> sing-box check -> atomic replace -> reload (with rollback)."""
        backup_path = self._create_backup()
        serialized = json.dumps(updated_cfg, indent=2, ensure_ascii=False) + "\n"

        # Preserve original file permissions if available
        orig_mode = None
        try:
            orig_mode = self.config_path.stat().st_mode & 0o777
        except OSError:
            pass

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".singbox-config-",
            suffix=".json.tmp",
            dir=str(self.config_path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(serialized)
            if orig_mode is not None:
                try:
                    os.chmod(tmp_path, orig_mode)
                except OSError:
                    pass

            self._validate_candidate_file(tmp_path)
            os.replace(tmp_path, self.config_path)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

        try:
            reload_msg = self._reload_singbox()
        except Exception:
            # Rollback live config immediately if reload failed
            shutil.copy2(backup_path, self.config_path)
            raise

        return backup_path, reload_msg

    def _sync_v2ray_stats_user(
        self, cfg: dict[str, Any], device_name: str, *, present: bool
    ) -> None:
        if not self.config.singbox.sync_v2ray_api_users:
            return
        exp = cfg.get("experimental")
        if not isinstance(exp, dict):
            return
        v2 = exp.get("v2ray_api")
        if not isinstance(v2, dict):
            return
        stats = v2.get("stats")
        if not isinstance(stats, dict) or not stats.get("enabled"):
            return
        users = stats.get("users")
        if not isinstance(users, list):
            if not present:
                return
            users = []
            stats["users"] = users
        if present:
            if device_name not in users:
                users.append(device_name)
        else:
            stats["users"] = [u for u in users if u != device_name]

    def add_device(
        self,
        *,
        name: str,
        note: str = "",
        protocols: list[str] | None = None,
        vless_uuid: str | None = None,
        vless_flow: str | None = None,
        hy2_password: str | None = None,
    ) -> dict[str, Any]:
        """Provision a new device across selected protocols, validate, and reload sing-box."""
        clean_name = validate_device_name(name)
        with self._lock:
            existing = self.db.get_device(clean_name)
            if existing and existing["status"] == "active":
                raise ValueError(f"设备 '{clean_name}' 已处于启用状态，请勿重复创建。")

            selected_keys = protocols or [p.key for p in self.config.protocols]
            target_protos = [
                p for p in self.config.protocols if p.key in selected_keys
            ]
            if not target_protos:
                raise ValueError("请至少选择一个有效协议。")

            raw_cfg = self.read_raw_config()
            updated = copy.deepcopy(raw_cfg)

            final_uuid: str | None = None
            final_flow: str = ""
            final_hy2_pwd: str | None = None
            enabled_keys: list[str] = []

            for proto in target_protos:
                inbound = self.find_inbound(updated, proto)
                if inbound is None:
                    raise SingBoxConfigError(
                        f"在 {self.config_path} 中未找到协议 {proto.label} 对应的 inbound "
                        f"(tag={proto.inbound_tag!r}, type={proto.protocol_type!r})"
                    )
                users = inbound.setdefault("users", [])
                if not isinstance(users, list):
                    raise SingBoxConfigError(f"inbound {inbound.get('tag')} 的 users 字段格式错误")

                # Remove any stale entry with same name before appending
                users[:] = [
                    u for u in users if not (isinstance(u, dict) and u.get("name") == clean_name)
                ]

                if proto.protocol_type == "vless":
                    if vless_uuid and vless_uuid.strip():
                        # Validate UUID syntax
                        final_uuid = str(uuid.UUID(vless_uuid.strip()))
                    else:
                        final_uuid = str(uuid.uuid4())
                    if vless_flow is not None:
                        final_flow = vless_flow.strip()
                    else:
                        # Inherit existing VLESS flow convention if present
                        existing_flow = next(
                            (str(u["flow"]) for u in users if isinstance(u, dict) and u.get("flow")),
                            proto.client.flow,
                        )
                        final_flow = existing_flow

                    user_obj: dict[str, Any] = {"name": clean_name, "uuid": final_uuid}
                    if final_flow:
                        user_obj["flow"] = final_flow
                    users.append(user_obj)
                    enabled_keys.append(proto.key)

                elif proto.protocol_type == "hysteria2":
                    final_hy2_pwd = (
                        hy2_password.strip()
                        if hy2_password and hy2_password.strip()
                        else secrets.token_urlsafe(24)
                    )
                    users.append({"name": clean_name, "password": final_hy2_pwd})
                    enabled_keys.append(proto.key)

            self._sync_v2ray_stats_user(updated, clean_name, present=True)

            try:
                backup_path, reload_msg = self._apply_config_safely(updated)
            except Exception as exc:
                self.db.append_audit_log("device.create", clean_name, str(exc), success=False)
                raise

            device = self.db.upsert_device(
                name=clean_name,
                note=note.strip(),
                vless_uuid=final_uuid,
                vless_flow=final_flow,
                hy2_password=final_hy2_pwd,
                enabled_protocols=enabled_keys,
                status="active",
                revoked_at=None,
            )
            self.db.append_audit_log(
                "device.create",
                clean_name,
                f"protocols={enabled_keys}, backup={backup_path.name}, reload={reload_msg}",
                success=True,
            )
            return {
                "device": device,
                "backup": backup_path.name,
                "reload": reload_msg,
            }

    def revoke_device(self, name: str) -> dict[str, Any]:
        """Revoke device credentials from sing-box config.json and reload (keeps traffic history)."""
        clean_name = validate_device_name(name)
        with self._lock:
            existing = self.db.get_device(clean_name)
            raw_cfg = self.read_raw_config()
            updated = copy.deepcopy(raw_cfg)

            removed_from: list[str] = []
            stored_uuid = existing.get("vless_uuid") if existing else None
            stored_pwd = existing.get("hy2_password") if existing else None

            for proto in self.config.protocols:
                inbound = self.find_inbound(updated, proto)
                if inbound is None or not isinstance(inbound.get("users"), list):
                    continue
                users = inbound["users"]
                before = len(users)
                new_users = []
                for u in users:
                    if not isinstance(u, dict):
                        new_users.append(u)
                        continue
                    match_name = u.get("name") == clean_name
                    match_uuid = bool(stored_uuid and u.get("uuid") == stored_uuid)
                    match_pwd = bool(stored_pwd and u.get("password") == stored_pwd)
                    if match_name or match_uuid or match_pwd:
                        continue
                    new_users.append(u)
                if len(new_users) != before:
                    inbound["users"] = new_users
                    removed_from.append(proto.key)

            self._sync_v2ray_stats_user(updated, clean_name, present=False)

            try:
                backup_path, reload_msg = self._apply_config_safely(updated)
            except Exception as exc:
                self.db.append_audit_log("device.revoke", clean_name, str(exc), success=False)
                raise

            if existing is None:
                device = self.db.upsert_device(
                    name=clean_name,
                    status="revoked",
                    revoked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                )
            else:
                device = self.db.mark_device_revoked(clean_name) or existing

            self.db.append_audit_log(
                "device.revoke",
                clean_name,
                f"removed_from={removed_from}, backup={backup_path.name}, reload={reload_msg}",
                success=True,
            )
            return {
                "device": device,
                "removed_from": removed_from,
                "backup": backup_path.name,
                "reload": reload_msg,
            }

    def restore_device(
        self,
        name: str,
        *,
        rotate_credentials: bool = True,
        protocols: list[str] | None = None,
    ) -> dict[str, Any]:
        """Re-enable a revoked device, rotating UUID/password by default for security."""
        clean_name = validate_device_name(name)
        with self._lock:
            existing = self.db.get_device(clean_name)
            if existing is None:
                raise ValueError(f"未找到设备 '{clean_name}'")
            if existing["status"] == "active":
                raise ValueError(f"设备 '{clean_name}' 当前已处于启用状态")

            target_protocols = protocols or existing["enabled_protocols"] or [
                p.key for p in self.config.protocols
            ]
            use_uuid = None if rotate_credentials else existing.get("vless_uuid")
            use_pwd = None if rotate_credentials else existing.get("hy2_password")

            result = self.add_device(
                name=clean_name,
                note=existing.get("note", ""),
                protocols=target_protocols,
                vless_uuid=use_uuid,
                vless_flow=existing.get("vless_flow") or None,
                hy2_password=use_pwd,
            )
            self.db.append_audit_log(
                "device.restore",
                clean_name,
                f"rotated={rotate_credentials}, protocols={target_protocols}",
                success=True,
            )
            return result
