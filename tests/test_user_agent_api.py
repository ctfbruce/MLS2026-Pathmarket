"""Step D2 — user-AS local API tests (``DESIGN.md`` §8.4).

Drives each endpoint with :class:`fastapi.testclient.TestClient` against a
:class:`FakeAggregatorClient`. Every mutation is verified both in the
response and via a fresh round-trip load from disk — the "state on disk is
what the UI observes" contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from pathmarket.agent.agent import Agent
from pathmarket.path_discovery.static_table import StaticPathTableDiscovery
from pathmarket.schemas import (
    PathHop,
    Policy,
    SLABounds,
    SignedSLA,
)
from pathmarket.user_agent.app import UserAgentService, create_app
from pathmarket.user_agent.state_store import (
    UserAgentState,
    load_or_init_state,
    save_state,
)
from pathmarket.aggregator.models import SignedSLAModel

from tests.fixtures.fake_client import FakeAggregatorClient


USER_AS = "1-ff00:0:112"
TRANSIT = ["1-a:0:1", "1-a:0:2"]


def _policy() -> Policy:
    return Policy(
        max_price_per_gb="0.08",
        min_reputation_floor=0.80,
        required_bounds=SLABounds(latency_max_ms=20, loss_max_ppm=500, bandwidth_min_kbps=None),
        alpha=1.5,
        beta=5.0,
        uncovered_tolerance="partial_ok",
        complaint_sensitivity="moderate",
        reshop_on_reputation_drop=0.15,
        portfolio_redundancy=1,
    )


def _seed_sla(client: FakeAggregatorClient) -> SignedSLA:
    transit_keys = {a: Ed25519PrivateKey.generate() for a in TRANSIT}
    disc = StaticPathTableDiscovery.from_mapping({})
    seller = Agent(
        isd_as=TRANSIT[0],
        private_key=transit_keys[TRANSIT[0]],
        policy=_policy(),
        aggregator_client=client,
        path_discovery=disc,
    )
    path = [
        PathHop(isd_as=TRANSIT[0], ingress=0, egress=201),
        PathHop(isd_as=TRANSIT[1], ingress=101, egress=0),
    ]
    return seller.publish_sla(
        path=path,
        bounds=SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None),
        price_per_gb="0.05",
        valid_from="2026-04-19T12:00:00Z",
        valid_until="2026-04-20T12:00:00Z",
        nonce="deadbeefdeadbeef",
        cosigner_keys=transit_keys,
    )


def _setup(tmp_path: Path) -> tuple[TestClient, UserAgentService, FakeAggregatorClient, SignedSLA]:
    client = FakeAggregatorClient()
    sla = _seed_sla(client)
    state = UserAgentState(
        isd_as=USER_AS,
        display_name="Zurich-Financial-Edge",
        policy=_policy(),
    )
    state_file = tmp_path / "state.json"
    svc = UserAgentService(
        state=state,
        state_file=state_file,
        aggregator_client=client,
        private_key=Ed25519PrivateKey.generate(),
        path_discovery=StaticPathTableDiscovery.from_mapping({}),
    )
    app = create_app(svc)
    return TestClient(app), svc, client, sla


# ---------------------------------------------------------------------------
# GET /local/state, /local/portfolio
# ---------------------------------------------------------------------------


class TestReads:
    def test_state_returns_identity_policy_and_empty_portfolio(self, tmp_path: Path) -> None:
        c, _, _, _ = _setup(tmp_path)
        r = c.get("/local/state")
        assert r.status_code == 200
        body = r.json()
        assert body["isd_as"] == USER_AS
        assert body["display_name"] == "Zurich-Financial-Edge"
        assert body["portfolio"] == []
        assert body["schema_version"] == 2

    def test_portfolio_empty_initially(self, tmp_path: Path) -> None:
        c, _, _, _ = _setup(tmp_path)
        r = c.get("/local/portfolio")
        assert r.status_code == 200
        assert r.json() == {"claims": []}


# ---------------------------------------------------------------------------
# PUT /local/policy
# ---------------------------------------------------------------------------


class TestPutPolicy:
    def test_valid_policy_replaces_and_persists(self, tmp_path: Path) -> None:
        c, svc, _, _ = _setup(tmp_path)
        new_body = {
            "max_price_per_gb": "0.05",
            "min_reputation_floor": 0.90,
            "required_bounds": {"latency_max_ms": 12},
            "alpha": 2.5,
            "beta": 4.0,
            "uncovered_tolerance": "never",
            "complaint_sensitivity": "strict",
            "reshop_on_reputation_drop": 0.07,
            "portfolio_redundancy": 3,
        }
        r = c.put("/local/policy", json=new_body)
        assert r.status_code == 200
        assert svc.agent.policy.max_price_per_gb == "0.05"
        # Round-trip from disk to confirm persistence.
        reloaded = load_or_init_state(
            svc.state_file,
            isd_as=USER_AS,
            display_name="Zurich-Financial-Edge",
            default_policy=_policy(),
        )
        assert reloaded.policy.complaint_sensitivity == "strict"

    def test_invalid_enum_returns_400(self, tmp_path: Path) -> None:
        c, _, _, _ = _setup(tmp_path)
        body = {
            "max_price_per_gb": "0.05",
            "min_reputation_floor": 0.80,
            "required_bounds": {},
            "alpha": 1.0,
            "beta": 1.0,
            "uncovered_tolerance": "whatever",
            "complaint_sensitivity": "moderate",
            "reshop_on_reputation_drop": 0.1,
            "portfolio_redundancy": 1,
        }
        r = c.put("/local/policy", json=body)
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# POST /local/actions/claim
# ---------------------------------------------------------------------------


class TestPostClaim:
    def test_known_sla_is_claimed_and_persisted(
        self, tmp_path: Path
    ) -> None:
        c, svc, _client, sla = _setup(tmp_path)
        r = c.post(
            "/local/actions/claim",
            json={"sla_id": sla.payload.sla_id, "gb_purchased": 500},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["payload"]["sla_id"] == sla.payload.sla_id
        assert body["payload"]["gb_purchased"] == 500
        # portfolio now holds one claim
        assert len(svc.agent.portfolio) == 1
        # and the file on disk agrees
        reloaded = load_or_init_state(
            svc.state_file, isd_as=USER_AS, display_name="x", default_policy=_policy()
        )
        assert len(reloaded.portfolio) == 1

    def test_unknown_sla_returns_404(self, tmp_path: Path) -> None:
        c, _, _, _ = _setup(tmp_path)
        r = c.post(
            "/local/actions/claim",
            json={"sla_id": "sha256:nope", "gb_purchased": 1},
        )
        assert r.status_code == 404

    def test_source_cosigner_claim_succeeds(self, tmp_path: Path) -> None:
        # The user's AS (112) is the first cosigner of the SLA — this models
        # templates like [112, 130] and [112, 111, 120, 130]. The relaxed
        # self-dealing rule allows 112 to claim because it sits at position 0.
        client = FakeAggregatorClient()
        user_key = Ed25519PrivateKey.generate()
        transit_key = Ed25519PrivateKey.generate()
        disc = StaticPathTableDiscovery.from_mapping({})
        # Simulator-side publish: user AS (112) at position 0, transit at 1.
        publisher = Agent(
            isd_as=USER_AS,
            private_key=user_key,
            policy=_policy(),
            aggregator_client=client,
            path_discovery=disc,
        )
        path = [
            PathHop(isd_as=USER_AS, ingress=0, egress=101),
            PathHop(isd_as="1-a:0:9", ingress=201, egress=0),
        ]
        sla = publisher.publish_sla(
            path=path,
            bounds=SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None),
            price_per_gb="0.05",
            valid_from="2026-04-19T12:00:00Z",
            valid_until="2026-04-20T12:00:00Z",
            nonce="feedfacefeedface",
            cosigner_keys={"1-a:0:9": transit_key},
        )
        state = UserAgentState(
            isd_as=USER_AS, display_name="Zurich-Financial-Edge", policy=_policy()
        )
        svc = UserAgentService(
            state=state,
            state_file=tmp_path / "state.json",
            aggregator_client=client,
            private_key=user_key,
            path_discovery=disc,
        )
        c = TestClient(create_app(svc))
        r = c.post(
            "/local/actions/claim",
            json={"sla_id": sla.payload.sla_id, "gb_purchased": 250},
        )
        assert r.status_code == 200, r.text
        assert len(svc.agent.portfolio) == 1


# ---------------------------------------------------------------------------
# POST /local/actions/complaint
# ---------------------------------------------------------------------------


class TestPostComplaint:
    def test_valid_complaint_posts_to_aggregator(self, tmp_path: Path) -> None:
        c, _svc, client, sla = _setup(tmp_path)
        r = c.post(
            "/local/actions/complaint",
            json={
                "sla_id": sla.payload.sla_id,
                "metric": "latency_ms",
                "measured_value": 47,
                "note": "sustained spike",
            },
        )
        assert r.status_code == 200
        assert len(client.posted_complaints) == 1
        assert client.posted_complaints[0].payload.metric == "latency_ms"

    def test_unknown_sla_returns_404(self, tmp_path: Path) -> None:
        c, _, _, _ = _setup(tmp_path)
        r = c.post(
            "/local/actions/complaint",
            json={"sla_id": "sha256:nope", "metric": "latency_ms",
                  "measured_value": 1, "note": ""},
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /local/actions/upload_sla
# ---------------------------------------------------------------------------


class TestUploadSLA:
    def test_forwards_to_aggregator(self, tmp_path: Path) -> None:
        c, _svc, client, sla = _setup(tmp_path)
        body = SignedSLAModel.from_dataclass(sla).model_dump()
        # Clear the aggregator's memory so we can see upload_sla re-post it.
        before = len(client.posted_slas)
        r = c.post("/local/actions/upload_sla", json=body)
        assert r.status_code == 200
        assert len(client.posted_slas) == before + 1

    def test_invalid_body_returns_400(self, tmp_path: Path) -> None:
        c, _, _, _ = _setup(tmp_path)
        r = c.post("/local/actions/upload_sla", json={"nope": "garbage"})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# PUT /local/routing/{dst}
# ---------------------------------------------------------------------------


class TestRouting:
    def test_valid_routing_persists(self, tmp_path: Path) -> None:
        c, svc, _client, sla = _setup(tmp_path)
        # First claim so we have a claim to reference.
        c.post(
            "/local/actions/claim",
            json={"sla_id": sla.payload.sla_id, "gb_purchased": 100},
        )
        claim_id = svc.agent.portfolio[0].payload.claim_id

        r = c.put(
            "/local/routing/1-ff00:0:130",
            json={
                "path": [
                    {"isd_as": TRANSIT[0], "ingress": 0, "egress": 201},
                    {"isd_as": TRANSIT[1], "ingress": 101, "egress": 0},
                ],
                "applied_claims": {"0": claim_id, "1": claim_id},
                "manual_override": True,
            },
        )
        assert r.status_code == 200
        reloaded = load_or_init_state(
            svc.state_file, isd_as=USER_AS, display_name="x", default_policy=_policy()
        )
        rd = reloaded.routing_decisions["1-ff00:0:130"]
        assert rd.applied_claims == {0: claim_id, 1: claim_id}
        assert rd.manual_override is True

    def test_unknown_claim_in_routing_returns_400(self, tmp_path: Path) -> None:
        c, _svc, _client, _sla = _setup(tmp_path)
        r = c.put(
            "/local/routing/1-ff00:0:130",
            json={
                "path": [{"isd_as": TRANSIT[0], "ingress": 0, "egress": 0}],
                "applied_claims": {"0": "sha256:not-in-portfolio"},
                "manual_override": True,
            },
        )
        assert r.status_code == 400

    def test_hop_index_out_of_range_returns_400(self, tmp_path: Path) -> None:
        c, svc, _client, sla = _setup(tmp_path)
        c.post(
            "/local/actions/claim",
            json={"sla_id": sla.payload.sla_id, "gb_purchased": 1},
        )
        claim_id = svc.agent.portfolio[0].payload.claim_id
        r = c.put(
            "/local/routing/1-ff00:0:130",
            json={
                "path": [{"isd_as": TRANSIT[0], "ingress": 0, "egress": 0}],
                "applied_claims": {"7": claim_id},
                "manual_override": False,
            },
        )
        assert r.status_code == 400
