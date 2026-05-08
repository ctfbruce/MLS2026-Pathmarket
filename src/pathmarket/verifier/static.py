"""Static-keyring Ed25519 verifier.

Authoritative source: ``DESIGN.md`` §4 (signatures), §15 (defends against
forgery and substitution), §10.3 (key material layout).

The static keyring is a JSON file mapping ISD-AS identifiers to a dict of
``key_id -> base64(public_key_bytes)``. For v2 every AS uses a single key id,
``"default"``, and the raw 32-byte Ed25519 public key.

Example keyring entry::

    {
      "1-ff00:0:110": {"default": "base64(32-byte raw pubkey)"}
    }
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from pathmarket.schemas import Signature


class StaticKeyVerifier:
    """``Verifier`` implementation backed by an in-memory keyring.

    Typical construction:

    - from a JSON file on disk: ``StaticKeyVerifier.from_file("keyring.json")``
    - from an in-memory mapping: ``StaticKeyVerifier(keyring={...})``
    """

    def __init__(self, keyring: dict[str, dict[str, bytes]]) -> None:
        """``keyring`` maps ``isd_as -> key_id -> raw 32-byte pubkey bytes``."""

        # Cache decoded public-key objects to avoid re-parsing on every verify.
        self._keys: dict[tuple[str, str], Ed25519PublicKey] = {}
        for isd_as, by_key in keyring.items():
            for key_id, pubkey_bytes in by_key.items():
                self._keys[(isd_as, key_id)] = Ed25519PublicKey.from_public_bytes(pubkey_bytes)

    @classmethod
    def from_file(cls, path: str | Path) -> "StaticKeyVerifier":
        """Load a keyring from ``path``. JSON shape per module docstring."""

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        decoded: dict[str, dict[str, bytes]] = {}
        for isd_as, by_key in data.items():
            decoded[isd_as] = {
                key_id: base64.b64decode(pubkey_b64) for key_id, pubkey_b64 in by_key.items()
            }
        return cls(decoded)

    def verify(self, *, signature: Signature, payload_bytes: bytes) -> bool:
        """Return True iff ``signature`` verifies over ``payload_bytes``.

        Returns False on unknown (isd_as, key_id), wrong-key-for-AS, tampered
        bytes, or any other verification failure — never raises.
        """

        key = self._keys.get((signature.meta.isd_as, signature.meta.key_id))
        if key is None:
            return False
        try:
            key.verify(signature.sig_bytes, payload_bytes)
        except InvalidSignature:
            return False
        return True

    def known_ases(self) -> list[str]:
        """Return every ISD-AS with at least one key in the keyring."""

        return sorted({isd_as for (isd_as, _) in self._keys.keys()})


__all__ = ["StaticKeyVerifier"]
