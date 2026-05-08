"""Step D1 — user-AS state store tests (``DESIGN.md`` §8.3, §13 D1).

Round-trip, corruption recovery, schema-version guard, and atomic-write
behavior of :mod:`pathmarket.user_agent.state_store`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pathmarket.agent.agent import Agent
from pathmarket.canonical import iso8601_utc_now
from pathmarket.path_discovery.static_table import StaticPathTableDiscovery
from pathmarket.schemas import (
    PathHop,
    Policy,
    RoutingDecision,
    SLABounds,
    SignedSLA,
    SLAPayload,
)
from pathmarket.user_agent.state_store import (
    SCHEMA_VERSION,
    UserAgentState,
    load_or_init_state,
    save_state,
)

from tests.fixtures.fake_client import FakeAggregatorClient


# ---------------------------------------------------------------------------
# Helpers: build a realistic Policy, SignedSLA, then a SignedClaim so we have
# something non-trivial to round-trip through the store.
# ---------------------------------------------------------------------------


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


def _make_signed_claim():
    """Build a valid SignedSLA + SignedClaim via a throw-away Agent+FakeClient."""

    user_as = "1-ff00:0:112"
    transit = ["1-a:0:1", "1-a:0:2"]
    user_key = Ed25519PrivateKey.generate()
    transit_keys = {a: Ed25519PrivateKey.generate() for a in transit}

    client = FakeAggregatorClient()
    disc = StaticPathTableDiscovery.from_mapping({})
    # We use a seller Agent (one of the transit ASes) to publish the SLA.
    seller = Agent(
        isd_as=transit[0],
        private_key=transit_keys[transit[0]],
        policy=_policy(),
        aggregator_client=client,
        path_discovery=disc,
    )
    path = [
        PathHop(isd_as=transit[0], ingress=0, egress=201),
        PathHop(isd_as=transit[1], ingress=101, egress=0),
    ]
    now_dt = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    now_iso = "2026-04-19T12:00:00Z"
    sla = seller.publish_sla(
        path=path,
        bounds=SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None),
        price_per_gb="0.05",
        valid_from="2026-04-19T12:00:00Z",
        valid_until="2026-04-20T12:00:00Z",
        nonce="deadbeefdeadbeef",
        cosigner_keys=transit_keys,
    )

    buyer = Agent(
        isd_as=user_as,
        private_key=user_key,
        policy=_policy(),
        aggregator_client=client,
        path_discovery=disc,
    )
    claim = buyer.claim_sla(sla, gb=500, claimed_at=now_iso)
    return sla, claim


def _state_with_one_claim() -> UserAgentState:
    sla, claim = _make_signed_claim()
    return UserAgentState(
        isd_as="1-ff00:0:112",
        display_name="Zurich-Financial-Edge",
        policy=_policy(),
        portfolio=[claim],
        routing_decisions={
            "1-ff00:0:130": RoutingDecision(
                dst_isd_as="1-ff00:0:130",
                path=sla.payload.path,
                applied_claims={0: claim.payload.claim_id, 1: claim.payload.claim_id},
                updated_at=iso8601_utc_now(),
                manual_override=True,
            )
        },
    )


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_fresh_default_when_missing(self, tmp_path: Path) -> None:
        p = tmp_path / "state.json"
        s = load_or_init_state(
            p,
            isd_as="1-ff00:0:112",
            display_name="Zurich-Financial-Edge",
            default_policy=_policy(),
        )
        assert s.portfolio == []
        assert s.routing_decisions == {}
        # load did not create the file — save_state does.
        assert not p.exists()

    def test_save_then_load_preserves_state(self, tmp_path: Path) -> None:
        p = tmp_path / "state.json"
        original = _state_with_one_claim()
        save_state(p, original)
        assert p.exists()

        loaded = load_or_init_state(
            p,
            isd_as=original.isd_as,
            display_name=original.display_name,
            default_policy=_policy(),
        )
        assert loaded.isd_as == original.isd_as
        assert loaded.display_name == original.display_name
        assert loaded.policy == original.policy
        assert len(loaded.portfolio) == 1
        assert loaded.portfolio[0].payload.claim_id == original.portfolio[0].payload.claim_id
        # Routing decision keys are ints (hop_index → claim_id) after reload.
        reloaded = next(iter(loaded.routing_decisions.values()))
        assert set(reloaded.applied_claims.keys()) == {0, 1}

    def test_last_saved_at_stamped(self, tmp_path: Path) -> None:
        p = tmp_path / "state.json"
        save_state(p, _state_with_one_claim())
        import json
        data = json.loads(p.read_text())
        assert data["last_saved_at"].endswith("Z")
        assert data["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Corruption recovery
# ---------------------------------------------------------------------------


class TestCorruptionRecovery:
    def test_invalid_json_triggers_fresh_init(self, tmp_path: Path) -> None:
        p = tmp_path / "state.json"
        p.write_text("{ not valid json ")
        s = load_or_init_state(
            p,
            isd_as="1-ff00:0:112",
            display_name="User",
            default_policy=_policy(),
        )
        assert s.portfolio == []
        # The corrupt file is NOT overwritten by load — only save_state does that.
        assert p.read_text().startswith("{ not valid")

    def test_stale_schema_version_triggers_fresh_init(self, tmp_path: Path) -> None:
        p = tmp_path / "state.json"
        p.write_text('{"schema_version": 1, "isd_as": "x", "display_name": "y", "policy": {}}')
        s = load_or_init_state(
            p,
            isd_as="1-ff00:0:112",
            display_name="User",
            default_policy=_policy(),
        )
        assert s.portfolio == []
        assert s.isd_as == "1-ff00:0:112"

    def test_partial_policy_triggers_fresh_init(self, tmp_path: Path) -> None:
        """Missing enum fields in policy → ValueError → fresh init (not crash)."""
        import json
        p = tmp_path / "state.json"
        p.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "isd_as": "x",
                    "display_name": "y",
                    "policy": {"max_price_per_gb": "0.05"},  # nearly empty
                }
            )
        )
        s = load_or_init_state(
            p, isd_as="user", display_name="User", default_policy=_policy()
        )
        assert s.isd_as == "user"


# ---------------------------------------------------------------------------
# Policy validation on load
# ---------------------------------------------------------------------------


class TestPolicyValidation:
    @pytest.mark.parametrize(
        "bad_field,bad_value",
        [
            ("uncovered_tolerance", "whatever"),
            ("complaint_sensitivity", "extreme"),
            ("min_reputation_floor", 2.0),
        ],
    )
    def test_invalid_policy_fields_trigger_fresh_init(
        self, tmp_path: Path, bad_field: str, bad_value: object
    ) -> None:
        import json
        p = tmp_path / "state.json"
        body = {
            "schema_version": SCHEMA_VERSION,
            "isd_as": "1-ff00:0:112",
            "display_name": "User",
            "policy": {
                "max_price_per_gb": "0.10",
                "min_reputation_floor": 0.5,
                "required_bounds": {},
                "alpha": 1.0,
                "beta": 1.0,
                "uncovered_tolerance": "never",
                "complaint_sensitivity": "moderate",
                "reshop_on_reputation_drop": 0.1,
                "portfolio_redundancy": 1,
            },
            "portfolio": [],
            "routing_decisions": {},
            "last_saved_at": "",
        }
        body["policy"][bad_field] = bad_value  # type: ignore[index]
        p.write_text(json.dumps(body))
        s = load_or_init_state(
            p, isd_as="user", display_name="User", default_policy=_policy()
        )
        # Fallback: fresh state with the default policy, not the corrupt one.
        assert s.policy == _policy()


# ---------------------------------------------------------------------------
# Atomicity (no half-written files)
# ---------------------------------------------------------------------------


class TestAtomicity:
    def test_save_leaves_no_tmp_files_on_success(self, tmp_path: Path) -> None:
        p = tmp_path / "state.json"
        save_state(p, _state_with_one_claim())
        leftovers = [f for f in tmp_path.iterdir() if f.name.endswith(".tmp")]
        assert leftovers == []

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        p = tmp_path / "nested" / "dir" / "state.json"
        save_state(p, _state_with_one_claim())
        assert p.exists()
