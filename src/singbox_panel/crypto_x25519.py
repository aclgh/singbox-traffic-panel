"""Pure-Python RFC 7748 X25519 basepoint derivation for sing-box REALITY keys.

Allows deriving the client-side `pbk` (public key) directly from sing-box's
server-side `tls.reality.private_key` without requiring C cryptography libraries.
"""

from __future__ import annotations

import base64

_P = (1 << 255) - 19
_A24 = 121665
_BASEPOINT = 9


def _decode_scalar(k: bytes) -> int:
    if len(k) != 32:
        raise ValueError(f"X25519 key must be 32 bytes, got {len(k)}")
    clamped = bytearray(k)
    clamped[0] &= 248
    clamped[31] &= 127
    clamped[31] |= 64
    return int.from_bytes(clamped, "little")


def _decode_u_coordinate(u: bytes) -> int:
    if len(u) != 32:
        raise ValueError(f"X25519 u-coordinate must be 32 bytes, got {len(u)}")
    buf = bytearray(u)
    buf[31] &= 127
    return int.from_bytes(buf, "little")


def x25519(scalar_bytes: bytes, u_bytes: bytes) -> bytes:
    """Compute X25519(k, u) per RFC 7748 Section 5."""
    k = _decode_scalar(scalar_bytes)
    x_1 = _decode_u_coordinate(u_bytes)
    x_2 = 1
    z_2 = 0
    x_3 = x_1
    z_3 = 1
    swap = 0

    for t in range(254, -1, -1):
        k_t = (k >> t) & 1
        swap ^= k_t
        if swap:
            x_2, x_3 = x_3, x_2
            z_2, z_3 = z_3, z_2
        swap = k_t

        a = (x_2 + z_2) % _P
        aa = (a * a) % _P
        b = (x_2 - z_2) % _P
        bb = (b * b) % _P
        e = (aa - bb) % _P
        c = (x_3 + z_3) % _P
        d = (x_3 - z_3) % _P
        da = (d * a) % _P
        cb = (c * b) % _P
        x_3 = pow(da + cb, 2, _P)
        z_3 = (x_1 * pow(da - cb, 2, _P)) % _P
        x_2 = (aa * bb) % _P
        z_2 = (e * (aa + _A24 * e)) % _P

    if swap:
        x_2, x_3 = x_3, x_2
        z_2, z_3 = z_3, z_2

    result = (x_2 * pow(z_2, _P - 2, _P)) % _P
    return result.to_bytes(32, "little")


def x25519_public_from_private_bytes(private_key: bytes) -> bytes:
    """Derive 32-byte X25519 public key from 32-byte private key."""
    basepoint = _BASEPOINT.to_bytes(32, "little")
    return x25519(private_key, basepoint)


def reality_public_key_from_private(private_key_b64url: str) -> str:
    """Derive sing-box REALITY base64url public key from base64url private key."""
    cleaned = private_key_b64url.strip()
    padded = cleaned + "=" * (-len(cleaned) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    pub = x25519_public_from_private_bytes(raw)
    return base64.urlsafe_b64encode(pub).rstrip(b"=").decode("ascii")
