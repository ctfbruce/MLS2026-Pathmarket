"""Reputation scorer — carried from v1 in mechanism (``DESIGN.md`` §6).

Two functions, both stateless:

- ``compute_violation_events(complaints, sla, config, now)`` — per §6.1:
  scan complaints on the given SLA in ``observed_at`` order, sliding a window
  of ``config.window_minutes``; when at least ``config.k`` distinct complainants
  have filed violating complaints within the window for the same metric, emit
  a ``ViolationEvent`` whose ``window_end`` is the timestamp that took us over
  the threshold. One event per "crossing" — the same window does not re-emit
  if more distinct complainants keep arriving; a new event requires the
  current cohort to age out and a fresh set of ``k`` distinct complainants.

- ``compute_score(isd_as, events, config, now)`` — per §6.2:
  ``score = max(0.0, 1.0 - Σ violation_weight * decay(now - event.window_end))``
  where ``decay(dt) = exp(-dt / decay_time_constant)``. Clamps to ``[0.0, 1.0]``.

Per §4.8, "violation" is metric-specific:
- ``latency_ms`` / ``loss_ppm`` — ``measured_value > bound``
- ``bandwidth_kbps`` — ``measured_value < bound``
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from pathmarket.schemas import Score, SignedComplaint, SignedSLA, ViolationEvent


@dataclass(frozen=True)
class ScoringConfig:
    """Scorer tuning knobs. Defaults per ``DESIGN.md`` §6.3."""

    k: int = 3
    window_minutes: int = 5
    violation_weight: float = 0.1
    decay_time_constant_hours: float = 100.0


# ---------------------------------------------------------------------------
# Violation detection
# ---------------------------------------------------------------------------


def _is_violation(metric: str, measured: int, sla: SignedSLA) -> bool:
    """Return True iff ``measured`` violates the SLA's bound for ``metric``.

    Returns False if the SLA has no bound for that metric (shouldn't normally
    happen — aggregator rejects such complaints at ingestion — but the scorer
    is defensive).
    """

    bounds = sla.payload.bounds
    if metric == "latency_ms":
        bound = bounds.latency_max_ms
        return bound is not None and measured > bound
    if metric == "loss_ppm":
        bound = bounds.loss_max_ppm
        return bound is not None and measured > bound
    if metric == "bandwidth_kbps":
        bound = bounds.bandwidth_min_kbps
        return bound is not None and measured < bound
    return False


def _parse_observed_at(ts: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp with trailing ``Z`` (§4.5 convention)."""

    # datetime.fromisoformat in 3.11+ handles trailing "Z".
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def compute_violation_events(
    complaints: list[SignedComplaint],
    sla: SignedSLA,
    config: ScoringConfig,
    now: datetime,  # noqa: ARG001 — kept for signature stability (§6.4)
) -> list[ViolationEvent]:
    """Compute violation events for ``sla`` from ``complaints``.

    ``complaints`` may contain complaints against other SLAs; they are filtered
    here by ``sla.payload.sla_id``. See module docstring for windowing semantics.
    """

    sla_id = sla.payload.sla_id
    relevant = [c for c in complaints if c.payload.sla_id == sla_id]
    # Deduplicate per §5.2 POST /complaint step 13: (complainant, sla_id, metric)
    # complaints within the window are deduped at ingestion. The scorer trusts
    # that but also de-dupes here (defensive; a noisy log should not inflate
    # the count of distinct complainants).

    events: list[ViolationEvent] = []
    # Group by metric and process each group in observed_at order.
    by_metric: dict[str, list[SignedComplaint]] = {}
    for c in relevant:
        if not _is_violation(c.payload.metric, c.payload.measured_value, sla):
            continue
        by_metric.setdefault(c.payload.metric, []).append(c)

    window = timedelta(minutes=config.window_minutes)

    for metric, metric_complaints in by_metric.items():
        ordered = sorted(metric_complaints, key=lambda c: _parse_observed_at(c.payload.observed_at))
        # Maintain a sliding window; complainants are de-duplicated inside it.
        window_start_idx = 0
        # Track whether the current window has already triggered an event;
        # we will only emit a new event after the trigger complainants have
        # fully aged out.
        triggered_complainants: set[str] | None = None

        for i, c in enumerate(ordered):
            t_i = _parse_observed_at(c.payload.observed_at)
            # Slide start forward so that window is [t_i - W, t_i].
            while window_start_idx < i and (
                t_i - _parse_observed_at(ordered[window_start_idx].payload.observed_at) > window
            ):
                window_start_idx += 1

            # If we previously triggered, check whether the triggering cohort
            # has aged out; if so, we can trigger again.
            if triggered_complainants is not None:
                still_in_window = {
                    ordered[j].payload.complainant
                    for j in range(window_start_idx, i + 1)
                    if ordered[j].payload.complainant in triggered_complainants
                }
                if not still_in_window:
                    triggered_complainants = None

            if triggered_complainants is not None:
                continue  # suppress until the old cohort ages out

            distinct = {
                ordered[j].payload.complainant for j in range(window_start_idx, i + 1)
            }
            if len(distinct) >= config.k:
                events.append(
                    ViolationEvent(
                        sla_id=sla_id,
                        metric=metric,
                        window_end=t_i,
                        complainants=sorted(distinct),
                        attributed_to=list(sla.payload.cosigners),
                    )
                )
                triggered_complainants = set(distinct)

    return events


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------


def compute_score(
    isd_as: str,
    all_violation_events_for_this_as: list[ViolationEvent],
    config: ScoringConfig,
    now: datetime,
) -> Score:
    """Compute the reputation ``Score`` for ``isd_as`` per §6.2.

    ``all_violation_events_for_this_as`` must be pre-filtered to events where
    ``isd_as`` is a cosigner (i.e. in ``attributed_to``). Scores start at 1.0
    for any AS with zero events and clamp to ``[0.0, 1.0]``.
    """

    if not all_violation_events_for_this_as:
        # §5.2 GET /score: an AS with zero violations returns 1.0 with empty components.
        return Score(isd_as=isd_as, score=1.0, components={})

    tau_hours = config.decay_time_constant_hours
    total_penalty = 0.0
    per_event = []
    for ev in all_violation_events_for_this_as:
        dt_hours = (now - ev.window_end).total_seconds() / 3600.0
        decay_factor = math.exp(-dt_hours / tau_hours) if tau_hours > 0 else 0.0
        penalty = config.violation_weight * decay_factor
        total_penalty += penalty
        per_event.append(
            {
                "sla_id": ev.sla_id,
                "metric": ev.metric,
                "window_end": ev.window_end.isoformat(),
                "decay_factor": decay_factor,
                "penalty": penalty,
            }
        )

    raw = 1.0 - total_penalty
    clamped = max(0.0, min(1.0, raw))
    components = {
        "recent_violations": len(all_violation_events_for_this_as),
        "total_penalty": total_penalty,
        "events": per_event,
    }
    return Score(isd_as=isd_as, score=clamped, components=components)


__all__ = ["ScoringConfig", "compute_score", "compute_violation_events"]
