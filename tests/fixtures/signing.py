"""Higher-level helpers for building fully-signed §4 artefacts in tests.

Composes :mod:`tests.fixtures.builders` (pure payload construction) with
:mod:`tests.fixtures.crypto` (keypairs). Used by aggregator-level tests that
need a keyring + valid SignedSLA/SignedClaim/SignedComplaint.
"""

from __future__ import annotations

from dataclasses import asdict, replace

from pathmarket.canonical import canonical_json, compute_sla_id
from pathmarket.schemas import (
    PathHop,
    SLABounds,
    SignedSLA,
)
from pathmarket.verifier import StaticKeyVerifier

from tests.fixtures.builders import make_sla_payload
from tests.fixtures.crypto import TestKey, keyring_from_test_keys, make_test_key


def make_keys_for(as_list: list[str]) -> dict[str, TestKey]:
    """Generate one fresh Ed25519 keypair per AS."""
    return {isd_as: make_test_key(isd_as) for isd_as in as_list}


def make_verifier(keys: dict[str, TestKey]) -> StaticKeyVerifier:
    return StaticKeyVerifier(keyring_from_test_keys(list(keys.values())))


def build_signed_sla(
    keys: dict[str, TestKey],
    *,
    cosigners: list[str] | None = None,
    path: list[PathHop] | None = None,
    bounds: SLABounds | None = None,
    price_per_gb: str = "0.028",
    valid_from: str = "2026-04-19T12:00:00Z",
    valid_until: str = "2026-04-20T12:00:00Z",
    nonce: str = "0001",
) -> SignedSLA:
    """Build a fully-signed SLA with ``sla_id`` pinned to the canonical hash.

    ``cosigners`` defaults to all keys in insertion order. ``path`` defaults to
    a trivial linear path of those cosigners with ingress==egress==0 except
    at the source/dest.
    """

    if cosigners is None:
        cosigners = list(keys.keys())
    if path is None:
        path = _default_linear_path(cosigners)
    if bounds is None:
        bounds = SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None)

    payload = make_sla_payload(
        cosigners=cosigners,
        path=path,
        bounds=bounds,
        price_per_gb=price_per_gb,
        valid_from=valid_from,
        valid_until=valid_until,
        nonce=nonce,
    )
    payload = replace(payload, sla_id=compute_sla_id(payload))
    payload_bytes = canonical_json(asdict(payload))
    signatures = [keys[c].sign(payload_bytes) for c in cosigners]
    return SignedSLA(payload=payload, signatures=signatures)


def _default_linear_path(cosigners: list[str]) -> list[PathHop]:
    hops: list[PathHop] = []
    for i, isd_as in enumerate(cosigners):
        ingress = 0 if i == 0 else 100 + i
        egress = 0 if i == len(cosigners) - 1 else 200 + i
        hops.append(PathHop(isd_as=isd_as, ingress=ingress, egress=egress))
    return hops


__all__ = ["build_signed_sla", "make_keys_for", "make_verifier"]
