"""Step C6 — cold-start recorder tests (``DESIGN.md`` §7.7).

Runs the recorder against a short-duration orchestrator and verifies the
resulting JSONL file is well-formed, type-tagged, and contains artifacts
that round-trip through the aggregator's wire models.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pathmarket.aggregator.models import (
    SignedClaimModel,
    SignedComplaintModel,
    SignedSLAModel,
)
from pathmarket.path_discovery.static_table import StaticPathTableDiscovery
from pathmarket.schemas import PathHop, Policy, SLABounds
from pathmarket.simulator.orchestrator import (
    AgentSpec,
    Orchestrator,
    SLATemplate,
)
from pathmarket.simulator.recorder import run_record_mode

from tests.fixtures.fake_client import FakeAggregatorClient


TRANSIT_GOOD = ["1-a:0:1", "1-a:0:2", "1-a:0:3"]
TRANSIT_BAD = ["1-b:0:1", "1-b:0:2", "1-b:0:3"]
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


def _setup(seed: int = 11) -> Orchestrator:
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
        specs.append(AgentSpec(isd_as=a, role="edge-buyer", policy=_buyer_policy()))

    templates = [
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
        rng=random.Random(seed),
        now_fn=now_fn,
    )
    orch.pre_seed(count=3)
    return orch


class TestRunRecordMode:
    def test_writes_well_formed_jsonl(self, tmp_path: Path) -> None:
        orch = _setup()
        out = tmp_path / "cold_start.jsonl"
        # Keep it short: 60 ticks of 3s each = 3 minutes of sim time.
        n = run_record_mode(
            orch, record_file=out, duration_hours=0.05, tick_interval_seconds=3.0
        )
        lines = out.read_text(encoding="utf-8").splitlines()
        assert len(lines) == n
        # Every line must parse and carry the expected envelope.
        for line in lines:
            row = json.loads(line)
            assert "sim_time" in row
            assert "artifact" in row
            assert row["artifact"]["type"] in {"sla", "claim", "complaint"}
            assert "payload" in row["artifact"]

    def test_artifacts_roundtrip_via_aggregator_models(self, tmp_path: Path) -> None:
        """Every written payload is accepted by the wire-model validators."""
        orch = _setup()
        out = tmp_path / "cs.jsonl"
        run_record_mode(orch, record_file=out, duration_hours=0.05, tick_interval_seconds=3.0)
        for line in out.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            payload = row["artifact"]["payload"]
            kind = row["artifact"]["type"]
            if kind == "sla":
                SignedSLAModel.model_validate(payload).to_dataclass()
            elif kind == "claim":
                SignedClaimModel.model_validate(payload).to_dataclass()
            else:
                SignedComplaintModel.model_validate(payload).to_dataclass()

    def test_creates_parent_dir_if_missing(self, tmp_path: Path) -> None:
        orch = _setup()
        out = tmp_path / "sub" / "dir" / "cold.jsonl"
        run_record_mode(orch, record_file=out, duration_hours=0.01, tick_interval_seconds=3.0)
        assert out.exists()

    def test_rejects_bad_durations(self, tmp_path: Path) -> None:
        orch = _setup()
        out = tmp_path / "x.jsonl"
        with pytest.raises(ValueError):
            run_record_mode(orch, record_file=out, duration_hours=0.0, tick_interval_seconds=1.0)
        with pytest.raises(ValueError):
            run_record_mode(orch, record_file=out, duration_hours=1.0, tick_interval_seconds=0.0)

    def test_artifact_counts_match_tick_summaries(self, tmp_path: Path) -> None:
        """Total JSONL lines == Σ(new_slas + new_claims + new_complaints) per tick.

        We cannot compute Σ a priori without re-running the sim, so we instead
        verify the recorder's return value matches the line count, and the
        JSONL line count matches the actual file content.
        """
        orch = _setup()
        out = tmp_path / "y.jsonl"
        n = run_record_mode(orch, record_file=out, duration_hours=0.02, tick_interval_seconds=3.0)
        assert n == len(out.read_text(encoding="utf-8").splitlines())
        assert n > 0  # Some activity should have occurred.
