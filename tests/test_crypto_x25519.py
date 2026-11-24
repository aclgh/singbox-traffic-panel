import base64

import pytest

from singbox_panel.crypto_x25519 import (
    reality_public_key_from_private,
    x25519,
    x25519_public_from_private_bytes,
)


def test_rfc7748_section_6_1_vectors() -> None:
    """Verify exact RFC 7748 X25519 Diffie-Hellman test vectors."""
    alice_priv = bytes.fromhex(
        "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"
    )
    alice_pub_expected = bytes.fromhex(
        "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    )
    bob_priv = bytes.fromhex(
        "5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb"
    )
    bob_pub_expected = bytes.fromhex(
        "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f"
    )
    shared_expected = bytes.fromhex(
        "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
    )

    alice_pub = x25519_public_from_private_bytes(alice_priv)
    bob_pub = x25519_public_from_private_bytes(bob_priv)

    assert alice_pub == alice_pub_expected
    assert bob_pub == bob_pub_expected
    assert x25519(alice_priv, bob_pub) == shared_expected
    assert x25519(bob_priv, alice_pub) == shared_expected


def test_reality_base64url_derivation() -> None:
    alice_priv = bytes.fromhex(
        "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"
    )
    alice_pub = bytes.fromhex(
        "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    )
    priv_b64 = base64.urlsafe_b64encode(alice_priv).rstrip(b"=").decode("ascii")
    expected_pub_b64 = base64.urlsafe_b64encode(alice_pub).rstrip(b"=").decode("ascii")

    derived = reality_public_key_from_private(priv_b64)
    assert derived == expected_pub_b64
    assert "=" not in derived


def test_invalid_key_length_raises() -> None:
    with pytest.raises(ValueError):
        x25519_public_from_private_bytes(b"too-short")
