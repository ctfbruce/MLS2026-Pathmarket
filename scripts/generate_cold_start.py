"""Generate ``assets/cold_start.jsonl`` for the UI pitch opener (§7.7, §9.5).

Runs a seeded in-memory orchestrator for 3 simulated hours at 3s/tick and
writes every emitted signed artifact to the JSONL file. The aggregator is
*not* exercised here: the recorder consumes the orchestrator's tick
summaries directly. The output is a pre-generated asset — the live demo
never regenerates it.

This script is deterministic under the embedded seed and does not depend on
any runtime config files, so it can be re-run to refresh the artifact after
schema changes.
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pathmarket.path_discovery.static_table import StaticPathTableDiscovery
from pathmarket.schemas import PathHop, Policy, SLABounds
from pathmarket.simulator.orchestrator import (
    AgentSpec,
    Orchestrator,
    SLATemplate,
)
from pathmarket.simulator.recorder import run_record_mode


TRANSIT_GOOD = [f"1-ff00:0:{100+i}" for i in range(6)]
TRANSIT_BAD = [f"1-ff00:0:{200+i}" for i in range(4)]
BUYERS = [f"1-ff00:0:{300+i}" for i in range(8)]

SEED = 20260419


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


def _buyer_policy(*, strict: bool) -> Policy:
    if strict:
        # Hospital-like
        return Policy(
            max_price_per_gb="0.20",
            min_reputation_floor=0.70,
            required_bounds=SLABounds(latency_max_ms=15, loss_max_ppm=600, bandwidth_min_kbps=None),
            alpha=3.0,
            beta=1.0,
            uncovered_tolerance="partial_ok",
            complaint_sensitivity="strict",
            reshop_on_reputation_drop=0.05,
            portfolio_redundancy=2,
        )
    # Cloud-like
    return Policy(
        max_price_per_gb="0.08",
        min_reputation_floor=0.50,
        required_bounds=SLABounds(latency_max_ms=30, loss_max_ppm=1_500, bandwidth_min_kbps=None),
        alpha=1.0,
        beta=6.0,
        uncovered_tolerance="partial_ok",
        complaint_sensitivity="tolerant",
        reshop_on_reputation_drop=0.20,
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


def _build_orchestrator(*, seed: int) -> Orchestrator:
    all_ases = TRANSIT_GOOD + TRANSIT_BAD + BUYERS
    private_keys = {a: Ed25519PrivateKey.generate() for a in all_ases}

    specs: list[AgentSpec] = []
    for a in TRANSIT_GOOD:
        specs.append(AgentSpec(isd_as=a, role="transit-good", policy=_transit_policy(),
                               quality_profile="transit-good"))
    for a in TRANSIT_BAD:
        specs.append(AgentSpec(isd_as=a, role="transit-bad", policy=_transit_policy(),
                               quality_profile="transit-bad"))
    for idx, a in enumerate(BUYERS):
        specs.append(AgentSpec(isd_as=a, role="edge-buyer",
                               policy=_buyer_policy(strict=idx % 3 == 0)))

    templates = [
        SLATemplate(
            path=_linear_path(TRANSIT_GOOD[:3]),
            bounds=SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None),
            price_per_gb="0.06",
            consortium_profile="transit-good",
        ),
        SLATemplate(
            path=_linear_path(TRANSIT_GOOD[3:]),
            bounds=SLABounds(latency_max_ms=12, loss_max_ppm=400, bandwidth_min_kbps=None),
            price_per_gb="0.07",
            consortium_profile="transit-good",
        ),
        SLATemplate(
            path=_linear_path(TRANSIT_BAD),
            bounds=SLABounds(latency_max_ms=18, loss_max_ppm=800, bandwidth_min_kbps=None),
            price_per_gb="0.035",
            consortium_profile="transit-bad",
        ),
        SLATemplate(
            path=_linear_path([TRANSIT_GOOD[0], TRANSIT_BAD[0], TRANSIT_GOOD[4]]),
            bounds=SLABounds(latency_max_ms=15, loss_max_ppm=500, bandwidth_min_kbps=None),
            price_per_gb="0.05",
            consortium_profile="transit-good",
        ),
    ]

    from pathmarket.simulator.orchestrator import Orchestrator as _O  # local re-import for clarity

    # Deterministic wall-clock: tick-index-driven so the resulting JSONL is
    # reproducible byte-for-byte under a fixed seed.
    start = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    counter = {"n": 0}

    def now_fn() -> datetime:
        t = start + timedelta(seconds=counter["n"])
        counter["n"] += 1
        return t

    orch = _O(
        specs=specs,
        private_keys=private_keys,
        sla_templates=templates,
        aggregator_client=_NullClient(),
        path_discovery=StaticPathTableDiscovery.from_mapping({}),
        rng=random.Random(seed),
        now_fn=now_fn,
    )
    orch.pre_seed(count=8)
    return orch


class _NullClient:
    """Minimal AggregatorClient stand-in — we only need it to satisfy the
    orchestrator's publish/claim/complaint posts; the recorder consumes the
    tick summaries directly, not the client's buffered posts."""

    def __init__(self) -> None:
        self._slas: dict[str, object] = {}

    def list_active_slas(self):
        return list(self._slas.values())

    def get_sla(self, sla_id):
        return self._slas.get(sla_id)

    def get_scores(self):
        return []

    def get_score(self, isd_as):
        from pathmarket.schemas import Score
        return Score(isd_as=isd_as, score=1.0, components={})

    def post_sla(self, sla):
        self._slas[sla.payload.sla_id] = sla
        return {"status": "accepted"}

    def post_claim(self, claim):
        return {"status": "accepted"}

    def post_complaint(self, complaint):
        return {"status": "accepted"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("assets/cold_start.jsonl"))
    parser.add_argument("--duration-hours", type=float, default=3.0)
    parser.add_argument("--tick-seconds", type=float, default=3.0)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    orch = _build_orchestrator(seed=args.seed)
    n = run_record_mode(
        orch,
        record_file=args.out,
        duration_hours=args.duration_hours,
        tick_interval_seconds=args.tick_seconds,
    )
    print(f"wrote {n} artifacts to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
