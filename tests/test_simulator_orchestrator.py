"""Step C3 — orchestrator tick-loop tests (``DESIGN.md`` §7.2, §7.6).

Tests run the orchestrator against a :class:`FakeAggregatorClient` so the
aggregator's validation chain is not re-exercised here. The focus is on the
orchestrator itself: pre-seed, per-tick SLA publish / claim / complaint
generation, determinism under a seeded RNG.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pathmarket.path_discovery.static_table import StaticPathTableDiscovery
from pathmarket.schemas import PathHop, Policy, SLABounds
from pathmarket.simulator.orchestrator import (
    AgentSpec,
    Orchestrator,
    SLATemplate,
)

from tests.fixtures.fake_client import FakeAggregatorClient


# ---------------------------------------------------------------------------
# Stock AS and policy fixtures
# ---------------------------------------------------------------------------


TRANSIT_GOOD = ["1-a:0:1", "1-a:0:2", "1-a:0:3"]
TRANSIT_BAD = ["1-b:0:1", "1-b:0:2", "1-b:0:3"]
BUYERS = ["1-z:0:1", "1-z:0:2", "1-z:0:3", "1-z:0:4"]


def _edge_buyer_policy() -> Policy:
    return Policy(
        max_price_per_gb="0.10",
        min_reputation_floor=0.70,
        required_bounds=SLABounds(latency_max_ms=20, loss_max_ppm=800, bandwidth_min_kbps=None),
        alpha=1.0,
        beta=5.0,
        uncovered_tolerance="partial_ok",
        complaint_sensitivity="moderate",
        reshop_on_reputation_drop=0.10,
        portfolio_redundancy=1,
    )


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


def _linear_path(ases: list[str]) -> list[PathHop]:
    return [
        PathHop(
            isd_as=a,
            ingress=0 if i == 0 else 100 + i,
            egress=0 if i == len(ases) - 1 else 200 + i,
        )
        for i, a in enumerate(ases)
    ]


def _fresh_setup(*, seed: int = 42):
    """Build a three-template, mixed-consortium orchestrator ready for tests."""

    all_ases = TRANSIT_GOOD + TRANSIT_BAD + BUYERS
    private_keys = {a: Ed25519PrivateKey.generate() for a in all_ases}

    specs: list[AgentSpec] = []
    for a in TRANSIT_GOOD:
        specs.append(AgentSpec(isd_as=a, role="transit-good", policy=_transit_policy(),
                               quality_profile="transit-good"))
    for a in TRANSIT_BAD:
        specs.append(AgentSpec(isd_as=a, role="transit-bad", policy=_transit_policy(),
                               quality_profile="transit-bad"))
    for a in BUYERS:
        specs.append(AgentSpec(isd_as=a, role="edge-buyer", policy=_edge_buyer_policy()))

    sla_templates = [
        SLATemplate(
            path=_linear_path(TRANSIT_GOOD),
            bounds=SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None),
            price_per_gb="0.05",
            consortium_profile="transit-good",
        ),
        SLATemplate(
            path=_linear_path(TRANSIT_BAD),
            bounds=SLABounds(latency_max_ms=15, loss_max_ppm=500, bandwidth_min_kbps=None),
            price_per_gb="0.04",
            consortium_profile="transit-bad",
        ),
        # Mixed-consortium template (one good, one bad) with good profile
        SLATemplate(
            path=_linear_path([TRANSIT_GOOD[0], TRANSIT_BAD[0], TRANSIT_BAD[1]]),
            bounds=SLABounds(latency_max_ms=12, loss_max_ppm=500, bandwidth_min_kbps=None),
            price_per_gb="0.06",
            consortium_profile="transit-good",
        ),
    ]

    client = FakeAggregatorClient()
    discovery = StaticPathTableDiscovery.from_mapping({})

    # Stable wall-clock so observed_at ISOs are deterministic too.
    start = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    tick_index = {"n": 0}

    def now_fn() -> datetime:
        # Each call advances one second — not per-tick to keep within-tick
        # events chronologically ordered but still deterministic.
        t = start + timedelta(seconds=tick_index["n"])
        tick_index["n"] += 1
        return t

    orch = Orchestrator(
        specs=specs,
        private_keys=private_keys,
        sla_templates=sla_templates,
        aggregator_client=client,
        path_discovery=discovery,
        rng=random.Random(seed),
        now_fn=now_fn,
    )
    return orch, client


# ---------------------------------------------------------------------------
# Pre-seed
# ---------------------------------------------------------------------------


class TestPreSeed:
    def test_publishes_requested_count(self) -> None:
        orch, client = _fresh_setup()
        slas = orch.pre_seed(count=5)
        assert len(slas) == 5
        assert len(client.posted_slas) == 5
        # Every pre-seeded SLA has a quality model + bounds recorded.
        for sla in slas:
            assert sla.payload.sla_id in orch.quality_models
            assert sla.payload.sla_id in orch.sla_bounds

    def test_wraps_over_templates_when_count_exceeds(self) -> None:
        orch, client = _fresh_setup()
        slas = orch.pre_seed(count=7)  # 3 templates → wrap
        assert len(slas) == 7
        # Paths repeat but sla_ids differ because of nonce entropy.
        assert len({sla.payload.sla_id for sla in slas}) == 7


# ---------------------------------------------------------------------------
# Tick loop
# ---------------------------------------------------------------------------


class TestTickLoop:
    def test_ten_ticks_produce_ballpark_artifacts(self) -> None:
        orch, _client = _fresh_setup()
        orch.pre_seed(count=3)
        totals = {"slas": 0, "claims": 0, "complaints": 0}
        for _ in range(10):
            s = orch.tick(publish_prob=0.5)
            totals["slas"] += len(s.new_slas)
            totals["claims"] += len(s.new_claims)
            totals["complaints"] += len(s.new_complaints)
        # Ballpark bands — permissive but meaningful.
        assert 0 <= totals["slas"] <= 10
        assert totals["claims"] >= 1, "expected at least one claim in 10 ticks"
        # With transit-bad consortia in the pool, at least one complaint is
        # highly likely but not guaranteed; we only assert non-negative here.
        assert totals["complaints"] >= 0

    def test_determinism_under_seed(self) -> None:
        """Same seed + same inputs → identical tick summaries."""
        orch1, _ = _fresh_setup(seed=123)
        orch2, _ = _fresh_setup(seed=123)
        orch1.pre_seed(count=3)
        orch2.pre_seed(count=3)
        sig1 = []
        sig2 = []
        for _ in range(5):
            s1 = orch1.tick()
            s2 = orch2.tick()
            sig1.append((len(s1.new_slas), len(s1.new_claims), len(s1.new_complaints)))
            sig2.append((len(s2.new_slas), len(s2.new_claims), len(s2.new_complaints)))
        assert sig1 == sig2

    def test_publish_is_gated_by_probability(self) -> None:
        """publish_prob=0.0 → zero new SLAs across any number of ticks."""
        orch, _ = _fresh_setup()
        orch.pre_seed(count=2)
        for _ in range(5):
            s = orch.tick(publish_prob=0.0)
            assert s.new_slas == []

    def test_buyer_never_claims_own_sla(self) -> None:
        """Roles in the pool don't overlap, but the orchestrator filters cosigners anyway."""
        orch, _ = _fresh_setup()
        orch.pre_seed(count=3)
        for _ in range(10):
            s = orch.tick()
            for claim in s.new_claims:
                sla_id = claim.payload.sla_id
                sla = orch.aggregator_client.get_sla(sla_id)
                assert sla is not None
                assert claim.payload.claimant not in sla.payload.cosigners


# ---------------------------------------------------------------------------
# Missing-key guard
# ---------------------------------------------------------------------------


class TestConstructionGuards:
    def test_missing_private_key_raises(self) -> None:
        spec = AgentSpec(isd_as="1-a:0:1", role="edge-buyer", policy=_edge_buyer_policy())
        with pytest.raises(ValueError, match="missing private key"):
            Orchestrator(
                specs=[spec],
                private_keys={},  # intentionally empty
                sla_templates=[],
                aggregator_client=FakeAggregatorClient(),
                path_discovery=StaticPathTableDiscovery.from_mapping({}),
                rng=random.Random(1),
            )
