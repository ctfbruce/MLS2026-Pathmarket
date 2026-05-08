"""Step B6 — aggregator end-to-end smoke test (``DESIGN.md`` §13).

A single TestClient-driven walk through the full product loop:

  POST /sla  →  POST /claim  →  3× POST /complaint (distinct complainants,
  within the 5-minute scoring window)  →  GET /score/{cosigner} observes a
  score drop  →  GET /ticker shows events in the expected temporal order.

This exercises the aggregator in the shape the UI will actually call it
during the demo, and serves as the contract check that §5.2's verification
chain, scoring wiring, and ticker synthesis all compose correctly.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal

from fastapi.testclient import TestClient

from pathmarket.aggregator.app import create_app
from pathmarket.aggregator.models import (
    SignedClaimModel,
    SignedComplaintModel,
    SignedSLAModel,
)
from pathmarket.aggregator.storage import AggregatorStorage
from pathmarket.canonical import canonical_json, compute_claim_id, compute_complaint_id
from pathmarket.schemas import SignedClaim, SignedComplaint

from tests.fixtures.builders import make_claim_payload, make_complaint_payload
from tests.fixtures.signing import build_signed_sla, make_keys_for, make_verifier


CONSORTIUM = ["1-ff00:0:112", "1-ff00:0:111", "1-ff00:0:122"]
CLAIMANT = "1-ff00:0:130"
COMPLAINANTS = ["1-ff00:0:221", "1-ff00:0:222", "1-ff00:0:223"]


def _make_claim(sla, claimant_key, *, gb: int = 500) -> SignedClaim:
    price = str(Decimal(sla.payload.price_per_gb) * Decimal(gb))
    payload = make_claim_payload(
        sla_id=sla.payload.sla_id,
        claimant=claimant_key.isd_as,
        gb_purchased=gb,
        price_paid_chf=price,
    )
    payload = replace(payload, claim_id=compute_claim_id(payload))
    sig = claimant_key.sign(canonical_json(asdict(payload)))
    return SignedClaim(payload=payload, signature=sig)


def _make_complaint(sla, complainant_key, *, observed_at: str) -> SignedComplaint:
    payload = make_complaint_payload(
        sla_id=sla.payload.sla_id,
        complainant=complainant_key.isd_as,
        path_used=list(sla.payload.path),
        metric="latency_ms",
        measured_value=42,  # SLA bound is latency_max_ms=20 by default.
        observed_at=observed_at,
    )
    payload = replace(payload, complaint_id=compute_complaint_id(payload))
    sig = complainant_key.sign(canonical_json(asdict(payload)))
    return SignedComplaint(payload=payload, signature=sig)


def test_sla_to_claim_to_complaints_to_score_drop_to_ticker() -> None:
    all_keys = make_keys_for(CONSORTIUM + [CLAIMANT] + COMPLAINANTS)
    storage = AggregatorStorage()
    app = create_app(storage=storage, verifier=make_verifier(all_keys))
    client = TestClient(app)

    # --- POST /sla ---------------------------------------------------------
    cosigner_keys = {a: all_keys[a] for a in CONSORTIUM}
    sla = build_signed_sla(
        cosigner_keys, cosigners=CONSORTIUM, price_per_gb="0.02"
    )
    r = client.post("/sla", json=SignedSLAModel.from_dataclass(sla).model_dump())
    assert r.status_code == 201, r.json()
    assert r.json()["status"] == "accepted"

    # --- POST /claim -------------------------------------------------------
    claim = _make_claim(sla, all_keys[CLAIMANT], gb=500)
    r = client.post(
        "/claim", json=SignedClaimModel.from_dataclass(claim).model_dump()
    )
    assert r.status_code == 201, r.json()
    assert r.json() == {"claim_id": claim.payload.claim_id, "status": "accepted"}

    # Baseline: all cosigners score 1.0 before any complaint fires.
    assert client.get(f"/score/{CONSORTIUM[0]}").json()["score"] == 1.0

    # --- POST 3× /complaint (k-corroboration threshold, within 5 min) ------
    for i, ca in enumerate(COMPLAINANTS):
        c = _make_complaint(
            sla, all_keys[ca], observed_at=f"2026-04-18T09:14:{i:02d}Z"
        )
        r = client.post(
            "/complaint", json=SignedComplaintModel.from_dataclass(c).model_dump()
        )
        assert r.status_code == 201, r.json()

    # --- GET /score/{cosigner} observes the drop ---------------------------
    score_body = client.get(f"/score/{CONSORTIUM[0]}").json()
    assert score_body["score"] < 1.0
    assert score_body["components"]["recent_violations"] == 1

    # Every cosigner should have dropped (they all share the violation).
    scores = {s["isd_as"]: s["score"] for s in client.get("/scores").json()["scores"]}
    for a in CONSORTIUM:
        assert scores[a] < 1.0, f"{a} score did not drop"

    # --- GET /ticker — newest-first, with the expected event types --------
    events = client.get("/ticker").json()["events"]
    # Ordering invariant: each event's timestamp >= the next event's timestamp.
    for earlier, later in zip(events, events[1:]):
        assert earlier["timestamp"] >= later["timestamp"]

    types_in_submission_order = list(reversed([e["type"] for e in events]))
    # The first events appended were sla_signed then claim.
    assert types_in_submission_order[0] == "sla_signed"
    assert types_in_submission_order[1] == "claim"
    # Every complaint POST appended a complaint event.
    assert types_in_submission_order.count("complaint") == 3
    # The k-th complaint crossed the reputation-change threshold, so at least
    # one reputation_change event was synthesized.
    rep_events = [e for e in events if e["type"] == "reputation_change"]
    assert rep_events, "expected at least one reputation_change ticker event"
    # At least one reputation_change mentions a cosigner AS.
    assert any(
        any(a in e["detail"] for a in CONSORTIUM) for e in rep_events
    )

    # --- Storage reflects the full walk -----------------------------------
    assert sla.payload.sla_id in storage.slas
    assert len(storage.claims) == 1
    assert len(storage.complaints) == 3
