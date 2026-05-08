"""Step D3 — ``GET /local/candidate_routes`` tests (``DESIGN.md`` §8.4, §A.5).

Covers: full-coverage single SLA, full-coverage composition of two SLAs,
partial coverage, no-coverage, overlapping-SLA enumeration, and the
aggregate maths (summed latency/loss, min-bandwidth, summed price, min
cosigner reputation for full coverage / None for partial).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from pathmarket.agent.agent import Agent
from pathmarket.path_discovery.static_table import StaticPathTableDiscovery
from pathmarket.schemas import (
    PathHop,
    Policy,
    SLABounds,
    Score,
    SignedClaim,
    SignedSLA,
)
from pathmarket.user_agent.app import UserAgentService, create_app
from pathmarket.user_agent.routing import build_candidate_routes
from pathmarket.user_agent.state_store import UserAgentState

from tests.fixtures.fake_client import FakeAggregatorClient


USER_AS = "1-ff00:0:112"
AS1, AS2, AS3, DST = "1-a:0:1", "1-a:0:2", "1-a:0:3", "1-ff00:0:130"


def _permissive_policy() -> Policy:
    return Policy(
        max_price_per_gb="99.00",
        min_reputation_floor=0.0,
        required_bounds=SLABounds(None, None, None),
        alpha=1.0,
        beta=1.0,
        uncovered_tolerance="anywhere",
        complaint_sensitivity="moderate",
        reshop_on_reputation_drop=1.0,
        portfolio_redundancy=0,
    )


def _publish_sla(
    client: FakeAggregatorClient,
    *,
    cosigners: list[str],
    bounds: SLABounds,
    price: str,
) -> SignedSLA:
    keys = {a: Ed25519PrivateKey.generate() for a in cosigners}
    seller = Agent(
        isd_as=cosigners[0],
        private_key=keys[cosigners[0]],
        policy=_permissive_policy(),
        aggregator_client=client,
        path_discovery=StaticPathTableDiscovery.from_mapping({}),
    )
    path = [
        PathHop(
            isd_as=a,
            ingress=0 if i == 0 else 100 + i,
            egress=0 if i == len(cosigners) - 1 else 200 + i,
        )
        for i, a in enumerate(cosigners)
    ]
    import secrets
    return seller.publish_sla(
        path=path,
        bounds=bounds,
        price_per_gb=price,
        valid_from="2026-04-19T12:00:00Z",
        valid_until="2026-04-20T12:00:00Z",
        nonce=secrets.token_hex(8),
        cosigner_keys=keys,
    )


def _claim(client: FakeAggregatorClient, sla: SignedSLA) -> SignedClaim:
    buyer = Agent(
        isd_as=USER_AS,
        private_key=Ed25519PrivateKey.generate(),
        policy=_permissive_policy(),
        aggregator_client=client,
        path_discovery=StaticPathTableDiscovery.from_mapping({}),
    )
    return buyer.claim_sla(sla, gb=100, claimed_at="2026-04-19T12:00:00Z")


# ---------------------------------------------------------------------------
# Helper builder (bypasses FastAPI)
# ---------------------------------------------------------------------------


def _run(
    client: FakeAggregatorClient,
    portfolio: list[SignedClaim],
    path_hops: list[str],
    scores: dict[str, float] | None = None,
) -> dict[str, Any]:
    # Inject scores into the client.
    for isd_as, sc in (scores or {}).items():
        client._scores[isd_as] = Score(isd_as=isd_as, score=sc, components={})

    path = [
        {"isd_as": a,
         "ingress": 0 if i == 0 else 100 + i,
         "egress": 0 if i == len(path_hops) - 1 else 200 + i}
        for i, a in enumerate(path_hops)
    ]
    disco = StaticPathTableDiscovery.from_mapping({f"{USER_AS} -> {DST}": [path]})
    return build_candidate_routes(
        src_isd_as=USER_AS,
        dst_isd_as=DST,
        portfolio=portfolio,
        path_discovery=disco,
        aggregator_client=client,
    )


# ---------------------------------------------------------------------------
# Single-SLA full coverage
# ---------------------------------------------------------------------------


class TestSingleSLAFullCoverage:
    def test_one_scheme_covers_full_path(self) -> None:
        client = FakeAggregatorClient()
        sla = _publish_sla(
            client,
            cosigners=[AS1, AS2, DST],
            bounds=SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=8_000_000),
            price="0.05",
        )
        c = _claim(client, sla)
        out = _run(client, [c], [AS1, AS2, DST], scores={AS1: 0.9, AS2: 0.8, DST: 0.95})
        assert len(out["candidates"]) == 1
        cov = out["candidates"][0]["coverage_options"]
        assert len(cov) == 1
        agg = cov[0]["aggregate"]
        assert agg["covered_hops"] == agg["total_hops"] == 3
        assert agg["latency_ms"] == 10
        assert agg["loss_ppm"] == 500
        assert agg["bandwidth_kbps"] == 8_000_000
        assert agg["price_per_gb"] == "0.05"
        assert agg["reputation"] == 0.8  # min cosigner score


# ---------------------------------------------------------------------------
# Composition — two SLAs tile the path
# ---------------------------------------------------------------------------


class TestCompositionTwoSLAs:
    def test_schemes_include_the_tiled_composition(self) -> None:
        client = FakeAggregatorClient()
        sla_a = _publish_sla(
            client,
            cosigners=[AS1, AS2],
            bounds=SLABounds(latency_max_ms=7, loss_max_ppm=200, bandwidth_min_kbps=4_000_000),
            price="0.03",
        )
        sla_b = _publish_sla(
            client,
            cosigners=[AS3, DST],
            bounds=SLABounds(latency_max_ms=5, loss_max_ppm=100, bandwidth_min_kbps=8_000_000),
            price="0.02",
        )
        ca, cb = _claim(client, sla_a), _claim(client, sla_b)
        out = _run(
            client, [ca, cb], [AS1, AS2, AS3, DST],
            scores={AS1: 0.9, AS2: 0.85, AS3: 0.8, DST: 0.75},
        )
        cov = out["candidates"][0]["coverage_options"]
        # Must include the tiled full-coverage scheme.
        full = [c for c in cov if c["aggregate"]["covered_hops"] == 4]
        assert full, "expected a full-coverage scheme"
        agg = full[0]["aggregate"]
        assert agg["latency_ms"] == 12  # 7 + 5
        assert agg["loss_ppm"] == 300  # 200 + 100
        assert agg["bandwidth_kbps"] == 4_000_000  # min
        assert agg["price_per_gb"] == "0.05"  # 0.03 + 0.02
        assert agg["reputation"] == 0.75  # min across all cosigners


# ---------------------------------------------------------------------------
# Partial coverage
# ---------------------------------------------------------------------------


class TestPartialCoverage:
    def test_partial_aggregate_has_reputation_none(self) -> None:
        client = FakeAggregatorClient()
        sla = _publish_sla(
            client,
            cosigners=[AS1, AS2],
            bounds=SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None),
            price="0.05",
        )
        c = _claim(client, sla)
        out = _run(
            client, [c], [AS1, AS2, AS3, DST],
            scores={AS1: 0.9, AS2: 0.9},
        )
        cov = out["candidates"][0]["coverage_options"]
        assert len(cov) == 1
        agg = cov[0]["aggregate"]
        assert agg["covered_hops"] == 2
        assert agg["total_hops"] == 4
        assert agg["reputation"] is None  # partial coverage


# ---------------------------------------------------------------------------
# No coverage
# ---------------------------------------------------------------------------


class TestNoCoverage:
    def test_empty_coverage_options_when_no_claim_matches(self) -> None:
        client = FakeAggregatorClient()
        sla = _publish_sla(
            client,
            cosigners=["1-x:0:1", "1-x:0:2"],
            bounds=SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None),
            price="0.05",
        )
        c = _claim(client, sla)
        out = _run(client, [c], [AS1, AS2, DST])
        assert out["candidates"][0]["coverage_options"] == []


# ---------------------------------------------------------------------------
# Overlapping SLAs — each is its own coverage option, no combined overlap
# ---------------------------------------------------------------------------


class TestOverlapping:
    def test_overlapping_slas_produce_separate_options(self) -> None:
        client = FakeAggregatorClient()
        sla_x = _publish_sla(
            client,
            cosigners=[AS1, AS2],
            bounds=SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None),
            price="0.05",
        )
        sla_y = _publish_sla(
            client,
            cosigners=[AS1, AS2],  # same path, different publisher path (different nonce)
            bounds=SLABounds(latency_max_ms=8, loss_max_ppm=400, bandwidth_min_kbps=None),
            price="0.06",
        )
        cx, cy = _claim(client, sla_x), _claim(client, sla_y)
        out = _run(client, [cx, cy], [AS1, AS2])
        cov = out["candidates"][0]["coverage_options"]
        # Exactly two options (one per SLA) — never both together, since they
        # overlap completely on hops [0,2).
        assert len(cov) == 2
        # Each option covers both hops.
        for opt in cov:
            assert opt["aggregate"]["covered_hops"] == 2


# ---------------------------------------------------------------------------
# End-to-end via FastAPI
# ---------------------------------------------------------------------------


class TestEndpoint:
    def test_endpoint_returns_valid_shape(self, tmp_path: Path) -> None:
        client = FakeAggregatorClient()
        sla = _publish_sla(
            client,
            cosigners=[AS1, AS2, DST],
            bounds=SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None),
            price="0.05",
        )
        c = _claim(client, sla)

        path_hops = [AS1, AS2, DST]
        path = [
            {"isd_as": a,
             "ingress": 0 if i == 0 else 100 + i,
             "egress": 0 if i == len(path_hops) - 1 else 200 + i}
            for i, a in enumerate(path_hops)
        ]
        disco = StaticPathTableDiscovery.from_mapping({f"{USER_AS} -> {DST}": [path]})
        state = UserAgentState(
            isd_as=USER_AS, display_name="User", policy=_permissive_policy(),
            portfolio=[c],
        )
        svc = UserAgentService(
            state=state,
            state_file=tmp_path / "s.json",
            aggregator_client=client,
            private_key=Ed25519PrivateKey.generate(),
            path_discovery=disco,
        )
        app = create_app(svc)
        tc = TestClient(app)
        r = tc.get("/local/candidate_routes", params={"dst": DST})
        assert r.status_code == 200
        body = r.json()
        assert body["dst_isd_as"] == DST
        assert len(body["candidates"]) == 1
        assert body["candidates"][0]["coverage_options"][0]["aggregate"]["covered_hops"] == 3
