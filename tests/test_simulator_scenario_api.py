"""Step C5 — scenario-control FastAPI tests (``DESIGN.md`` §9.12).

Exercises the HTTP surface of :mod:`pathmarket.simulator.scenario_api` via
:class:`fastapi.testclient.TestClient`. Each scenario is already unit-tested
against the underlying :mod:`pathmarket.simulator.scenarios` functions; these
tests only verify the HTTP wiring (status codes, request/response shapes,
the ``on_reset`` callback hook).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from pathmarket.path_discovery.static_table import StaticPathTableDiscovery
from pathmarket.schemas import PathHop, Policy, SLABounds
from pathmarket.simulator.orchestrator import (
    AgentSpec,
    Orchestrator,
    SLATemplate,
)
from pathmarket.simulator.scenario_api import create_scenario_app

from tests.fixtures.fake_client import FakeAggregatorClient


TRANSIT = ["1-a:0:1", "1-a:0:2", "1-a:0:3"]
BUYERS = ["1-z:0:1", "1-z:0:2"]


def _transit_policy() -> Policy:
    return Policy(
        max_price_per_gb="1.00",
        min_reputation_floor=0.0,
        required_bounds=SLABounds(None, None, None),
        alpha=1.0,
        beta=1.0,
        uncovered_tolerance="anywhere",
        complaint_sensitivity="moderate",
        reshop_on_reputation_drop=1.0,
        portfolio_redundancy=0,
    )


def _buyer_policy() -> Policy:
    return Policy(
        max_price_per_gb="0.10",
        min_reputation_floor=0.50,
        required_bounds=SLABounds(latency_max_ms=30, loss_max_ppm=1_000, bandwidth_min_kbps=None),
        alpha=1.0,
        beta=5.0,
        uncovered_tolerance="partial_ok",
        complaint_sensitivity="moderate",
        reshop_on_reputation_drop=0.10,
        portfolio_redundancy=1,
    )


def _linear_path(ases: list[str]) -> list[PathHop]:
    return [
        PathHop(
            isd_as=a,
            ingress=0 if i == 0 else 100 + i,
            egress=0 if i == len(ases) - 1 else 200 + i,
        )
        for i, a in enumerate(ases)
    ]


def _setup() -> tuple[Orchestrator, FakeAggregatorClient]:
    all_ases = TRANSIT + BUYERS
    private_keys = {a: Ed25519PrivateKey.generate() for a in all_ases}
    specs: list[AgentSpec] = []
    for a in TRANSIT:
        specs.append(AgentSpec(isd_as=a, role="transit-good", policy=_transit_policy(),
                               quality_profile="transit-good"))
    for a in BUYERS:
        specs.append(AgentSpec(isd_as=a, role="edge-buyer", policy=_buyer_policy()))

    templates = [
        SLATemplate(
            path=_linear_path(TRANSIT),
            bounds=SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None),
            price_per_gb="0.05",
            consortium_profile="transit-good",
        ),
    ]
    client = FakeAggregatorClient()
    start = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    counter = {"n": 0}

    def now_fn() -> datetime:
        t = start + timedelta(seconds=counter["n"])
        counter["n"] += 1
        return t

    orch = Orchestrator(
        specs=specs,
        private_keys=private_keys,
        sla_templates=templates,
        aggregator_client=client,
        path_discovery=StaticPathTableDiscovery.from_mapping({}),
        rng=random.Random(7),
        now_fn=now_fn,
    )
    return orch, client


# ---------------------------------------------------------------------------
# /scenarios/reset
# ---------------------------------------------------------------------------


class TestResetEndpoint:
    def test_reset_preseeds_default_count(self) -> None:
        orch, _ = _setup()
        app = create_scenario_app(orch, default_preseed_count=4)
        with TestClient(app) as c:
            r = c.post("/scenarios/reset", json={})
            assert r.status_code == 200
            body = r.json()
            assert body == {"triggered": "reset", "preseeded": 4}
            assert len(orch.live_sla_ids) == 4

    def test_reset_honors_explicit_count(self) -> None:
        orch, _ = _setup()
        app = create_scenario_app(orch, default_preseed_count=4)
        with TestClient(app) as c:
            r = c.post("/scenarios/reset", json={"preseed_count": 2})
            assert r.json()["preseeded"] == 2
            assert len(orch.live_sla_ids) == 2

    def test_reset_invokes_on_reset_hook(self) -> None:
        orch, _ = _setup()
        hits: list[int] = []
        app = create_scenario_app(orch, on_reset=lambda: hits.append(1))
        with TestClient(app) as c:
            c.post("/scenarios/reset", json={})
            c.post("/scenarios/reset", json={})
        assert len(hits) == 2


# ---------------------------------------------------------------------------
# /scenarios/hospital_reshops
# ---------------------------------------------------------------------------


class TestHospitalReshopsEndpoint:
    def test_flips_latest_sla_by_default(self) -> None:
        orch, _ = _setup()
        orch.pre_seed(count=2)
        expected = orch.live_sla_ids[-1]
        app = create_scenario_app(orch)
        with TestClient(app) as c:
            r = c.post("/scenarios/hospital_reshops", json={})
            assert r.status_code == 200
            assert r.json() == {"triggered": "hospital_reshops", "sla_id": expected}

    def test_no_live_slas_returns_400(self) -> None:
        orch, _ = _setup()
        app = create_scenario_app(orch)
        with TestClient(app) as c:
            r = c.post("/scenarios/hospital_reshops", json={})
            assert r.status_code == 400
            assert "no live SLAs" in r.json()["detail"]

    def test_unknown_sla_returns_404(self) -> None:
        orch, _ = _setup()
        orch.pre_seed(count=1)
        app = create_scenario_app(orch)
        with TestClient(app) as c:
            r = c.post(
                "/scenarios/hospital_reshops",
                json={"target_sla_id": "sha256:nope"},
            )
            assert r.status_code == 404


# ---------------------------------------------------------------------------
# /scenarios/cloud_bargain
# ---------------------------------------------------------------------------


class TestCloudBargainEndpoint:
    def test_publishes_cheap_sla_at_default_price(self) -> None:
        orch, client = _setup()
        app = create_scenario_app(orch)
        with TestClient(app) as c:
            r = c.post("/scenarios/cloud_bargain", json={})
            assert r.status_code == 200
            sla_id = r.json()["sla_id"]
            assert sla_id in orch.live_sla_ids
            assert client.posted_slas[-1].payload.price_per_gb == "0.008"

    def test_respects_override_price(self) -> None:
        orch, client = _setup()
        app = create_scenario_app(orch)
        with TestClient(app) as c:
            r = c.post("/scenarios/cloud_bargain", json={"price_per_gb": "0.003"})
            assert r.status_code == 200
            assert client.posted_slas[-1].payload.price_per_gb == "0.003"

    def test_no_templates_returns_400(self) -> None:
        orch, _ = _setup()
        orch.sla_templates.clear()
        app = create_scenario_app(orch)
        with TestClient(app) as c:
            r = c.post("/scenarios/cloud_bargain", json={})
            assert r.status_code == 400


# ---------------------------------------------------------------------------
# /scenarios/bad_actor_cascade
# ---------------------------------------------------------------------------


class TestBadActorCascadeEndpoint:
    def test_flips_all_cosigned_slas(self) -> None:
        orch, _ = _setup()
        orch.pre_seed(count=3)
        app = create_scenario_app(orch)
        with TestClient(app) as c:
            r = c.post("/scenarios/bad_actor_cascade", json={"isd_as": TRANSIT[0]})
            assert r.status_code == 200
            body = r.json()
            assert body["triggered"] == "bad_actor_cascade"
            assert len(body["sla_ids"]) == 3

    def test_unknown_as_returns_404(self) -> None:
        orch, _ = _setup()
        app = create_scenario_app(orch)
        with TestClient(app) as c:
            r = c.post("/scenarios/bad_actor_cascade", json={"isd_as": "1-ZZ:0:999"})
            assert r.status_code == 404

    def test_missing_isd_as_returns_422(self) -> None:
        """pydantic rejects the body (isd_as is required)."""
        orch, _ = _setup()
        app = create_scenario_app(orch)
        with TestClient(app) as c:
            r = c.post("/scenarios/bad_actor_cascade", json={})
            assert r.status_code == 422
