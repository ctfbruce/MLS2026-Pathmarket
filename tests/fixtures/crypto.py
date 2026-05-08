"""Ephemeral Ed25519 key material for tests.

Generates keypairs in-memory; never touches disk. The on-disk key format
(``keys/<isd_as>.private`` / ``.public`` raw 32-byte seeds) is covered by
``scripts/generate_keys.py`` and tested there.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from pathmarket.schemas import Signature, SignatureMeta


@dataclass(frozen=True)
class TestKey:
    """One AS's Ed25519 keypair held in memory for tests."""

    isd_as: str
    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey

    @property
    def public_bytes(self) -> bytes:
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def sign(self, payload_bytes: bytes, *, key_id: str = "default") -> Signature:
        sig_bytes = self.private_key.sign(payload_bytes)
        return Signature(
            meta=SignatureMeta(isd_as=self.isd_as, key_id=key_id, trc_serial=0, trc_base=0),
            sig_bytes=sig_bytes,
        )


def make_test_key(isd_as: str) -> TestKey:
    """Generate a fresh Ed25519 keypair for ``isd_as``."""

    sk = Ed25519PrivateKey.generate()
    return TestKey(isd_as=isd_as, private_key=sk, public_key=sk.public_key())


def keyring_from_test_keys(keys: list[TestKey]) -> dict[str, dict[str, bytes]]:
    """Build an in-memory keyring dict suitable for ``StaticKeyVerifier(keyring=...)``."""

    return {k.isd_as: {"default": k.public_bytes} for k in keys}
