"""Canonical JSON serialization and content-hash ID computation.

Authoritative source: ``DESIGN.md`` §4.5 and §4.6. The procedures here are the
signing substrate — any change here is a cryptographic change.

Key decisions (§4.5):
- Sort keys, compact separators, ASCII-only.
- Bytes fields (only ``Signature.sig_bytes``) never appear in the signed payload.
  The signed payload is the ``*Payload`` dataclass — there is no ``signatures``
  field to hash.
- Prices and other potentially-non-integer decimals are strings, not JSON floats.
- Timestamps are ISO-8601 UTC with trailing ``Z``; no fractional seconds.

ID computation (§4.6) uses the "hash-with-empty-self" pattern: set the id field
to ``""``, compute ``sha256(canonical_json(asdict(p)))``, prefix ``sha256:``,
then write that value back into the id field before signing. The aggregator
re-runs the same computation on ingestion to verify.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, timezone

from pathmarket.schemas import ClaimPayload, ComplaintPayload, SLAPayload

_ID_PREFIX = "sha256:"


def canonical_json(obj: object) -> bytes:
    """Serialize ``obj`` to canonical JSON bytes.

    ``obj`` is expected to be a ``dict`` (typically from ``dataclasses.asdict``
    on one of the ``*Payload`` dataclasses). The serialization settings below
    MUST NOT change without a cryptographic review — they are the signing
    substrate for every artefact in PathMarket.
    """

    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return _ID_PREFIX + hashlib.sha256(data).hexdigest()


def compute_sla_id(payload: SLAPayload) -> str:
    """Compute the content-hash SLA id per §4.6.

    Clears ``sla_id`` in a copy of ``payload``, serializes canonically, returns
    ``"sha256:<hex>"``. Deterministic; sensitive to any field change.
    """

    p = replace(payload, sla_id="")
    return _sha256_hex(canonical_json(asdict(p)))


def compute_claim_id(payload: ClaimPayload) -> str:
    """Compute the content-hash claim id per §4.6."""

    p = replace(payload, claim_id="")
    return _sha256_hex(canonical_json(asdict(p)))


def compute_complaint_id(payload: ComplaintPayload) -> str:
    """Compute the content-hash complaint id per §4.6."""

    p = replace(payload, complaint_id="")
    return _sha256_hex(canonical_json(asdict(p)))


def iso8601_utc_now() -> str:
    """ISO-8601 UTC timestamp with trailing ``Z`` and no fractional seconds."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "canonical_json",
    "compute_claim_id",
    "compute_complaint_id",
    "compute_sla_id",
    "iso8601_utc_now",
]
