"""Aggregator-side scoring helpers (``DESIGN.md`` §5, §6).

Wraps :mod:`pathmarket.scorer` with the storage-shaped inputs used by the
FastAPI handlers: all stored complaints, all stored SLAs.
"""

from __future__ import annotations

from datetime import datetime

from pathmarket.aggregator.storage import AggregatorStorage
from pathmarket.schemas import Score, SignedComplaint, ViolationEvent
from pathmarket.scorer.scorer import ScoringConfig, compute_score, compute_violation_events


def all_violation_events(
    storage: AggregatorStorage, config: ScoringConfig, now: datetime
) -> list[ViolationEvent]:
    """Compute violation events across every SLA in storage."""
    complaints: list[SignedComplaint] = [c for _, c in storage.complaints]
    events: list[ViolationEvent] = []
    for sla in storage.slas.values():
        events.extend(compute_violation_events(complaints, sla, config, now))
    return events


def score_for_as(
    isd_as: str, storage: AggregatorStorage, config: ScoringConfig, now: datetime
) -> Score:
    events = [e for e in all_violation_events(storage, config, now) if isd_as in e.attributed_to]
    return compute_score(isd_as, events, config, now)


def all_cosigner_ases(storage: AggregatorStorage) -> list[str]:
    """Distinct ISD-ASes appearing as a cosigner in any SLA, sorted."""
    seen: set[str] = set()
    for sla in storage.slas.values():
        seen.update(sla.payload.cosigners)
    return sorted(seen)


def scores_for_all(
    storage: AggregatorStorage, config: ScoringConfig, now: datetime
) -> list[Score]:
    events = all_violation_events(storage, config, now)
    by_as: dict[str, list[ViolationEvent]] = {}
    for ev in events:
        for isd_as in ev.attributed_to:
            by_as.setdefault(isd_as, []).append(ev)
    return [
        compute_score(isd_as, by_as.get(isd_as, []), config, now)
        for isd_as in all_cosigner_ases(storage)
    ]


__all__ = [
    "all_cosigner_ases",
    "all_violation_events",
    "score_for_as",
    "scores_for_all",
]
