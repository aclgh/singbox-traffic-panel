import asyncio
import struct

import h2.config
import h2.connection
import h2.events
import pytest

from singbox_panel.collectors.grpc_v2ray import (
    GrpcError,
    QUERY_STATS_PATH,
    V2RayStat,
    V2RayStatsClient,
    _encode_varint,
    decode_query_stats_response,
    encode_query_stats_request,
    frame_grpc_message,
)
from singbox_panel.collectors.v2ray_api import V2RayApiCollector
from singbox_panel.config import AppConfig, default_protocols


def _encode_stat(name: str, value: int) -> bytes:
    out = bytearray()
    name_b = name.encode("utf-8")
    out.append((1 << 3) | 2)
    out.extend(_encode_varint(len(name_b)))
    out.extend(name_b)
    if value:
        out.append((2 << 3) | 0)
        out.extend(_encode_varint(value))
    return bytes(out)


def _encode_query_stats_response(stats: list[tuple[str, int]]) -> bytes:
    out = bytearray()
    for name, val in stats:
        msg = _encode_stat(name, val)
        out.append((1 << 3) | 2)
        out.extend(_encode_varint(len(msg)))
        out.extend(msg)
    return bytes(out)


def test_protobuf_request_and_response_codec() -> None:
    req = encode_query_stats_request(pattern="user>>>", reset=True)
    assert b"user>>>" in req
    assert req.endswith(b"\x10\x01")

    large_bytes = 5 * (1000**4) + 12345  # > 5 TB (exceeds 32-bit int)
    raw = _encode_query_stats_response(
        [
            ("user>>>owrt-router>>>traffic>>>downlink", large_bytes),
            ("user>>>mac>>>traffic>>>uplink", 42_000),
            ("inbound>>>vless-tokyo-in>>>traffic>>>downlink", 999_000_000),
        ]
    )
    decoded = decode_query_stats_response(raw)
    assert decoded == [
        V2RayStat("user>>>owrt-router>>>traffic>>>downlink", large_bytes),
        V2RayStat("user>>>mac>>>traffic>>>uplink", 42_000),
        V2RayStat("inbound>>>vless-tokyo-in>>>traffic>>>downlink", 999_000_000),
    ]


@pytest.mark.asyncio
async def test_h2c_grpc_client_roundtrip() -> None:
    received_paths: list[str] = []
    received_bodies: list[bytes] = []

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        cfg = h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
        conn = h2.connection.H2Connection(config=cfg)
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        await writer.drain()

        req_body = bytearray()
        stream_id = None
        while True:
            data = await reader.read(65536)
            if not data:
                break
            events = conn.receive_data(data)
            for ev in events:
                if isinstance(ev, h2.events.RequestReceived):
                    stream_id = ev.stream_id
                    headers = dict(ev.headers)
                    received_paths.append(headers[":path"])
                elif isinstance(ev, h2.events.DataReceived):
                    req_body.extend(ev.data)
                    conn.acknowledge_received_data(ev.flow_controlled_length, ev.stream_id)
                elif isinstance(ev, h2.events.StreamEnded) and stream_id is not None:
                    received_bodies.append(bytes(req_body))
                    resp_proto = _encode_query_stats_response(
                        [
                            ("user>>>owrt-router>>>traffic>>>uplink", 10_000),
                            ("user>>>owrt-router>>>traffic>>>downlink", 250_000),
                            ("inbound>>>vless-tokyo-in>>>traffic>>>downlink", 200_000),
                            ("inbound>>>hy2-sg-in>>>traffic>>>downlink", 50_000),
                        ]
                    )
                    conn.send_headers(
                        stream_id,
                        [(":status", "200"), ("content-type", "application/grpc")],
                    )
                    conn.send_data(stream_id, frame_grpc_message(resp_proto), end_stream=False)
                    conn.send_headers(stream_id, [("grpc-status", "0")], end_stream=True)
                    writer.write(conn.data_to_send())
                    await writer.drain()
                    writer.close()
                    return
            to_send = conn.data_to_send()
            if to_send:
                writer.write(to_send)
                await writer.drain()

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        client = V2RayStatsClient(f"127.0.0.1:{port}", timeout=3.0)
        stats = await client.query_stats(pattern="", reset=True)

    assert received_paths == [QUERY_STATS_PATH]
    # Request body has 5-byte gRPC frame + reset=true (0x10 0x01)
    assert received_bodies[0][:5] == b"\x00" + struct.pack(">I", 2)
    assert received_bodies[0][5:] == b"\x10\x01"
    assert len(stats) == 4
    assert stats[0] == V2RayStat("user>>>owrt-router>>>traffic>>>uplink", 10_000)


@pytest.mark.asyncio
async def test_h2c_grpc_error_status() -> None:
    async def handle_err(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        cfg = h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
        conn = h2.connection.H2Connection(config=cfg)
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        await writer.drain()
        while True:
            data = await reader.read(65536)
            if not data:
                break
            for ev in conn.receive_data(data):
                if isinstance(ev, h2.events.StreamEnded):
                    conn.send_headers(
                        ev.stream_id,
                        [
                            (":status", "200"),
                            ("content-type", "application/grpc"),
                            ("grpc-status", "12"),
                            ("grpc-message", "unimplemented%20service"),
                        ],
                        end_stream=True,
                    )
                    writer.write(conn.data_to_send())
                    await writer.drain()
                    writer.close()
                    return

    server = await asyncio.start_server(handle_err, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        client = V2RayStatsClient(f"127.0.0.1:{port}", timeout=3.0)
        with pytest.raises(GrpcError) as exc_info:
            await client.query_stats()
    assert exc_info.value.status == 12
    assert "unimplemented service" in exc_info.value.grpc_message


def test_v2ray_collector_delta_parsing_and_restart_wrap() -> None:
    cfg = AppConfig(protocols=default_protocols())
    cfg.collector.v2ray_api.reset_on_poll = False
    col = V2RayApiCollector(cfg)

    # First poll establishes baseline (no historical spike attributed)
    devs1, protos1 = col.parse_stats_to_deltas(
        [
            V2RayStat("user>>>mac>>>traffic>>>uplink", 1000),
            V2RayStat("user>>>mac>>>traffic>>>downlink", 5000),
            V2RayStat("inbound>>>vless-tokyo-in>>>traffic>>>downlink", 5000),
        ]
    )
    assert devs1 == []
    assert protos1 == []

    # Second poll reports exact increment
    devs2, protos2 = col.parse_stats_to_deltas(
        [
            V2RayStat("user>>>mac>>>traffic>>>uplink", 1400),
            V2RayStat("user>>>mac>>>traffic>>>downlink", 7500),
            V2RayStat("inbound>>>vless-tokyo-in>>>traffic>>>downlink", 7500),
        ]
    )
    assert len(devs2) == 1
    assert devs2[0].device_name == "mac"
    assert devs2[0].upload_bytes == 400
    assert devs2[0].download_bytes == 2500
    assert len(protos2) == 1
    assert protos2[0].protocol_key == "tokyo_vless"
    assert protos2[0].multiplier == 2.0
    assert protos2[0].download_bytes == 2500

    # Third poll simulates sing-box restart (counter drops below previous)
    devs3, _ = col.parse_stats_to_deltas(
        [
            V2RayStat("user>>>mac>>>traffic>>>uplink", 200),
            V2RayStat("user>>>mac>>>traffic>>>downlink", 800),
        ]
    )
    assert devs3[0].upload_bytes == 200
    assert devs3[0].download_bytes == 800
