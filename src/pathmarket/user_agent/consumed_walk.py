"""Simulated consumed-GB random walk (``DESIGN.md`` §8.5).

Per-minute of wall-clock time (regardless of tick interval), each claim's
virtual consumed counter advances by ``Uniform(0.1%, 0.6%)`` of
``gb_purchased``, capped at ``gb_purchased``. State lives in-memory only
(§8.5): restarts reset the counters.

This module is intentionally pure-Python + seeded RNG so it can be unit
tested without touching time or the UI.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable

from pathmarket.schemas import SignedClaim


_MIN_FRAC_PER_MIN = 0.001
_MAX_FRAC_PER_MIN = 0.006


@dataclass
class _ClaimWalkState:
    consumed_gb: float = 0.0
    last_update_monotonic: float = 0.0


@dataclass
class ConsumedGBWalk:
    """Per-claim simulated consumption. One instance per user-agent process."""

    rng: random.Random = field(default_factory=random.Random)
    monotonic_fn: Callable[[], float] = field(default_factory=lambda: _monotonic)
    _by_claim: dict[str, _ClaimWalkState] = field(default_factory=dict)

    def advance(self, claims: list[SignedClaim]) -> dict[str, float]:
        """Advance all claims' counters to ``now``, return ``{claim_id: consumed_gb}``.

        For each claim: sample ``Uniform(0.1%, 0.6%)`` of ``gb_purchased`` per
        simulated-minute of wall-clock elapsed since its last update; scale
        proportionally to the actual elapsed time so short polls don't
        over-advance. Capped at ``gb_purchased``.
        """

        now = self.monotonic_fn()
        result: dict[str, float] = {}
        for claim in claims:
            claim_id = claim.payload.claim_id
            gb_purchased = float(claim.payload.gb_purchased)
            st = self._by_claim.get(claim_id)
            if st is None:
                st = _ClaimWalkState(consumed_gb=0.0, last_update_monotonic=now)
                self._by_claim[claim_id] = st
            elapsed_s = max(0.0, now - st.last_update_monotonic)
            if elapsed_s > 0.0 and st.consumed_gb < gb_purchased:
                frac_per_minute = self.rng.uniform(_MIN_FRAC_PER_MIN, _MAX_FRAC_PER_MIN)
                # Scale by elapsed minutes; callers polling every few seconds
                # get a proportional fraction of the per-minute draw.
                delta = gb_purchased * frac_per_minute * (elapsed_s / 60.0)
                st.consumed_gb = min(gb_purchased, st.consumed_gb + delta)
            st.last_update_monotonic = now
            result[claim_id] = st.consumed_gb
        return result


def _monotonic() -> float:
    import time
    return time.monotonic()


__all__ = ["ConsumedGBWalk"]
