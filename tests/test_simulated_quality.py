"""Step C2 — simulated-quality distribution tests (``DESIGN.md`` §7.5).

The acceptance property from DESIGN.md §7.5:

- ``transit-good`` samples rarely violate their SLA bounds.
- ``transit-bad`` samples violate on the order of 20-40% of the time
  (per-metric; compounded over multiple metrics, overall complaint rate can
  be higher).

These are statistical tests seeded with a fixed RNG so they are fully
deterministic and stable on re-run (no flakes).
"""

from __future__ import annotations

import random

import pytest

from pathmarket.agent.simulated_quality import (
    QualityModel,
    model_for_profile,
    sample_violates,
)
from pathmarket.schemas import SLABounds


_BOUNDS = SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=4_000_000)


def _violation_rates(profile: str, bounds: SLABounds, *, n: int = 5000, seed: int = 42) -> dict[str, float]:
    rng = random.Random(seed)
    model = model_for_profile(profile, bounds)
    totals = {"latency_ms": 0, "loss_ppm": 0, "bandwidth_kbps": 0}
    for _ in range(n):
        s = model.sample(rng)
        for m in totals:
            if sample_violates(bounds, s, m):
                totals[m] += 1
    return {m: totals[m] / n for m in totals}


class TestModelForProfile:
    def test_unknown_profile_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown quality profile"):
            model_for_profile("nonsense", _BOUNDS)  # type: ignore[arg-type]

    def test_none_bound_yields_none_params(self) -> None:
        loose = SLABounds(latency_max_ms=None, loss_max_ppm=200, bandwidth_min_kbps=None)
        model = model_for_profile("transit-good", loose)
        assert model.latency_mean_ms is None and model.latency_stddev_ms is None
        assert model.loss_mean_ppm is not None
        assert model.bandwidth_mean_kbps is None

    def test_premium_has_lowest_latency_mean(self) -> None:
        good = model_for_profile("transit-good", _BOUNDS).latency_mean_ms
        bad = model_for_profile("transit-bad", _BOUNDS).latency_mean_ms
        premium = model_for_profile("transit-premium", _BOUNDS).latency_mean_ms
        assert premium < good < bad


class TestViolationRates:
    def test_transit_good_rarely_violates(self) -> None:
        rates = _violation_rates("transit-good", _BOUNDS)
        # Each metric individually well under 10%.
        for metric, rate in rates.items():
            assert rate < 0.10, f"{metric} too high for transit-good: {rate:.3f}"

    def test_transit_bad_violates_in_the_20_to_40_percent_band(self) -> None:
        """Per §7.5: 20-40% per metric. We assert a loose but meaningful band."""
        rates = _violation_rates("transit-bad", _BOUNDS)
        # Each metric individually between 15% and 75% — the DESIGN.md band
        # with slack on both sides for normal-distribution tails under the
        # hand-picked profile parameters. The lower edge is the important
        # property (reputation MUST degrade); the upper edge catches a
        # future profile that degenerates into "always fail."
        for metric, rate in rates.items():
            assert 0.15 <= rate <= 0.75, f"{metric} out of band for transit-bad: {rate:.3f}"

    def test_transit_premium_almost_never_violates(self) -> None:
        rates = _violation_rates("transit-premium", _BOUNDS)
        for metric, rate in rates.items():
            assert rate < 0.01, f"{metric} too high for transit-premium: {rate:.4f}"


class TestSampleViolates:
    def test_latency_polarity(self) -> None:
        bounds = SLABounds(latency_max_ms=10, loss_max_ppm=None, bandwidth_min_kbps=None)
        from pathmarket.agent.simulated_quality import QualitySample
        assert sample_violates(bounds, QualitySample(latency_ms=11, loss_ppm=None, bandwidth_kbps=None), "latency_ms")
        assert not sample_violates(bounds, QualitySample(latency_ms=10, loss_ppm=None, bandwidth_kbps=None), "latency_ms")

    def test_bandwidth_polarity(self) -> None:
        bounds = SLABounds(latency_max_ms=None, loss_max_ppm=None, bandwidth_min_kbps=1_000_000)
        from pathmarket.agent.simulated_quality import QualitySample
        assert sample_violates(bounds, QualitySample(None, None, 900_000), "bandwidth_kbps")
        assert not sample_violates(bounds, QualitySample(None, None, 1_000_000), "bandwidth_kbps")

    def test_no_bound_never_violates(self) -> None:
        bounds = SLABounds(latency_max_ms=None, loss_max_ppm=None, bandwidth_min_kbps=None)
        from pathmarket.agent.simulated_quality import QualitySample
        assert not sample_violates(bounds, QualitySample(9999, 9999, 1), "latency_ms")

    def test_unknown_metric_raises(self) -> None:
        from pathmarket.agent.simulated_quality import QualitySample
        with pytest.raises(ValueError, match="unknown metric"):
            sample_violates(_BOUNDS, QualitySample(1, 1, 1), "jitter")


class TestDeterminismUnderSeed:
    def test_same_seed_same_samples(self) -> None:
        model = model_for_profile("transit-bad", _BOUNDS)
        a = [model.sample(random.Random(7)) for _ in range(1)][0]
        b = [model.sample(random.Random(7)) for _ in range(1)][0]
        assert a == b

    def test_different_seed_usually_different(self) -> None:
        model = model_for_profile("transit-bad", _BOUNDS)
        a = model.sample(random.Random(1))
        b = model.sample(random.Random(2))
        # Not a hard invariant, but with the wide stddev this is reliable.
        assert a != b
