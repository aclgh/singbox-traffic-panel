"""Pure-Python protobuf wire codec & h2c gRPC client for sing-box v2ray_api StatsService.

Targets `v2ray.core.app.stats.command.StatsService/QueryStats` over plaintext HTTP/2 (h2c)
on loopback (e.g., 127.0.0.1:10085) without requiring native C++ grpcio extensions.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from urllib.parse import unquote

import h2.config
import h2.connection
import h2.events


QUERY_STATS_PATH = "/v2ray.core.app.stats.command.StatsService/QueryStats"
GET_STATS_PATH = "/v2ray.core.app.stats.command.StatsService/GetStats"


@dataclass(frozen=True)
class V2RayStat:
    name: str
    value: int


class GrpcError(RuntimeError):
    """Raised when a gRPC call returns a non-zero status or malformed frame."""

    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        self.grpc_message = message
        super().__init__(f"gRPC error (status={status}): {message or 'unknown error'}")


# --- Minimal exact Protobuf v3 wire codec ---


def _encode_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def _decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    length = len(buf)
    while pos < length:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift >= 70:
            raise ValueError("Protobuf varint overflow")
    raise ValueError("Truncated protobuf varint")


def _to_signed_int64(val: int) -> int:
    if val >= (1 << 63):
        return val - (1 << 64)
    return val


def _skip_field(buf: bytes, pos: int, wire_type: int) -> int:
    if wire_type == 0:  # varint
        _, pos = _decode_varint(buf, pos)
        return pos
    if wire_type == 1:  # 64-bit
        if pos + 8 > len(buf):
            raise ValueError("Truncated 64-bit protobuf field")
        return pos + 8
    if wire_type == 2:  # length-delimited
        size, pos = _decode_varint(buf, pos)
        if pos + size > len(buf):
            raise ValueError("Truncated length-delimited protobuf field")
        return pos + size
    if wire_type == 5:  # 32-bit
        if pos + 4 > len(buf):
            raise ValueError("Truncated 32-bit protobuf field")
        return pos + 4
    raise ValueError(f"Unsupported protobuf wire type: {wire_type}")


def encode_query_stats_request(
    pattern: str = "",
    reset: bool = False,
    patterns: list[str] | None = None,
    regexp: bool = False,
) -> bytes:
    """Encode v2ray.core.app.stats.command.QueryStatsRequest."""
    out = bytearray()
    if pattern:
        encoded = pattern.encode("utf-8")
        out.append((1 << 3) | 2)  # field 1, length-delimited
        out.extend(_encode_varint(len(encoded)))
        out.extend(encoded)
    if reset:
        out.append((2 << 3) | 0)  # field 2, varint (bool)
        out.append(1)
    for item in patterns or []:
        encoded = item.encode("utf-8")
        out.append((3 << 3) | 2)  # field 3, repeated string patterns
        out.extend(_encode_varint(len(encoded)))
        out.extend(encoded)
    if regexp:
        out.append((4 << 3) | 0)  # field 4, varint (bool regexp)
        out.append(1)
    return bytes(out)


def decode_stat_message(buf: bytes) -> V2RayStat:
    """Decode v2ray.core.app.stats.command.Stat { string name = 1; int64 value = 2; }."""
    pos = 0
    length = len(buf)
    name = ""
    value = 0
    while pos < length:
        tag, pos = _decode_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 1 and wire_type == 2:
            size, pos = _decode_varint(buf, pos)
            if pos + size > length:
                raise ValueError("Truncated Stat.name")
            name = buf[pos : pos + size].decode("utf-8", errors="replace")
            pos += size
        elif field_num == 2 and wire_type == 0:
            raw_val, pos = _decode_varint(buf, pos)
            value = _to_signed_int64(raw_val)
        else:
            pos = _skip_field(buf, pos, wire_type)
    return V2RayStat(name=name, value=value)


def decode_query_stats_response(buf: bytes) -> list[V2RayStat]:
    """Decode v2ray.core.app.stats.command.QueryStatsResponse { repeated Stat stat = 1; }."""
    pos = 0
    length = len(buf)
    stats: list[V2RayStat] = []
    while pos < length:
        tag, pos = _decode_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 1 and wire_type == 2:
            size, pos = _decode_varint(buf, pos)
            if pos + size > length:
                raise ValueError("Truncated QueryStatsResponse.stat")
            stats.append(decode_stat_message(buf[pos : pos + size]))
            pos += size
        else:
            pos = _skip_field(buf, pos, wire_type)
    return stats


def frame_grpc_message(payload: bytes) -> bytes:
    """Wrap protobuf bytes in a 5-byte uncompressed gRPC length-prefixed frame."""
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def unframe_grpc_message(data: bytes) -> bytes:
    """Unwrap a single uncompressed gRPC length-prefixed frame."""
    if not data:
        return b""
    if len(data) < 5:
        raise GrpcError(-1, f"Truncated gRPC frame header ({len(data)} bytes)")
    compressed = data[0]
    if compressed != 0:
        raise GrpcError(-1, f"Unsupported gRPC compression flag: {compressed}")
    msg_len = struct.unpack(">I", data[1:5])[0]
    if len(data) < 5 + msg_len:
        raise GrpcError(-1, f"Truncated gRPC frame body (expected {msg_len}, got {len(data) - 5})")
    return data[5 : 5 + msg_len]


def parse_host_port(address: str, default_port: int = 10085) -> tuple[str, int]:
    addr = address.strip()
    if addr.startswith("["):
        host_part, _, rest = addr[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host_part, port
    if ":" in addr:
        host_part, port_part = addr.rsplit(":", 1)
        return host_part or "127.0.0.1", int(port_part)
    return addr or "127.0.0.1", default_port


class V2RayStatsClient:
    """Async unary h2c gRPC client for sing-box experimental.v2ray_api."""

    def __init__(self, address: str = "127.0.0.1:10085", timeout: float = 5.0) -> None:
        self.address = address
        self.host, self.port = parse_host_port(address)
        self.timeout = timeout

    async def query_stats(self, pattern: str = "", reset: bool = False) -> list[V2RayStat]:
        payload = encode_query_stats_request(pattern=pattern, reset=reset)
        response_bytes = await asyncio.wait_for(
            self._unary_call(QUERY_STATS_PATH, payload),
            timeout=self.timeout,
        )
        return decode_query_stats_response(response_bytes)

    async def probe(self) -> bool:
        """Non-destructive probe (reset=False) to verify v2ray_api StatsService is reachable."""
        try:
            await self.query_stats(pattern="", reset=False)
            return True
        except Exception:
            return False

    async def _unary_call(self, path: str, request_payload: bytes) -> bytes:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            cfg = h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
            conn = h2.connection.H2Connection(config=cfg)
            conn.initiate_connection()

            stream_id = conn.get_next_available_stream_id()
            authority = f"{self.host}:{self.port}"
            headers = [
                (":method", "POST"),
                (":scheme", "http"),
                (":authority", authority),
                (":path", path),
                ("content-type", "application/grpc"),
                ("te", "trailers"),
                ("user-agent", "singbox-traffic-panel/0.1"),
            ]
            conn.send_headers(stream_id, headers)
            framed = frame_grpc_message(request_payload)
            conn.send_data(stream_id, framed, end_stream=True)

            writer.write(conn.data_to_send())
            await writer.drain()

            response_headers: dict[str, str] = {}
            response_trailers: dict[str, str] = {}
            response_body = bytearray()
            stream_ended = False

            while not stream_ended:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                events = conn.receive_data(chunk)
                for event in events:
                    if isinstance(event, h2.events.ResponseReceived):
                        for k, v in event.headers:
                            response_headers[k.lower()] = v
                    elif isinstance(event, h2.events.TrailersReceived):
                        for k, v in event.headers:
                            response_trailers[k.lower()] = v
                    elif isinstance(event, h2.events.DataReceived):
                        if event.stream_id == stream_id:
                            response_body.extend(event.data)
                        if event.flow_controlled_length > 0:
                            conn.acknowledge_received_data(
                                event.flow_controlled_length, event.stream_id
                            )
                    elif isinstance(event, h2.events.StreamEnded):
                        if event.stream_id == stream_id:
                            stream_ended = True
                    elif isinstance(event, h2.events.StreamReset):
                        if event.stream_id == stream_id:
                            raise GrpcError(-1, f"HTTP/2 stream reset: {event.error_code}")
                    elif isinstance(event, h2.events.ConnectionTerminated):
                        stream_ended = True

                to_send = conn.data_to_send()
                if to_send:
                    writer.write(to_send)
                    await writer.drain()

            http_status = response_headers.get(":status")
            if http_status and http_status != "200":
                raise GrpcError(-1, f"Unexpected HTTP status {http_status}")

            grpc_status_str = response_trailers.get(
                "grpc-status", response_headers.get("grpc-status", "")
            )
            grpc_msg = unquote(
                response_trailers.get("grpc-message", response_headers.get("grpc-message", ""))
            )
            if grpc_status_str != "":
                status_code = int(grpc_status_str)
                if status_code != 0:
                    raise GrpcError(status_code, grpc_msg)
            elif not stream_ended:
                raise GrpcError(-1, "Connection closed before gRPC response completed")

            return unframe_grpc_message(bytes(response_body))
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
