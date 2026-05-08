"""Policy evaluation helpers (``DESIGN.md`` §4.11, §7).

Pure functions that evaluate a :class:`Policy` against market data. Kept
stateless and side-effect-free so they can be unit-tested in isolation; the
stateful :class:`Agent` class in :mod:`pathmarket.agent.agent` calls into
them.

The two "feel" dimensions of a Policy are:

- **Acceptance** (§7.2 step 3) — does this SLA meet my price / reputation /
  required-bounds floors? Used when browsing the market to shop.
- **Ranking** (§7.2 step 4) — given multiple acceptable candidates, which do
  I prefer? Utility is ``alpha * mean_rep - beta * normalized_price`` per
  §7.2. Higher is better.

Plus a small helper for :class:`Policy.complaint_sensitivity` — converting
the string enum into the numeric over-bound threshold used to decide whether
a simulated quality sample is "bad enough" to complain about (§4.11).
"""

from __future__ import annotations

from decimal import Decimal

from pathmarket.schemas import Policy, SLABounds, SignedSLA


# ---------------------------------------------------------------------------
# Complaint sensitivity → numeric threshold multiplier
# ---------------------------------------------------------------------------


_COMPLAINT_MULTIPLIERS: dict[str, float] = {
    "strict": 1.10,    # complain at bound + 10%
    "moderate": 1.25,  # complain at bound + 25%
    "tolerant": 1.75,  # complain at bound + 75%
}


def complaint_multiplier(sensitivity: str) -> float:
    """Return the over-bound multiplier for a ``complaint_sensitivity`` string.

    Unknown values raise ``ValueError`` — the enum is validated at policy-load
    time, so encountering an unknown value here is a programming error.
    """

    try:
        return _COMPLAINT_MULTIPLIERS[sensitivity]
    except KeyError as e:
        raise ValueError(f"unknown complaint_sensitivity: {sensitivity!r}") from e


def should_file_complaint(
    policy: Policy, metric: str, bound: int | None, measured: int
) -> bool:
    """Decide whether ``measured`` crosses ``policy``'s complaint threshold.

    Violation polarity follows §4.8:
    - latency / loss: violation when ``measured > bound`` (high is bad)
    - bandwidth:      violation when ``measured < bound`` (low is bad)

    For latency/loss, the complaint threshold is ``bound * multiplier``;
    for bandwidth, it is ``bound / multiplier`` (going below by the same
    fractional amount).

    Returns ``False`` if the SLA has no bound for ``metric``.
    """

    if bound is None:
        return False
    m = complaint_multiplier(policy.complaint_sensitivity)
    if metric in ("latency_ms", "loss_ppm"):
        return measured > bound * m
    if metric == "bandwidth_kbps":
        return measured < bound / m
    raise ValueError(f"unknown metric: {metric!r}")


# ---------------------------------------------------------------------------
# Acceptance: would this policy claim this SLA?
# ---------------------------------------------------------------------------


def sla_meets_required_bounds(sla_bounds: SLABounds, required: SLABounds) -> bool:
    """Return True iff ``sla_bounds`` is at least as strict as ``required``.

    A ``None`` field in ``required`` means "don't care." A ``None`` field in
    ``sla_bounds`` means "no commitment" — which fails any non-``None``
    requirement.
    """

    if required.latency_max_ms is not None:
        if sla_bounds.latency_max_ms is None or sla_bounds.latency_max_ms > required.latency_max_ms:
            return False
    if required.loss_max_ppm is not None:
        if sla_bounds.loss_max_ppm is None or sla_bounds.loss_max_ppm > required.loss_max_ppm:
            return False
    if required.bandwidth_min_kbps is not None:
        if sla_bounds.bandwidth_min_kbps is None or sla_bounds.bandwidth_min_kbps < required.bandwidth_min_kbps:
            return False
    return True


def policy_accepts_sla(policy: Policy, sla: SignedSLA, min_cosigner_score: float) -> bool:
    """Full acceptance check: price ≤ max, min cosigner score ≥ floor, bounds meet required.

    ``min_cosigner_score`` is the minimum score among the SLA's cosigners at
    the moment of the decision — this is the "reputation" the policy sees for
    the SLA as a whole. Using the min (not the mean) reflects §9 composition
    semantics (chain reputation = min over hops).
    """

    # Price gate
    if Decimal(sla.payload.price_per_gb) > Decimal(policy.max_price_per_gb):
        return False
    # Reputation gate
    if min_cosigner_score < policy.min_reputation_floor:
        return False
    # Bounds gate
    if not sla_meets_required_bounds(sla.payload.bounds, policy.required_bounds):
        return False
    return True


# ---------------------------------------------------------------------------
# Ranking: utility = alpha * mean_rep - beta * normalized_price
# ---------------------------------------------------------------------------


def normalized_price(price_per_gb: str) -> float:
    """Normalize a CHF-per-GB decimal string to a float in the same scale
    agents reason about (direct numeric value).

    We keep this a simple float conversion; the utility function's sensitivity
    to absolute magnitude is controlled by ``beta``, not by any additional
    normalization.
    """

    return float(Decimal(price_per_gb))


def sla_utility(policy: Policy, sla: SignedSLA, mean_cosigner_score: float) -> float:
    """Utility under this policy for claiming this SLA.

    ``alpha * mean_rep - beta * price``. Higher is better. Ties are broken
    lexicographically by ``sla_id`` at the caller; no tie-break here.
    """

    return policy.alpha * mean_cosigner_score - policy.beta * normalized_price(sla.payload.price_per_gb)


def min_cosigner_score(sla: SignedSLA, scores_by_as: dict[str, float]) -> float:
    """Minimum score across an SLA's cosigners. Missing cosigner → score 1.0.

    The default of 1.0 for missing ASes matches the aggregator's ``GET
    /score/{as}`` behavior for ASes with no violations.
    """

    return min((scores_by_as.get(c, 1.0) for c in sla.payload.cosigners), default=1.0)


def mean_cosigner_score(sla: SignedSLA, scores_by_as: dict[str, float]) -> float:
    """Mean score across an SLA's cosigners. Empty → 1.0."""

    vals = [scores_by_as.get(c, 1.0) for c in sla.payload.cosigners]
    return sum(vals) / len(vals) if vals else 1.0


def choose_best_sla(
    policy: Policy, candidates: list[SignedSLA], scores_by_as: dict[str, float]
) -> SignedSLA | None:
    """Among SLAs accepted by ``policy``, pick the one with highest utility.

    Ties on utility broken by ``sla_id`` ascending — deterministic, so replay
    under the same seed produces the same pick. Returns ``None`` if no
    candidate is acceptable.
    """

    acceptable = [
        s for s in sorted(candidates, key=lambda s: s.payload.sla_id)
        if policy_accepts_sla(policy, s, min_cosigner_score(s, scores_by_as))
    ]
    if not acceptable:
        return None
    best = acceptable[0]
    best_u = sla_utility(policy, best, mean_cosigner_score(best, scores_by_as))
    for s in acceptable[1:]:
        u = sla_utility(policy, s, mean_cosigner_score(s, scores_by_as))
        if u > best_u:
            best, best_u = s, u
    return best


# ---------------------------------------------------------------------------
# Bounds composition helpers (used by routing in §9 composition semantics).
# Kept here because they're closely coupled to Policy interpretation.
# ---------------------------------------------------------------------------


def compose_bounds(bounds_seq: list[SLABounds]) -> SLABounds:
    """Compose a sequence of hop-level bounds into an end-to-end aggregate.

    Per §9: latency sums, loss sums (approximate), bandwidth = min.
    A ``None`` field in any hop yields ``None`` in the aggregate (unknown).
    Empty input yields all-``None``.
    """

    if not bounds_seq:
        return SLABounds(latency_max_ms=None, loss_max_ppm=None, bandwidth_min_kbps=None)

    latencies = [b.latency_max_ms for b in bounds_seq]
    losses = [b.loss_max_ppm for b in bounds_seq]
    bws = [b.bandwidth_min_kbps for b in bounds_seq]

    lat_agg = None if any(v is None for v in latencies) else sum(latencies)  # type: ignore[arg-type]
    loss_agg = None if any(v is None for v in losses) else sum(losses)  # type: ignore[arg-type]
    bw_agg = None if any(v is None for v in bws) else min(bws)  # type: ignore[type-var]

    return SLABounds(latency_max_ms=lat_agg, loss_max_ppm=loss_agg, bandwidth_min_kbps=bw_agg)


__all__ = [
    "choose_best_sla",
    "complaint_multiplier",
    "compose_bounds",
    "mean_cosigner_score",
    "min_cosigner_score",
    "normalized_price",
    "policy_accepts_sla",
    "should_file_complaint",
    "sla_meets_required_bounds",
    "sla_utility",
]
