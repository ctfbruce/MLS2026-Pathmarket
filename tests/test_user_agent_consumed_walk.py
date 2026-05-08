"""Step D7 — consumed-GB walk tests (``DESIGN.md`` §8.5).

Deterministic under a seeded RNG + injected monotonic clock. Verifies:
starts at 0, advances on wall-clock elapsed, proportional to elapsed
minutes, capped at ``gb_purchased``, independent per claim.
"""

from __future__ import annotations

import random
from unittest.mock import MagicMock

from pathmarket.schemas import ClaimPayload, Signature, SignatureMeta, SignedClaim
from pathmarket.user_agent.consumed_walk import ConsumedGBWalk


def _claim(claim_id: str, gb: int) -> SignedClaim:
    payload = ClaimPayload(
        claim_id=claim_id,
        sla_id="sha256:sla",
        claimant="1-ff00:0:112",
        gb_purchased=gb,
        claimed_at="2026-04-19T12:00:00Z",
        price_paid_chf="0.00",
        schema_version=2,
    )
    sig = Signature(
        meta=SignatureMeta(isd_as="1-ff00:0:112", key_id="k", trc_serial=0, trc_base=0),
        sig_bytes=b"\x00" * 64,
    )
    return SignedClaim(payload=payload, signature=sig)


class TestConsumedGBWalk:
    def test_starts_at_zero_on_first_advance(self) -> None:
        clock = {"t": 100.0}
        walk = ConsumedGBWalk(rng=random.Random(1), monotonic_fn=lambda: clock["t"])
        result = walk.advance([_claim("c1", 500)])
        assert result == {"c1": 0.0}

    def test_advances_proportionally_to_elapsed_time(self) -> None:
        clock = {"t": 100.0}
        walk = ConsumedGBWalk(rng=random.Random(42), monotonic_fn=lambda: clock["t"])
        c = _claim("c1", 1_000)
        walk.advance([c])  # initialize t
        clock["t"] = 100.0 + 60.0  # +1 minute
        r = walk.advance([c])
        # Uniform(0.001, 0.006) * 1000 ≈ in [1, 6] gb
        assert 1.0 <= r["c1"] <= 6.0

    def test_cap_at_gb_purchased(self) -> None:
        clock = {"t": 0.0}
        walk = ConsumedGBWalk(rng=random.Random(1), monotonic_fn=lambda: clock["t"])
        c = _claim("c1", 100)
        walk.advance([c])
        clock["t"] = 10_000_000.0  # way more than needed to saturate
        r = walk.advance([c])
        assert r["c1"] == 100.0

    def test_independent_per_claim(self) -> None:
        clock = {"t": 0.0}
        walk = ConsumedGBWalk(rng=random.Random(7), monotonic_fn=lambda: clock["t"])
        c1, c2 = _claim("c1", 1_000), _claim("c2", 2_000)
        walk.advance([c1, c2])
        clock["t"] = 120.0  # +2 minutes
        r = walk.advance([c1, c2])
        assert 0.0 < r["c1"] <= 12.0  # ≤ 0.6% × 1000 × 2 min
        assert 0.0 < r["c2"] <= 24.0  # ≤ 0.6% × 2000 × 2 min
        # And they aren't literally the same number.
        assert r["c1"] != r["c2"]

    def test_no_elapsed_time_no_advance(self) -> None:
        clock = {"t": 42.0}
        walk = ConsumedGBWalk(rng=random.Random(1), monotonic_fn=lambda: clock["t"])
        c = _claim("c1", 500)
        walk.advance([c])
        r = walk.advance([c])  # same clock value
        assert r["c1"] == 0.0

    def test_counter_survives_between_calls(self) -> None:
        clock = {"t": 0.0}
        walk = ConsumedGBWalk(rng=random.Random(3), monotonic_fn=lambda: clock["t"])
        c = _claim("c1", 10_000)
        walk.advance([c])
        clock["t"] = 60.0
        r1 = walk.advance([c])
        clock["t"] = 120.0
        r2 = walk.advance([c])
        assert r2["c1"] > r1["c1"]
