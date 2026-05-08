"""Simulated-quality distributions (``DESIGN.md`` §7.5).

Each simulated SLA has a private "true quality" distribution held inside the
simulator — it never leaves this process and never appears in any signed
artifact. On each tick, an AS that holds a claim on an SLA draws one sample
per metric; if the sample violates the SLA's bound by more than the
complainant's ``complaint_sensitivity`` threshold (see
:mod:`pathmarket.agent.policy`), a complaint is filed.

Profile semantics (§7.5, §7.4):

- ``transit-good`` — mean comfortably inside the bound. E.g. latency bound
  10 ms, mean 6 ms, stddev 1.5 ms. Violates rarely.
- ``transit-bad`` — mean at or slightly above the bound. Violates ~20-40%
  per metric sample. Reputation degrades under normal market traffic.
- ``transit-premium`` — near-perfect: mean well below bound, tiny stddev.
  Used for the Hospital-facing premium cosigners.

Because the quality model is simulator-internal, the exact numbers here are
implementation choices bounded by the DESIGN.md ranges, not amendments to
the spec. The tests pin the statistical behavior (violation rate) that the
demo loop depends on.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

from pathmarket.schemas import SLABounds


QualityProfile = Literal["transit-good", "transit-bad", "transit-premium"]


@dataclass(frozen=True)
class QualitySample:
    """One simulator-internal measurement of an SLA's quality.

    ``None`` fields mean "the underlying bound was ``None`` in the SLA, so
    the simulator doesn't generate a measurement for this metric." That
    matches how the aggregator rejects complaints against unbounded metrics
    (§5.2 complaint validation step).
    """

    latency_ms: int | None
    loss_ppm: int | None
    bandwidth_kbps: int | None


@dataclass(frozen=True)
class QualityModel:
    """Per-SLA per-metric normal-distribution parameters.

    The simulator keeps one ``QualityModel`` per (SLA, cosigner) pair — or
    one per SLA if the whole consortium has a shared profile, which is the
    §7.5 default.
    """

    latency_mean_ms: float | None
    latency_stddev_ms: float | None
    loss_mean_ppm: float | None
    loss_stddev_ppm: float | None
    bandwidth_mean_kbps: float | None
    bandwidth_stddev_kbps: float | None

    def sample(self, rng: random.Random) -> QualitySample:
        return QualitySample(
            latency_ms=self._sample_int(rng, self.latency_mean_ms, self.latency_stddev_ms, low=0),
            loss_ppm=self._sample_int(rng, self.loss_mean_ppm, self.loss_stddev_ppm, low=0),
            bandwidth_kbps=self._sample_int(
                rng, self.bandwidth_mean_kbps, self.bandwidth_stddev_kbps, low=0
            ),
        )

    @staticmethod
    def _sample_int(
        rng: random.Random, mean: float | None, stddev: float | None, *, low: int
    ) -> int | None:
        if mean is None or stddev is None:
            return None
        return max(low, round(rng.gauss(mean, stddev)))


# ---------------------------------------------------------------------------
# Profile → concrete parameters, given the SLA's bounds
# ---------------------------------------------------------------------------


_PROFILE_PARAMS: dict[QualityProfile, dict[str, tuple[float, float]]] = {
    # (mean_fraction_of_bound, stddev_fraction_of_bound) per metric.
    # For bandwidth the fractions are applied above the bound (higher is better).
    "transit-premium": {
        "latency": (0.40, 0.05),   # mean 40% of bound, stddev 5% — tight
        "loss":    (0.30, 0.05),
        "bandwidth": (1.80, 0.05), # well above the min
    },
    "transit-good": {
        "latency": (0.60, 0.15),   # mean 60%, stddev 15% — rarely > bound
        "loss":    (0.55, 0.15),
        "bandwidth": (1.40, 0.10),
    },
    "transit-bad": {
        "latency": (1.00, 0.15),   # centered AT bound — ~50% sample violations
        "loss":    (1.05, 0.20),   # mean above bound → majority violates
        "bandwidth": (0.95, 0.10), # mean slightly below bound
    },
}


def model_for_profile(profile: QualityProfile, bounds: SLABounds) -> QualityModel:
    """Instantiate a :class:`QualityModel` matching ``profile`` for ``bounds``.

    Metrics with no bound in the SLA contribute ``None`` to the model —
    they won't produce samples, and complaints about them would be rejected
    at aggregator-side validation anyway (§5.2).
    """

    try:
        params = _PROFILE_PARAMS[profile]
    except KeyError as e:
        raise ValueError(f"unknown quality profile: {profile!r}") from e

    def _mean_std(bound: int | None, key: str) -> tuple[float | None, float | None]:
        if bound is None:
            return (None, None)
        mean_frac, std_frac = params[key]
        return (bound * mean_frac, bound * std_frac)

    lat_m, lat_s = _mean_std(bounds.latency_max_ms, "latency")
    loss_m, loss_s = _mean_std(bounds.loss_max_ppm, "loss")
    bw_m, bw_s = _mean_std(bounds.bandwidth_min_kbps, "bandwidth")

    return QualityModel(
        latency_mean_ms=lat_m,
        latency_stddev_ms=lat_s,
        loss_mean_ppm=loss_m,
        loss_stddev_ppm=loss_s,
        bandwidth_mean_kbps=bw_m,
        bandwidth_stddev_kbps=bw_s,
    )


# ---------------------------------------------------------------------------
# Violation check: does a single sample violate a specific metric's bound?
# ---------------------------------------------------------------------------


def sample_violates(bounds: SLABounds, sample: QualitySample, metric: str) -> bool:
    """Return True iff ``sample`` violates the SLA's ``bounds`` for ``metric``.

    Polarity matches §4.8: latency/loss — higher is bad; bandwidth — lower
    is bad. Missing bound → never violates.
    """

    if metric == "latency_ms":
        return (
            bounds.latency_max_ms is not None
            and sample.latency_ms is not None
            and sample.latency_ms > bounds.latency_max_ms
        )
    if metric == "loss_ppm":
        return (
            bounds.loss_max_ppm is not None
            and sample.loss_ppm is not None
            and sample.loss_ppm > bounds.loss_max_ppm
        )
    if metric == "bandwidth_kbps":
        return (
            bounds.bandwidth_min_kbps is not None
            and sample.bandwidth_kbps is not None
            and sample.bandwidth_kbps < bounds.bandwidth_min_kbps
        )
    raise ValueError(f"unknown metric: {metric!r}")


__all__ = [
    "QualityModel",
    "QualityProfile",
    "QualitySample",
    "model_for_profile",
    "sample_violates",
]
