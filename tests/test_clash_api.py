import asyncio
import json

import pytest
import websockets

from singbox_panel.collectors.clash_api import ClashApiCollector
from singbox_panel.config import AppConfig, default_protocols


@pytest.mark.asyncio
async def test_clash_api_incremental_deltas_and_eviction() -> None:
    cfg = AppConfig(protocols=default_protocols())
    col = ClashApiCollector(cfg)
    col._ws_connected = True

    # Snapshot 1: two connections open
    col.process_snapshot(
        {
            "connections": [
                {
                    "id": "conn-1",
                    "metadata": {
                        "type": "vless/vless-tokyo-in",
                        "user": "owrt-router",
                    },
                    "upload": 1000,
                    "download": 5000,
                    "chains": ["tokyo-vless-out"],
                },
                {
                    "id": "conn-2",
                    "metadata": {
                        "type": "hysteria2/hy2-sg-in",
                        "user": "mac",
                    },
                    "upload": 500,
                    "download": 2000,
                    "chains": ["direct"],
                },
            ]
        }
    )

    # Snapshot 2: conn-1 transfers more, conn-2 closed (absent)
    col.process_snapshot(
        {
            "connections": [
                {
                    "id": "conn-1",
                    "metadata": {
                        "type": "vless/vless-tokyo-in",
                        "user": "owrt-router",
                    },
                    "upload": 1600,
                    "download": 9000,
                    "chains": ["tokyo-vless-out"],
                }
            ]
        }
    )

    assert len(col._active_conns) == 1  # conn-2 evicted

    dev_deltas, proto_deltas = await col.collect_deltas()
    dev_map = {d.device_name: d for d in dev_deltas}
    proto_map = {p.protocol_key: p for p in proto_deltas}

    # owrt-router total = initial (1000, 5000) + delta (600, 4000) = (1600, 9000), NOT double-counted!
    assert dev_map["owrt-router"].upload_bytes == 1600
    assert dev_map["owrt-router"].download_bytes == 9000
    assert dev_map["mac"].upload_bytes == 500
    assert dev_map["mac"].download_bytes == 2000

    assert proto_map["tokyo_vless"].download_bytes == 9000
    assert proto_map["tokyo_vless"].multiplier == 2.0
    assert proto_map["sg_hysteria2"].download_bytes == 2000
    assert proto_map["sg_hysteria2"].multiplier == 1.0

    # Second drain without new traffic yields empty lists
    dev2, proto2 = await col.collect_deltas()
    assert dev2 == []
    assert proto2 == []


@pytest.mark.asyncio
async def test_clash_api_missing_user_diagnostic_hint_all_and_partial() -> None:
    cfg = AppConfig(protocols=default_protocols())
    col = ClashApiCollector(cfg)
    col._ws_connected = True

    # Case 1: ALL observed connections lack metadata.user (like upstream official sing-box clash_api)
    col.process_snapshot(
        {
            "connections": [
                {
                    "id": "conn-legacy-1",
                    "metadata": {"type": "hysteria2/hy2-sg-in"},
                    "upload": 100,
                    "download": 900,
                    "chains": ["direct"],
                },
                {
                    "id": "conn-legacy-2",
                    "metadata": {"type": "vless/vless-tokyo-in"},
                    "upload": 200,
                    "download": 1800,
                    "chains": ["tokyo-vless-out"],
                },
            ]
        }
    )
    devs, _ = await col.collect_deltas()
    assert all(d.device_name == "unknown" for d in devs)
    diag_all = col.diagnostics()
    assert diag_all["observed_connections"] == 2
    assert diag_all["missing_user_samples"] == 2
    assert "2/2 条连接全部缺失" in diag_all["hint"]
    assert "with_v2ray_api" in diag_all["hint"]
    assert "建议升级" not in diag_all["hint"]

    # Case 2: Subsequent connection DOES carry metadata.user -> now partial (2/3)
    col.process_snapshot(
        {
            "connections": [
                {
                    "id": "conn-with-user",
                    "metadata": {"type": "vless/vless-tokyo-in", "user": "mac"},
                    "upload": 50,
                    "download": 500,
                    "chains": ["tokyo-vless-out"],
                }
            ]
        }
    )
    diag_partial = col.diagnostics()
    assert diag_partial["observed_connections"] == 3
    assert diag_partial["missing_user_samples"] == 2
    assert "2/3 条连接缺少 metadata.user" in diag_partial["hint"]


@pytest.mark.asyncio
async def test_clash_api_live_websocket_stream_with_bearer_secret() -> None:
    seen_auth: list[str] = []

    async def ws_handler(websocket) -> None:
        auth = websocket.request.headers.get("Authorization", "")
        seen_auth.append(auth)
        await websocket.send(
            json.dumps(
                {
                    "connections": [
                        {
                            "id": "ws-c1",
                            "metadata": {"type": "vless/vless-tokyo-in", "user": "mobile"},
                            "upload": 2048,
                            "download": 8192,
                            "chains": ["tokyo-vless-out"],
                        }
                    ]
                }
            )
        )
        await asyncio.sleep(0.5)

    async with websockets.serve(ws_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = AppConfig(protocols=default_protocols())
        cfg.collector.clash_api.ws_url = f"ws://127.0.0.1:{port}/connections"
        cfg.collector.clash_api.secret = "top-secret-token"
        col = ClashApiCollector(cfg)

        await col.start()
        try:
            for _ in range(30):
                if col._messages_received >= 1:
                    break
                await asyncio.sleep(0.02)
            devs, protos = await col.collect_deltas()
        finally:
            await col.stop()

    assert seen_auth == ["Bearer top-secret-token"]
    assert len(devs) == 1
    assert devs[0].device_name == "mobile"
    assert devs[0].download_bytes == 8192
    assert protos[0].protocol_key == "tokyo_vless"
