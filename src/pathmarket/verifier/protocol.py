"""Verifier protocol — the abstraction every signature check goes through.

Authoritative source: ``DESIGN.md`` §4 (signatures), §5.2 (aggregator uses it),
§15 (security posture — this is where Ed25519 verification lives).

The protocol is intentionally tiny: one method to verify a signature over
canonical-JSON bytes, one method to enumerate the keyring. The aggregator,
the agent, and test fixtures all program against this interface so that
future implementations (e.g. a TRC-rooted verifier in v3) drop in without
touching any caller.
"""

from __future__ import annotations

from typing import Protocol

from pathmarket.schemas import Signature


class Verifier(Protocol):
    """Abstract signature verifier.

    Implementations:
    - ``StaticKeyVerifier`` (this package): loads an Ed25519 public key per
      ISD-AS from ``keyring.json``; verifies under that key.
    - (future) TRC-rooted verifier.
    """

    def verify(self, *, signature: Signature, payload_bytes: bytes) -> bool:
        """Return ``True`` iff ``signature`` verifies over ``payload_bytes``.

        ``payload_bytes`` is the canonical-JSON serialization of the signed
        payload (SLA / Claim / Complaint), per §4.5.

        Implementations must return ``False`` — not raise — on:
        - unknown ``signature.meta.isd_as`` (no key for that AS);
        - unknown ``signature.meta.key_id`` (no such key id for that AS);
        - any cryptographic verification failure (tampered bytes, wrong key).
        """
        ...

    def known_ases(self) -> list[str]:
        """Return the list of ISD-AS identifiers this verifier has keys for.

        Used for diagnostic output and for the aggregator's pre-validation
        check that all of an SLA's cosigners are known.
        """
        ...
