"""CLI entrypoint for singbox-traffic-panel."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path

import uvicorn

from .app import create_app
from .collectors.grpc_v2ray import V2RayStatsClient
from .config import AppConfig, load_config
from .singbox_config import SingBoxConfigManager


def _repo_root() -> Path:
    # src/singbox_panel/__main__.py -> repo root
    return Path(__file__).resolve().parents[2]


def _prepare_demo_config(host: str | None, port: int | None) -> AppConfig:
    cfg = load_config(None)
    demo_dir = Path("./data/demo").resolve()
    demo_dir.mkdir(parents=True, exist_ok=True)

    demo_sb_target = demo_dir / "singbox-demo.json"
    template_sb = _repo_root() / "config" / "singbox-demo.json"
    if not demo_sb_target.exists():
        if template_sb.exists():
            shutil.copy2(template_sb, demo_sb_target)
        else:
            demo_sb_target.write_text(
                json.dumps(
                    {
                        "inbounds": [
                            {
                                "type": "vless",
                                "tag": "vless-tokyo-in",
                                "listen": "::",
                                "listen_port": 443,
                                "users": [
                                    {"name": "owrt-router", "uuid": "11111111-1111-4111-8111-111111111111", "flow": "xtls-rprx-vision"},
                                    {"name": "mac", "uuid": "22222222-2222-4222-8222-222222222222", "flow": "xtls-rprx-vision"},
                                    {"name": "mobile", "uuid": "33333333-3333-4333-8333-333333333333", "flow": "xtls-rprx-vision"},
                                ],
                                "tls": {
                                    "enabled": True,
                                    "server_name": "www.microsoft.com",
                                    "reality": {
                                        "enabled": True,
                                        "handshake": {"server": "www.microsoft.com", "server_port": 443},
                                        "private_key": "cO3_w-8X8xL-9kV8m5hY7s9L5nM2pQ4rS6tU8vW0x1A",
                                        "short_id": ["a1b2c3d4"],
                                    },
                                },
                            },
                            {
                                "type": "hysteria2",
                                "tag": "hy2-sg-in",
                                "listen": "::",
                                "listen_port": 8443,
                                "users": [
                                    {"name": "owrt-router", "password": "demo-owrt-hy2-password"},
                                    {"name": "mac", "password": "demo-mac-hy2-password"},
                                    {"name": "mobile", "password": "demo-mobile-hy2-password"},
                                ],
                                "tls": {"enabled": True, "server_name": "sg.example.com"},
                            },
                        ],
                        "experimental": {
                            "v2ray_api": {
                                "listen": "127.0.0.1:10085",
                                "stats": {
                                    "enabled": True,
                                    "inbounds": ["vless-tokyo-in", "hy2-sg-in"],
                                    "users": ["owrt-router", "mac", "mobile"],
                                },
                            }
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    cfg.collector.source = "mock"
    cfg.collector.flush_interval_seconds = 10
    cfg.storage.db_path = str(demo_dir / "demo-traffic.db")
    cfg.storage.backup_dir = str(demo_dir / "backups")
    cfg.singbox.config_path = str(demo_sb_target)
    cfg.singbox.dry_run = True
    cfg.singbox.validate_before_reload = False
    if host:
        cfg.server.host = host
    if port:
        cfg.server.port = port
    cfg.server.validate_bind()
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="singbox-traffic-panel",
        description="Lightweight loopback-only traffic monitor & credential panel for pure sing-box.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help="Path to panel.yaml configuration file (or set SINGBOX_PANEL_CONFIG).",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run with realistic simulated Singapore VPS traffic & isolated demo sing-box config.",
    )
    parser.add_argument("--host", default=None, help="Override bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=None, help="Override bind port (default: 8088).")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Inspect panel.yaml and sing-box config.json compatibility, then exit.",
    )
    parser.add_argument(
        "--probe-v2ray-api",
        action="store_true",
        help="Non-destructively query sing-box v2ray_api (reset=false) and print all counters, then exit.",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Logging verbosity.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        if args.demo:
            cfg = _prepare_demo_config(args.host, args.port)
        else:
            cfg = load_config(args.config)
            if args.host:
                cfg.server.host = args.host
            if args.port:
                cfg.server.port = args.port
            cfg.server.validate_bind()
    except Exception as exc:
        print(f"[ERROR] Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.check_config:
        mgr = SingBoxConfigManager(cfg, db=None)
        health = mgr.inspect_config_health()
        print(json.dumps(health, indent=2, ensure_ascii=False))
        return 0 if health.get("exists") and not health.get("error") else 1

    if args.probe_v2ray_api:
        client = V2RayStatsClient(
            address=cfg.collector.v2ray_api.address,
            timeout=cfg.collector.v2ray_api.timeout_seconds,
        )
        try:
            stats = asyncio.run(client.query_stats(pattern="", reset=False))
        except Exception as exc:
            print(
                f"[ERROR] v2ray_api 探测失败 ({cfg.collector.v2ray_api.address}): {exc}",
                file=sys.stderr,
            )
            return 1
        print(
            json.dumps(
                {
                    "address": cfg.collector.v2ray_api.address,
                    "count": len(stats),
                    "stats": [{"name": s.name, "value": s.value} for s in stats],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    print(
        f"sing-box Traffic Panel listening on http://{cfg.server.host}:{cfg.server.port} "
        f"(collector={cfg.collector.source}, interval={cfg.collector.flush_interval_seconds}s)"
    )
    if args.demo:
        print(f"DEMO mode active: using isolated config at {cfg.singbox.config_path}")

    app = create_app(cfg)
    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=args.log_level,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
