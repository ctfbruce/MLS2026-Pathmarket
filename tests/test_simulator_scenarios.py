"""Step C4 — presenter scenario-trigger tests (``DESIGN.md`` §9.11, §9.12).

Each test pokes the simulator with one scenario mutation and asserts the
intended effect materializes within a few ticks.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pathmarket.agent.simulated_quality import sample_violates
from pathmarket.path_discovery.static_table import StaticPathTableDiscovery
from pathmarket.schemas import PathHop, Policy, SLABounds
from pathmarket.simulator.orchestrator import (
    AgentSpec,
    Orchestrator,
    SLATemplate,
)
from pathmarket.simulator.scenarios import (
    bad_actor_cascade,
    cloud_bargain,
    hospital_reshops,
    reset_market,
)

from tests.fixtures.fake_client import FakeAggregatorClient


# ---------------------------------------------------------------------------
# Fixture setup mirroring C3's, but kept local so this file is independent.
# ---------------------------------------------------------------------------


TRANSIT_A = ["1-a:0:1", "1-a:0:2", "1-a:0:3"]
TRANSIT_B = ["1-b:0:1", "1-b:0:2", "1-b:0:3"]
BUYERS = ["1-z:0:1", "1-z:0:2", "1-z:0:3"]


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


def _setup(seed: int = 7):
    all_ases = TRANSIT_A + TRANSIT_B + BUYERS
    private_keys = {a: Ed25519PrivateKey.generate() for a in all_ases}
    specs: list[AgentSpec] = []
    for a in TRANSIT_A:
        specs.append(AgentSpec(isd_as=a, role="transit-good", policy=_transit_policy(), quality_profile="transit-good"))
    for a in TRANSIT_B:
        specs.append(AgentSpec(isd_as=a, role="transit-bad", policy=_transit_policy(), quality_profile="transit-bad"))
    for a in BUYERS:
        specs.append(AgentSpec(isd_as=a, role="edge-buyer", policy=_buyer_policy()))

    templates = [
        SLATemplate(
            path=_linear_path(TRANSIT_A),
            bounds=SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None),
            price_per_gb="0.05",
            consortium_profile="transit-good",
        ),
        SLATemplate(
            path=_linear_path(TRANSIT_B),
            bounds=SLABounds(latency_max_ms=15, loss_max_ppm=500, bandwidth_min_kbps=None),
            price_per_gb="0.04",
            consortium_profile="transit-bad",
        ),
    ]
    client = FakeAggregatorClient()
    start = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    counter = {"n": 0}

    def now_fn():
        t = start + timedelta(seconds=counter["n"])
        counter["n"] += 1
        return t

    return Orchestrator(
        specs=specs,
        private_keys=private_keys,
        sla_templates=templates,
        aggregator_client=client,
        path_discovery=StaticPathTableDiscovery.from_mapping({}),
        rng=random.Random(seed),
        now_fn=now_fn,
    ), client


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestResetMarket:
    def test_clears_and_reseeds(self) -> None:
        orch, _ = _setup()
        orch.pre_seed(count=3)
        initial_sla_ids = set(orch.live_sla_ids)
        # Run a tick to generate some portfolio state.
        orch.tick(publish_prob=1.0)
        assert any(a.portfolio for a in orch.agents.values()) or orch.live_sla_ids

        n = reset_market(orch, preseed_count=2)
        assert n == 2
        # Old SLA ids are gone from simulator-side tracking.
        assert not (set(orch.live_sla_ids) & initial_sla_ids)
        # Every agent's portfolio is cleared.
        for a in orch.agents.values():
            assert a.portfolio == []
            assert a.routing_decisions == {}


# ---------------------------------------------------------------------------
# hospital_reshops
# ---------------------------------------------------------------------------


class TestHospitalReshops:
    def test_flips_quality_to_always_violating(self) -> None:
        orch, _ = _setup()
        orch.pre_seed(count=2)
        target = orch.live_sla_ids[-1]
        bounds = orch.sla_bounds[target]
        flipped = hospital_reshops(orch)
        assert flipped == target

        # 50 samples: essentially all violate at least one metric.
        model = orch.quality_models[target]
        violations = 0
        for _ in range(50):
            s = model.sample(orch.rng)
            if any(sample_violates(bounds, s, m) for m in ("latency_ms", "loss_ppm", "bandwidth_kbps")):
                violations += 1
        assert violations >= 48

    def test_within_few_ticks_complaints_land(self) -> None:
        """After flipping, a buyer holding a claim files a complaint within a few ticks."""
        orch, _ = _setup()
        orch.pre_seed(count=2)
        # First: force buyers to hold claims on the target SLA via several ticks.
        for _ in range(8):
            orch.tick(publish_prob=0.0)
        # Pick an SLA that at least one buyer actually claimed.
        claimed = {c.payload.sla_id for a in orch.agents.values() for c in a.portfolio}
        if not claimed:
            pytest.skip("no claims in baseline ticks — adjust seed")
        target = next(iter(claimed))
        hospital_reshops(orch, target_sla_id=target)
        complaints_seen = 0
        for _ in range(5):
            complaints_seen += len(orch.tick(publish_prob=0.0).new_complaints)
        assert complaints_seen >= 1

    def test_no_live_slas_raises(self) -> None:
        orch, _ = _setup()
        with pytest.raises(RuntimeError, match="no live SLAs"):
            hospital_reshops(orch)

    def test_unknown_target_raises(self) -> None:
        orch, _ = _setup()
        orch.pre_seed(count=1)
        with pytest.raises(KeyError):
            hospital_reshops(orch, target_sla_id="sha256:nope")


# ---------------------------------------------------------------------------
# cloud_bargain
# ---------------------------------------------------------------------------


class TestCloudBargain:
    def test_publishes_cheap_sla(self) -> None:
        orch, client = _setup()
        orch.pre_seed(count=1)
        before = len(client.posted_slas)
        new_id = cloud_bargain(orch, price_per_gb="0.005")
        assert new_id in orch.live_sla_ids
        assert len(client.posted_slas) == before + 1
        sla = client.posted_slas[-1]
        assert sla.payload.price_per_gb == "0.005"

    def test_uses_template_path_by_default(self) -> None:
        orch, client = _setup()
        new_id = cloud_bargain(orch)
        # Reused the first template's path.
        sla = client.posted_slas[-1]
        assert sla.payload.path == orch.sla_templates[0].path

    def test_no_templates_and_no_path_raises(self) -> None:
        orch, _ = _setup()
        orch.sla_templates.clear()
        with pytest.raises(RuntimeError, match="needs either a path"):
            cloud_bargain(orch)


# ---------------------------------------------------------------------------
# bad_actor_cascade
# ---------------------------------------------------------------------------


class TestBadActorCascade:
    def test_flips_all_slas_cosigned_by_as(self) -> None:
        orch, _ = _setup()
        orch.pre_seed(count=6)  # ~3 from each template, so transit B appears
        bad_as = TRANSIT_B[0]
        flipped = bad_actor_cascade(orch, isd_as=bad_as)
        assert flipped, "expected at least one flipped SLA"
        # Every flipped SLA now samples as violating.
        for sla_id in flipped:
            bounds = orch.sla_bounds[sla_id]
            model = orch.quality_models[sla_id]
            s = model.sample(orch.rng)
            assert any(
                sample_violates(bounds, s, m) for m in ("latency_ms", "loss_ppm", "bandwidth_kbps")
            )

    def test_no_slas_returns_empty(self) -> None:
        """If the AS cosigns no live SLAs, return [] without raising."""
        orch, _ = _setup()
        orch.pre_seed(count=2)
        # Pick a buyer — never a cosigner on any SLA.
        assert bad_actor_cascade(orch, isd_as=BUYERS[0]) == []

    def test_unknown_as_raises(self) -> None:
        orch, _ = _setup()
        with pytest.raises(KeyError, match="unknown AS"):
            bad_actor_cascade(orch, isd_as="1-ZZ:0:999")
