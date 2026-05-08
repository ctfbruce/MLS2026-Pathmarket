"""Step C1 — agent + policy tests (``DESIGN.md`` §7.1, §7.2, §4.11).

Coverage:
- Policy helpers (``complaint_multiplier``, ``should_file_complaint``,
  ``policy_accepts_sla``, ``sla_utility``, ``choose_best_sla``,
  ``compose_bounds``).
- Agent methods (``publish_sla``, ``claim_sla``, ``file_complaint``,
  ``choose_route``).
- **Personas distinguish**: Hospital (§7.3) and Cloud (§7.3) policies
  applied to the same market yield visibly different choices.
"""

from __future__ import annotations

from pathmarket.agent.agent import Agent
from pathmarket.agent.policy import (
    choose_best_sla,
    complaint_multiplier,
    compose_bounds,
    mean_cosigner_score,
    min_cosigner_score,
    policy_accepts_sla,
    should_file_complaint,
    sla_utility,
)
from pathmarket.path_discovery.static_table import StaticPathTableDiscovery
from pathmarket.schemas import PathHop, Policy, SLABounds

from tests.fixtures.crypto import make_test_key
from tests.fixtures.fake_client import FakeAggregatorClient
from tests.fixtures.signing import build_signed_sla, make_keys_for


# ---------------------------------------------------------------------------
# Policy fixtures
# ---------------------------------------------------------------------------


def _hospital_policy() -> Policy:
    return Policy(
        max_price_per_gb="0.20",
        min_reputation_floor=0.90,
        required_bounds=SLABounds(latency_max_ms=8, loss_max_ppm=100, bandwidth_min_kbps=2_000_000),
        alpha=3.0,
        beta=1.0,
        uncovered_tolerance="never",
        complaint_sensitivity="strict",
        reshop_on_reputation_drop=0.05,
        portfolio_redundancy=2,
    )


def _cloud_policy() -> Policy:
    return Policy(
        max_price_per_gb="0.030",
        min_reputation_floor=0.70,
        required_bounds=SLABounds(latency_max_ms=30, loss_max_ppm=1_000, bandwidth_min_kbps=8_000_000),
        alpha=1.0,
        beta=10.0,
        uncovered_tolerance="partial_ok",
        complaint_sensitivity="tolerant",
        reshop_on_reputation_drop=0.25,
        portfolio_redundancy=1,
    )


# ---------------------------------------------------------------------------
# Complaint-threshold helpers
# ---------------------------------------------------------------------------


class TestComplaintThresholds:
    def test_multipliers(self) -> None:
        assert complaint_multiplier("strict") == 1.10
        assert complaint_multiplier("moderate") == 1.25
        assert complaint_multiplier("tolerant") == 1.75

    def test_latency_threshold(self) -> None:
        p = _hospital_policy()
        # strict: bound * 1.10. bound=8 → threshold 8.8.
        assert should_file_complaint(p, "latency_ms", bound=8, measured=9) is True
        assert should_file_complaint(p, "latency_ms", bound=8, measured=8) is False
        assert should_file_complaint(p, "latency_ms", bound=8, measured=88) is True

    def test_bandwidth_threshold(self) -> None:
        p = _hospital_policy()
        # strict: bound / 1.10. bound=2M → threshold ≈ 1.818M. measured < threshold → complain.
        assert should_file_complaint(p, "bandwidth_kbps", bound=2_000_000, measured=1_000_000) is True
        assert should_file_complaint(p, "bandwidth_kbps", bound=2_000_000, measured=1_900_000) is False

    def test_tolerant_vs_strict_boundary(self) -> None:
        """A measurement at 1.5× bound: strict complains, tolerant does not."""
        strict = _hospital_policy()
        tolerant = _cloud_policy()
        bound, measured = 10, 15  # 1.5× bound
        assert should_file_complaint(strict, "latency_ms", bound=bound, measured=measured) is True
        assert should_file_complaint(tolerant, "latency_ms", bound=bound, measured=measured) is False

    def test_no_bound_never_complains(self) -> None:
        assert should_file_complaint(_hospital_policy(), "latency_ms", bound=None, measured=999) is False


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


class TestAcceptance:
    def test_price_gate(self) -> None:
        cheap_keys = make_keys_for(["1-a:0:1", "1-a:0:2"])
        sla = build_signed_sla(
            cheap_keys,
            cosigners=["1-a:0:1", "1-a:0:2"],
            price_per_gb="0.25",  # above hospital's 0.20 max
            bounds=SLABounds(latency_max_ms=5, loss_max_ppm=50, bandwidth_min_kbps=4_000_000),
        )
        assert not policy_accepts_sla(_hospital_policy(), sla, min_cosigner_score=1.0)
        # Same SLA, lower price: accepted.
        sla_cheap = build_signed_sla(
            cheap_keys,
            cosigners=["1-a:0:1", "1-a:0:2"],
            price_per_gb="0.05",
            bounds=SLABounds(latency_max_ms=5, loss_max_ppm=50, bandwidth_min_kbps=4_000_000),
        )
        assert policy_accepts_sla(_hospital_policy(), sla_cheap, min_cosigner_score=1.0)

    def test_reputation_gate(self) -> None:
        keys = make_keys_for(["1-a:0:1", "1-a:0:2"])
        sla = build_signed_sla(
            keys,
            cosigners=["1-a:0:1", "1-a:0:2"],
            price_per_gb="0.05",
            bounds=SLABounds(latency_max_ms=5, loss_max_ppm=50, bandwidth_min_kbps=4_000_000),
        )
        # Below 0.90 rep floor.
        assert not policy_accepts_sla(_hospital_policy(), sla, min_cosigner_score=0.85)
        # Above floor.
        assert policy_accepts_sla(_hospital_policy(), sla, min_cosigner_score=0.95)

    def test_bounds_gate(self) -> None:
        keys = make_keys_for(["1-a:0:1", "1-a:0:2"])
        # Latency worse than hospital requires (9 > 8).
        sla = build_signed_sla(
            keys,
            cosigners=["1-a:0:1", "1-a:0:2"],
            price_per_gb="0.05",
            bounds=SLABounds(latency_max_ms=9, loss_max_ppm=50, bandwidth_min_kbps=4_000_000),
        )
        assert not policy_accepts_sla(_hospital_policy(), sla, min_cosigner_score=1.0)

    def test_bandwidth_unset_rejects_when_required(self) -> None:
        keys = make_keys_for(["1-a:0:1", "1-a:0:2"])
        sla = build_signed_sla(
            keys,
            cosigners=["1-a:0:1", "1-a:0:2"],
            price_per_gb="0.05",
            bounds=SLABounds(latency_max_ms=5, loss_max_ppm=50, bandwidth_min_kbps=None),
        )
        assert not policy_accepts_sla(_hospital_policy(), sla, min_cosigner_score=1.0)


# ---------------------------------------------------------------------------
# Utility and ranking
# ---------------------------------------------------------------------------


class TestUtilityAndChoice:
    def test_sla_utility_formula(self) -> None:
        keys = make_keys_for(["1-a:0:1", "1-a:0:2"])
        sla = build_signed_sla(keys, cosigners=["1-a:0:1", "1-a:0:2"], price_per_gb="0.10")
        # alpha=3, mean_rep=0.95, beta=1, price=0.10 → 3*0.95 - 1*0.10 = 2.75
        assert abs(sla_utility(_hospital_policy(), sla, mean_cosigner_score=0.95) - 2.75) < 1e-9

    def test_choose_best_sla_prefers_high_rep_under_hospital(self) -> None:
        """Hospital: alpha dominates → picks SLA with highest cosigner reputation."""
        keys = make_keys_for(
            ["1-a:0:1", "1-a:0:2", "1-b:0:1", "1-b:0:2"]
        )
        premium_cosigners = ["1-a:0:1", "1-a:0:2"]
        cheap_cosigners = ["1-b:0:1", "1-b:0:2"]
        premium = build_signed_sla(
            {k: keys[k] for k in premium_cosigners},
            cosigners=premium_cosigners,
            price_per_gb="0.12",
            bounds=SLABounds(latency_max_ms=5, loss_max_ppm=50, bandwidth_min_kbps=4_000_000),
            nonce="premium",
        )
        cheap = build_signed_sla(
            {k: keys[k] for k in cheap_cosigners},
            cosigners=cheap_cosigners,
            price_per_gb="0.05",
            bounds=SLABounds(latency_max_ms=5, loss_max_ppm=50, bandwidth_min_kbps=4_000_000),
            nonce="cheap",
        )
        scores = {a: 0.99 for a in premium_cosigners} | {a: 0.92 for a in cheap_cosigners}
        pick = choose_best_sla(_hospital_policy(), [premium, cheap], scores)
        assert pick is not None
        assert pick.payload.cosigners == premium_cosigners

    def test_cloud_rejects_premium_on_price_gate(self) -> None:
        """Cloud: ``max_price_per_gb == 0.030`` excludes the premium SLA entirely."""
        keys = make_keys_for(["1-a:0:1", "1-a:0:2", "1-b:0:1", "1-b:0:2"])
        premium_cosigners = ["1-a:0:1", "1-a:0:2"]
        cheap_cosigners = ["1-b:0:1", "1-b:0:2"]
        bounds = SLABounds(latency_max_ms=20, loss_max_ppm=800, bandwidth_min_kbps=8_000_000)
        premium = build_signed_sla(
            {k: keys[k] for k in premium_cosigners},
            cosigners=premium_cosigners, price_per_gb="0.050", bounds=bounds, nonce="p",
        )
        cheap = build_signed_sla(
            {k: keys[k] for k in cheap_cosigners},
            cosigners=cheap_cosigners, price_per_gb="0.015", bounds=bounds, nonce="c",
        )
        scores = {a: 0.99 for a in premium_cosigners} | {a: 0.72 for a in cheap_cosigners}
        pick = choose_best_sla(_cloud_policy(), [premium, cheap], scores)
        assert pick is not None
        assert pick.payload.cosigners == cheap_cosigners

    def test_cloud_picks_cheaper_within_acceptable(self) -> None:
        """Cloud: both SLAs priced within the 0.030 cap → beta swings to cheaper."""
        keys = make_keys_for(["1-a:0:1", "1-a:0:2", "1-b:0:1", "1-b:0:2"])
        pricey_cos = ["1-a:0:1", "1-a:0:2"]
        cheap_cos = ["1-b:0:1", "1-b:0:2"]
        bounds = SLABounds(latency_max_ms=20, loss_max_ppm=800, bandwidth_min_kbps=8_000_000)
        pricey = build_signed_sla(
            {k: keys[k] for k in pricey_cos},
            cosigners=pricey_cos, price_per_gb="0.028", bounds=bounds, nonce="p",
        )
        cheap = build_signed_sla(
            {k: keys[k] for k in cheap_cos},
            cosigners=cheap_cos, price_per_gb="0.010", bounds=bounds, nonce="c",
        )
        # Reps equal → beta dominates → cheap wins.
        scores = {a: 0.90 for a in pricey_cos + cheap_cos}
        pick = choose_best_sla(_cloud_policy(), [pricey, cheap], scores)
        assert pick is not None
        assert pick.payload.cosigners == cheap_cos

    def test_choose_best_sla_returns_none_when_nothing_acceptable(self) -> None:
        """Hospital with a market of only cheap-and-loose SLAs gets nothing."""
        keys = make_keys_for(["1-b:0:1", "1-b:0:2"])
        loose = build_signed_sla(
            keys, cosigners=["1-b:0:1", "1-b:0:2"],
            price_per_gb="0.05",
            bounds=SLABounds(latency_max_ms=25, loss_max_ppm=800, bandwidth_min_kbps=4_000_000),
        )
        assert choose_best_sla(_hospital_policy(), [loose], {}) is None


# ---------------------------------------------------------------------------
# Personas diverge on the same market (§7.3)
# ---------------------------------------------------------------------------


class TestPersonasDiverge:
    def test_hospital_and_cloud_choose_differently(self) -> None:
        """The point of personas: same market, different picks."""
        # Two SLAs on different paths. Premium: expensive, strict, high rep.
        # Commodity: cheap, looser bounds, medium rep.
        premium_cos = ["1-a:0:1", "1-a:0:2"]
        commodity_cos = ["1-c:0:1", "1-c:0:2"]
        keys = make_keys_for(premium_cos + commodity_cos)
        premium = build_signed_sla(
            {k: keys[k] for k in premium_cos},
            cosigners=premium_cos,
            price_per_gb="0.15",
            bounds=SLABounds(latency_max_ms=5, loss_max_ppm=50, bandwidth_min_kbps=4_000_000),
            nonce="pA",
        )
        commodity = build_signed_sla(
            {k: keys[k] for k in commodity_cos},
            cosigners=commodity_cos,
            price_per_gb="0.020",
            bounds=SLABounds(latency_max_ms=25, loss_max_ppm=800, bandwidth_min_kbps=8_000_000),
            nonce="cA",
        )
        scores = {a: 0.99 for a in premium_cos} | {a: 0.78 for a in commodity_cos}

        hospital_pick = choose_best_sla(_hospital_policy(), [premium, commodity], scores)
        cloud_pick = choose_best_sla(_cloud_policy(), [premium, commodity], scores)
        assert hospital_pick is not None
        assert cloud_pick is not None
        assert hospital_pick.payload.sla_id != cloud_pick.payload.sla_id
        # Hospital grabs the premium; Cloud grabs the commodity.
        assert hospital_pick.payload.cosigners == premium_cos
        assert cloud_pick.payload.cosigners == commodity_cos


# ---------------------------------------------------------------------------
# compose_bounds
# ---------------------------------------------------------------------------


class TestComposeBounds:
    def test_empty(self) -> None:
        b = compose_bounds([])
        assert b == SLABounds(latency_max_ms=None, loss_max_ppm=None, bandwidth_min_kbps=None)

    def test_sums_latency_and_loss_mins_bandwidth(self) -> None:
        hop_a = SLABounds(latency_max_ms=5, loss_max_ppm=100, bandwidth_min_kbps=4_000_000)
        hop_b = SLABounds(latency_max_ms=7, loss_max_ppm=200, bandwidth_min_kbps=2_000_000)
        agg = compose_bounds([hop_a, hop_b])
        assert agg.latency_max_ms == 12
        assert agg.loss_max_ppm == 300
        assert agg.bandwidth_min_kbps == 2_000_000

    def test_unknown_propagates(self) -> None:
        hop_a = SLABounds(latency_max_ms=5, loss_max_ppm=None, bandwidth_min_kbps=4_000_000)
        hop_b = SLABounds(latency_max_ms=7, loss_max_ppm=200, bandwidth_min_kbps=2_000_000)
        agg = compose_bounds([hop_a, hop_b])
        assert agg.loss_max_ppm is None


# ---------------------------------------------------------------------------
# Agent action methods
# ---------------------------------------------------------------------------


def _linear_path(cosigners: list[str]) -> list[PathHop]:
    hops: list[PathHop] = []
    for i, a in enumerate(cosigners):
        hops.append(
            PathHop(
                isd_as=a,
                ingress=0 if i == 0 else 100 + i,
                egress=0 if i == len(cosigners) - 1 else 200 + i,
            )
        )
    return hops


def _make_agent(isd_as: str, policy: Policy, *, discovery=None, client=None) -> Agent:
    key = make_test_key(isd_as)
    return Agent(
        isd_as=isd_as,
        private_key=key.private_key,
        policy=policy,
        aggregator_client=client or FakeAggregatorClient(),
        path_discovery=discovery or StaticPathTableDiscovery.from_mapping({}),
    )


class TestAgentPublishSla:
    def test_signs_and_posts(self) -> None:
        cos = ["1-a:0:1", "1-a:0:2", "1-a:0:3"]
        keys = make_keys_for(cos)
        client = FakeAggregatorClient()
        agent = Agent(
            isd_as="1-a:0:1",
            private_key=keys["1-a:0:1"].private_key,
            policy=_hospital_policy(),
            aggregator_client=client,
            path_discovery=StaticPathTableDiscovery.from_mapping({}),
        )
        sla = agent.publish_sla(
            path=_linear_path(cos),
            bounds=SLABounds(latency_max_ms=7, loss_max_ppm=80, bandwidth_min_kbps=3_000_000),
            price_per_gb="0.10",
            valid_from="2026-04-19T12:00:00Z",
            valid_until="2026-04-20T12:00:00Z",
            nonce="n0",
            cosigner_keys={
                "1-a:0:2": keys["1-a:0:2"].private_key,
                "1-a:0:3": keys["1-a:0:3"].private_key,
            },
        )
        assert sla.payload.sla_id.startswith("sha256:")
        assert sla.payload.cosigners == cos
        assert len(sla.signatures) == 3
        assert len(client.posted_slas) == 1

    def test_missing_cosigner_key_raises(self) -> None:
        cos = ["1-a:0:1", "1-a:0:2"]
        keys = make_keys_for(cos)
        agent = Agent(
            isd_as="1-a:0:1",
            private_key=keys["1-a:0:1"].private_key,
            policy=_hospital_policy(),
            aggregator_client=FakeAggregatorClient(),
            path_discovery=StaticPathTableDiscovery.from_mapping({}),
        )
        import pytest
        with pytest.raises(ValueError, match="missing cosigner key"):
            agent.publish_sla(
                path=_linear_path(cos),
                bounds=SLABounds(latency_max_ms=5, loss_max_ppm=50, bandwidth_min_kbps=None),
                price_per_gb="0.05",
                valid_from="2026-04-19T12:00:00Z",
                valid_until="2026-04-20T12:00:00Z",
                nonce="x",
                cosigner_keys={},  # missing 1-a:0:2
            )


class TestAgentClaim:
    def test_signs_posts_appends_portfolio(self) -> None:
        cos = ["1-a:0:1", "1-a:0:2"]
        keys = make_keys_for(cos)
        sla = build_signed_sla(keys, cosigners=cos, price_per_gb="0.04")
        client = FakeAggregatorClient(slas=[sla])
        agent = _make_agent("1-z:0:9", _cloud_policy(), client=client)
        claim = agent.claim_sla(sla, gb=250)
        assert claim.payload.claim_id.startswith("sha256:")
        assert claim.payload.claimant == "1-z:0:9"
        assert claim.payload.price_paid_chf == "10.00"  # Decimal("0.04") * Decimal("250")
        assert len(client.posted_claims) == 1
        assert agent.portfolio == [claim]

    def test_self_dealing_raises(self) -> None:
        # A non-source cosigner attempting to claim raises. The source AS
        # (cosigners[0]) is allowed — covered by test_source_cosigner_claims.
        cos = ["1-a:0:1", "1-a:0:2"]
        keys = make_keys_for(cos)
        sla = build_signed_sla(keys, cosigners=cos)
        agent = _make_agent("1-a:0:2", _cloud_policy())
        import pytest
        with pytest.raises(ValueError, match="self-dealing"):
            agent.claim_sla(sla, gb=10)

    def test_source_cosigner_claims(self) -> None:
        # The first cosigner (path source) is a legitimate buyer.
        cos = ["1-a:0:1", "1-a:0:2"]
        keys = make_keys_for(cos)
        sla = build_signed_sla(keys, cosigners=cos)
        agent = _make_agent("1-a:0:1", _cloud_policy())
        claim = agent.claim_sla(sla, gb=10)
        assert claim.payload.claimant == "1-a:0:1"


class TestAgentFileComplaint:
    def test_signs_and_posts(self) -> None:
        cos = ["1-a:0:1", "1-a:0:2"]
        keys = make_keys_for(cos)
        sla = build_signed_sla(keys, cosigners=cos)
        client = FakeAggregatorClient()
        agent = _make_agent("1-z:0:9", _hospital_policy(), client=client)
        c = agent.file_complaint(
            sla,
            metric="latency_ms",
            measured_value=42,
            observed_at="2026-04-19T13:00:00Z",
            note="p95 latency exceeded bound for 6 consecutive minutes",
        )
        assert c.payload.complaint_id.startswith("sha256:")
        assert c.payload.complainant == "1-z:0:9"
        assert c.payload.note.startswith("p95 latency")
        assert len(client.posted_complaints) == 1

    def test_cosigner_cannot_complain_about_own_sla(self) -> None:
        cos = ["1-a:0:1", "1-a:0:2"]
        keys = make_keys_for(cos)
        sla = build_signed_sla(keys, cosigners=cos)
        agent = _make_agent("1-a:0:1", _hospital_policy())
        import pytest
        with pytest.raises(ValueError, match="cannot be a cosigner"):
            agent.file_complaint(sla, metric="latency_ms", measured_value=50, observed_at="2026-04-19T13:00:00Z")


class TestAgentChooseRoute:
    def test_picks_policy_best_sla_on_enumerated_path(self) -> None:
        src = "1-z:0:9"
        dst = "1-a:0:3"
        cos = ["1-z:0:9", "1-a:0:2", "1-a:0:3"]  # source-first hops
        # Actually the user agent shouldn't sign a transit SLA covering itself
        # as a cosigner, but choose_route only *matches* candidate_slas by path
        # equality, so we build an SLA cosigned by transits plus dst.
        transit_cos = ["1-t:0:1", "1-a:0:2", "1-a:0:3"]
        keys = make_keys_for(transit_cos)
        sla_path = _linear_path(transit_cos)
        sla = build_signed_sla(
            keys, cosigners=transit_cos, path=sla_path,
            bounds=SLABounds(latency_max_ms=5, loss_max_ppm=50, bandwidth_min_kbps=4_000_000),
            price_per_gb="0.08",
        )
        # Path discovery must return a path matching the SLA's path.
        discovery = StaticPathTableDiscovery(
            table={(src, dst): [sla_path]}
        )
        client = FakeAggregatorClient(slas=[sla])
        agent = _make_agent(src, _hospital_policy(), discovery=discovery, client=client)
        decision = agent.choose_route(dst)
        assert decision is not None
        assert decision.dst_isd_as == dst
        assert decision.path == sla_path
        assert decision.manual_override is False

    def test_returns_none_when_no_acceptable_sla(self) -> None:
        src, dst = "1-z:0:9", "1-a:0:3"
        transit_cos = ["1-t:0:1", "1-a:0:2", "1-a:0:3"]
        keys = make_keys_for(transit_cos)
        path = _linear_path(transit_cos)
        loose = build_signed_sla(
            keys, cosigners=transit_cos, path=path,
            bounds=SLABounds(latency_max_ms=30, loss_max_ppm=1_000, bandwidth_min_kbps=None),
            price_per_gb="0.10",
        )
        discovery = StaticPathTableDiscovery(table={(src, dst): [path]})
        client = FakeAggregatorClient(slas=[loose])
        agent = _make_agent(src, _hospital_policy(), discovery=discovery, client=client)
        assert agent.choose_route(dst) is None

    def test_no_paths_returns_none(self) -> None:
        agent = _make_agent(
            "1-z:0:9",
            _cloud_policy(),
            discovery=StaticPathTableDiscovery.from_mapping({}),
        )
        assert agent.choose_route("1-a:0:1") is None


# ---------------------------------------------------------------------------
# Sanity: min / mean cosigner score helpers
# ---------------------------------------------------------------------------


class TestCosignerScoreHelpers:
    def test_min_and_mean(self) -> None:
        keys = make_keys_for(["1-a:0:1", "1-a:0:2"])
        sla = build_signed_sla(keys, cosigners=["1-a:0:1", "1-a:0:2"])
        scores = {"1-a:0:1": 0.9, "1-a:0:2": 0.6}
        assert min_cosigner_score(sla, scores) == 0.6
        assert abs(mean_cosigner_score(sla, scores) - 0.75) < 1e-9

    def test_missing_defaults_to_one(self) -> None:
        keys = make_keys_for(["1-a:0:1", "1-a:0:2"])
        sla = build_signed_sla(keys, cosigners=["1-a:0:1", "1-a:0:2"])
        assert min_cosigner_score(sla, {}) == 1.0
        assert mean_cosigner_score(sla, {}) == 1.0
