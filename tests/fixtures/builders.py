"""Small builders for constructing valid §4 payloads in tests.

These are deliberately minimal: just enough to produce hashable, signable
payloads. Individual tests override fields as needed. Keep this file free of
crypto (no key material, no signatures) — that belongs in test_verifier.py's
fixtures.
"""

from __future__ import annotations

from pathmarket.schemas import (
    Attachment,
    ClaimPayload,
    ComplaintPayload,
    PathHop,
    SLABounds,
    SLAPayload,
)


def make_sla_payload(
    *,
    sla_id: str = "",
    cosigners: list[str] | None = None,
    price_per_gb: str = "0.028",
    valid_from: str = "2026-04-19T12:00:00Z",
    valid_until: str = "2026-04-20T12:00:00Z",
    nonce: str = "3f1e4a9b2c8d7e6f5a3b2c1d9e8f7a6b",
    bounds: SLABounds | None = None,
    path: list[PathHop] | None = None,
    schema_version: int = 2,
) -> SLAPayload:
    if cosigners is None:
        cosigners = ["1-ff00:0:112", "1-ff00:0:111", "1-ff00:0:122"]
    if path is None:
        path = [
            PathHop(isd_as="1-ff00:0:112", ingress=0, egress=495),
            PathHop(isd_as="1-ff00:0:111", ingress=113, egress=104),
            PathHop(isd_as="1-ff00:0:122", ingress=2, egress=0),
        ]
    if bounds is None:
        bounds = SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None)
    return SLAPayload(
        sla_id=sla_id,
        path=path,
        cosigners=cosigners,
        bounds=bounds,
        price_per_gb=price_per_gb,
        valid_from=valid_from,
        valid_until=valid_until,
        nonce=nonce,
        schema_version=schema_version,
    )


def make_claim_payload(
    *,
    claim_id: str = "",
    sla_id: str = "sha256:abc",
    claimant: str = "1-ff00:0:130",
    gb_purchased: int = 500,
    claimed_at: str = "2026-04-19T13:15:00Z",
    price_paid_chf: str = "14.00",
    schema_version: int = 2,
) -> ClaimPayload:
    return ClaimPayload(
        claim_id=claim_id,
        sla_id=sla_id,
        claimant=claimant,
        gb_purchased=gb_purchased,
        claimed_at=claimed_at,
        price_paid_chf=price_paid_chf,
        schema_version=schema_version,
    )


def make_complaint_payload(
    *,
    complaint_id: str = "",
    sla_id: str = "sha256:abc",
    complainant: str = "1-ff00:0:221",
    path_used: list[PathHop] | None = None,
    metric: str = "latency_ms",
    measured_value: int = 22,
    observed_at: str = "2026-04-18T09:14:00Z",
    note: str = "",
    attachments: list[Attachment] | None = None,
    schema_version: int = 2,
) -> ComplaintPayload:
    if path_used is None:
        path_used = [
            PathHop(isd_as="1-ff00:0:112", ingress=0, egress=495),
            PathHop(isd_as="1-ff00:0:111", ingress=113, egress=104),
            PathHop(isd_as="1-ff00:0:122", ingress=2, egress=0),
        ]
    if attachments is None:
        attachments = []
    return ComplaintPayload(
        complaint_id=complaint_id,
        sla_id=sla_id,
        complainant=complainant,
        path_used=path_used,
        metric=metric,
        measured_value=measured_value,
        observed_at=observed_at,
        note=note,
        attachments=attachments,
        schema_version=schema_version,
    )
