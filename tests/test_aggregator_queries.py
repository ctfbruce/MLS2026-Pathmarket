"""Step B5 — query-endpoint tests (``DESIGN.md`` §5.2).

Covers: /slas filters, /slas/{id}, /claims filters, /complaints filters+search,
/score/{as}, /scores, /market/summary, /ticker ordering, reputation_change
ticker synthesis. Plus /admin/reset gated by config flag.
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
from pathmarket.schemas import PathHop, SLABounds, SignedClaim, SignedComplaint

from tests.fixtures.builders import make_claim_payload, make_complaint_payload
from tests.fixtures.signing import build_signed_sla, make_keys_for, make_verifier


CONSORTIUM = ["1-ff00:0:112", "1-ff00:0:111", "1-ff00:0:122"]
CLAIMANT = "1-ff00:0:130"
COMPLAINANTS = ["1-ff00:0:221", "1-ff00:0:222", "1-ff00:0:223"]


def _post(client, path, model):
    return client.post(path, json=model.model_dump())


def _setup(*, enable_admin: bool = False):
    all_keys = make_keys_for(CONSORTIUM + [CLAIMANT] + COMPLAINANTS)
    storage = AggregatorStorage()
    app = create_app(
        storage=storage,
        verifier=make_verifier(all_keys),
        enable_admin_endpoints=enable_admin,
    )
    return TestClient(app), storage, all_keys


def _post_sla(client, sla):
    return _post(client, "/sla", SignedSLAModel.from_dataclass(sla))


def _post_claim(client, claim):
    return _post(client, "/claim", SignedClaimModel.from_dataclass(claim))


def _post_complaint(client, complaint):
    return _post(client, "/complaint", SignedComplaintModel.from_dataclass(complaint))


def _make_claim(sla, claimant_key, gb=100) -> SignedClaim:
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


def _make_complaint(
    sla, complainant_key, *, metric="latency_ms", measured=22, observed_at=None, note=""
) -> SignedComplaint:
    payload = make_complaint_payload(
        sla_id=sla.payload.sla_id,
        complainant=complainant_key.isd_as,
        path_used=list(sla.payload.path),
        metric=metric,
        measured_value=measured,
        observed_at=observed_at or "2026-04-18T09:14:00Z",
        note=note,
    )
    payload = replace(payload, complaint_id=compute_complaint_id(payload))
    sig = complainant_key.sign(canonical_json(asdict(payload)))
    return SignedComplaint(payload=payload, signature=sig)


class TestGetSlas:
    def test_empty_when_no_slas(self) -> None:
        client, _, _ = _setup()
        assert client.get("/slas").json() == {"slas": []}

    def test_lists_all_active(self) -> None:
        client, _, keys = _setup()
        cosigner_keys = {a: keys[a] for a in CONSORTIUM}
        sla = build_signed_sla(cosigner_keys, cosigners=CONSORTIUM)
        assert _post_sla(client, sla).status_code == 201
        body = client.get("/slas").json()
        assert len(body["slas"]) == 1
        assert body["slas"][0]["payload"]["sla_id"] == sla.payload.sla_id

    def test_filter_by_src_and_dst(self) -> None:
        client, _, keys = _setup()
        cosigner_keys = {a: keys[a] for a in CONSORTIUM}
        sla = build_signed_sla(cosigner_keys, cosigners=CONSORTIUM)
        _post_sla(client, sla)
        # Matches src.
        r = client.get(f"/slas?src_isd_as={CONSORTIUM[0]}")
        assert len(r.json()["slas"]) == 1
        # Wrong src.
        r = client.get("/slas?src_isd_as=1-ff00:0:999")
        assert r.json() == {"slas": []}
        # Matches dst.
        r = client.get(f"/slas?dst_isd_as={CONSORTIUM[-1]}")
        assert len(r.json()["slas"]) == 1

    def test_covers_hop(self) -> None:
        client, _, keys = _setup()
        cosigner_keys = {a: keys[a] for a in CONSORTIUM}
        sla = build_signed_sla(cosigner_keys, cosigners=CONSORTIUM)
        _post_sla(client, sla)
        hop = f"{CONSORTIUM[0]}>{CONSORTIUM[1]}"
        r = client.get(f"/slas?covers_hop={hop}")
        assert len(r.json()["slas"]) == 1
        r = client.get(f"/slas?covers_hop={CONSORTIUM[0]}>1-ff00:0:999")
        assert r.json() == {"slas": []}

    def test_active_only_excludes_expired(self) -> None:
        client, _, keys = _setup()
        cosigner_keys = {a: keys[a] for a in CONSORTIUM}
        expired = build_signed_sla(
            cosigner_keys,
            cosigners=CONSORTIUM,
            valid_from="2024-01-01T00:00:00Z",
            valid_until="2024-01-02T00:00:00Z",
        )
        _post_sla(client, expired)
        assert client.get("/slas").json() == {"slas": []}
        assert len(client.get("/slas?active_only=false").json()["slas"]) == 1


class TestGetSlaById:
    def test_404_when_missing(self) -> None:
        client, _, _ = _setup()
        r = client.get("/slas/sha256:" + "0" * 64)
        assert r.status_code == 404
        assert r.json() == {"error": "not_found"}

    def test_returns_sla_when_present(self) -> None:
        client, _, keys = _setup()
        cosigner_keys = {a: keys[a] for a in CONSORTIUM}
        sla = build_signed_sla(cosigner_keys, cosigners=CONSORTIUM)
        _post_sla(client, sla)
        r = client.get(f"/slas/{sla.payload.sla_id}")
        assert r.status_code == 200
        assert r.json()["payload"]["sla_id"] == sla.payload.sla_id


class TestGetClaims:
    def test_filter_by_claimant_and_sla(self) -> None:
        client, _, keys = _setup()
        cosigner_keys = {a: keys[a] for a in CONSORTIUM}
        sla = build_signed_sla(cosigner_keys, cosigners=CONSORTIUM)
        _post_sla(client, sla)
        claim = _make_claim(sla, keys[CLAIMANT], gb=200)
        assert _post_claim(client, claim).status_code == 201

        r = client.get(f"/claims?claimant={CLAIMANT}").json()
        assert len(r["claims"]) == 1
        r = client.get("/claims?claimant=1-ff00:0:999").json()
        assert r == {"claims": []}
        r = client.get(f"/claims?sla_id={sla.payload.sla_id}").json()
        assert len(r["claims"]) == 1


class TestGetComplaints:
    def test_filter_by_metric_and_search(self) -> None:
        client, _, keys = _setup()
        cosigner_keys = {a: keys[a] for a in CONSORTIUM}
        sla = build_signed_sla(
            cosigner_keys,
            cosigners=CONSORTIUM,
            bounds=SLABounds(
                latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None
            ),
        )
        _post_sla(client, sla)
        c1 = _make_complaint(
            sla, keys[COMPLAINANTS[0]], metric="latency_ms", measured=22,
            note="Zurich spike observed"
        )
        c2 = _make_complaint(
            sla, keys[COMPLAINANTS[1]], metric="loss_ppm", measured=800,
            note="packet drops over threshold"
        )
        assert _post_complaint(client, c1).status_code == 201
        assert _post_complaint(client, c2).status_code == 201

        r = client.get("/complaints?metric=latency_ms").json()
        assert len(r["complaints"]) == 1
        r = client.get("/complaints?search=spike").json()
        assert len(r["complaints"]) == 1
        r = client.get("/complaints?search=drops").json()
        assert len(r["complaints"]) == 1
        r = client.get("/complaints?search=ZURICH").json()
        # Search is case-insensitive per §5.2.
        assert len(r["complaints"]) == 1

    def test_filter_by_since(self) -> None:
        client, _, keys = _setup()
        cosigner_keys = {a: keys[a] for a in CONSORTIUM}
        sla = build_signed_sla(cosigner_keys, cosigners=CONSORTIUM)
        _post_sla(client, sla)
        c = _make_complaint(
            sla, keys[COMPLAINANTS[0]], observed_at="2026-04-18T09:14:00Z"
        )
        _post_complaint(client, c)
        r = client.get("/complaints?since=2026-04-18T09:20:00Z").json()
        assert r == {"complaints": []}
        r = client.get("/complaints?since=2026-04-18T00:00:00Z").json()
        assert len(r["complaints"]) == 1


class TestGetScore:
    def test_as_with_no_complaints_scores_one(self) -> None:
        client, _, _ = _setup()
        r = client.get(f"/score/{CONSORTIUM[0]}").json()
        assert r["score"] == 1.0
        assert r["components"] == {}

    def test_score_drops_after_k_corroborated_complaints(self) -> None:
        client, _, keys = _setup()
        cosigner_keys = {a: keys[a] for a in CONSORTIUM}
        sla = build_signed_sla(cosigner_keys, cosigners=CONSORTIUM)
        _post_sla(client, sla)
        # Three distinct complainants within the default 5-minute scoring window.
        for i, ca in enumerate(COMPLAINANTS):
            c = _make_complaint(
                sla, keys[ca], observed_at=f"2026-04-18T09:14:{i:02d}Z"
            )
            assert _post_complaint(client, c).status_code == 201
        r = client.get(f"/score/{CONSORTIUM[0]}").json()
        assert r["score"] < 1.0
        assert r["components"]["recent_violations"] == 1


class TestGetScores:
    def test_returns_every_cosigner(self) -> None:
        client, _, keys = _setup()
        cosigner_keys = {a: keys[a] for a in CONSORTIUM}
        sla = build_signed_sla(cosigner_keys, cosigners=CONSORTIUM)
        _post_sla(client, sla)
        r = client.get("/scores").json()
        assert {s["isd_as"] for s in r["scores"]} == set(CONSORTIUM)
        assert all(s["score"] == 1.0 for s in r["scores"])


class TestMarketSummary:
    def test_tiers_and_counts(self) -> None:
        client, _, keys = _setup()
        cosigner_keys = {a: keys[a] for a in CONSORTIUM}
        # Premium SLA (price >= 0.10).
        premium = build_signed_sla(cosigner_keys, cosigners=CONSORTIUM, price_per_gb="0.15", nonce="p")
        # Commodity SLA (price < 0.03).
        commodity = build_signed_sla(cosigner_keys, cosigners=CONSORTIUM, price_per_gb="0.02", nonce="c")
        _post_sla(client, premium)
        _post_sla(client, commodity)

        r = client.get("/market/summary").json()
        assert r["total_slas_active"] == 2
        assert r["total_claims_active"] == 0
        assert r["total_complaints_last_hour"] == 0
        assert r["mean_price_by_tier"]["premium"] is not None
        assert r["mean_price_by_tier"]["commodity"] is not None
        # No mid tier SLAs → None.
        assert r["mean_price_by_tier"]["mid"] is None


class TestTicker:
    def test_newest_first_ordering(self) -> None:
        client, _, keys = _setup()
        cosigner_keys = {a: keys[a] for a in CONSORTIUM}
        sla1 = build_signed_sla(cosigner_keys, cosigners=CONSORTIUM, nonce="t1")
        sla2 = build_signed_sla(cosigner_keys, cosigners=CONSORTIUM, nonce="t2")
        _post_sla(client, sla1)
        _post_sla(client, sla2)
        events = client.get("/ticker").json()["events"]
        # Both are sla_signed; the second one posted should appear first.
        assert events[0]["type"] == "sla_signed"
        assert events[1]["type"] == "sla_signed"
        assert events[0]["timestamp"] >= events[1]["timestamp"]

    def test_limit_caps_at_100(self) -> None:
        client, _, _ = _setup()
        r = client.get("/ticker?limit=500").json()
        assert len(r["events"]) <= 100

    def test_reputation_change_events_synthesized_on_score_drop(self) -> None:
        """When a new complaint pushes a cosigner past the 0.01 threshold, emit."""
        client, _, keys = _setup()
        cosigner_keys = {a: keys[a] for a in CONSORTIUM}
        sla = build_signed_sla(cosigner_keys, cosigners=CONSORTIUM)
        _post_sla(client, sla)
        for i, ca in enumerate(COMPLAINANTS):
            _post_complaint(
                client,
                _make_complaint(
                    sla, keys[ca], observed_at=f"2026-04-18T09:14:{i:02d}Z"
                ),
            )
        events = client.get("/ticker").json()["events"]
        types = [e["type"] for e in events]
        # At least one reputation_change event appeared (per cosigner, score moved).
        assert "reputation_change" in types
        # Detail shape: "AS <isd_as> reputation: <before> → <after>".
        rep_events = [e for e in events if e["type"] == "reputation_change"]
        assert any(CONSORTIUM[0] in e["detail"] for e in rep_events)


class TestAdminReset:
    def test_404_when_disabled(self) -> None:
        client, _, _ = _setup(enable_admin=False)
        r = client.post("/admin/reset")
        assert r.status_code == 404

    def test_clears_storage_when_enabled(self) -> None:
        client, storage, keys = _setup(enable_admin=True)
        cosigner_keys = {a: keys[a] for a in CONSORTIUM}
        sla = build_signed_sla(cosigner_keys, cosigners=CONSORTIUM)
        _post_sla(client, sla)
        assert len(storage.slas) == 1

        r = client.post("/admin/reset")
        assert r.status_code == 200
        assert r.json() == {"status": "reset"}
        assert storage.slas == {}
        assert len(storage.ticker_events) == 0
