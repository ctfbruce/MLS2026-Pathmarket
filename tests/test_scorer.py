"""Scorer tests — §6 behaviours.

Covers:
- All ASes start at 1.0 with empty components (§5.2 /score semantics).
- One complaint does not trigger a violation event.
- k distinct complainants within the window DO trigger an event.
- The same complainant filing twice within a window is de-duplicated
  (one-distinct, not k-distinct).
- Complainants outside the window are not counted.
- Non-violating complaints (measured_value within bound) are ignored.
- Bandwidth-violation polarity is opposite (measured < bound).
- Score decays with the expected time constant.
- Multiple events accumulate but clamp at 0.0.
- `attributed_to` lists the SLA's cosigners.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from pathmarket.canonical import compute_complaint_id, compute_sla_id
from pathmarket.schemas import (
    SLABounds,
    Signature,
    SignatureMeta,
    SignedComplaint,
    SignedSLA,
    ViolationEvent,
)
from pathmarket.scorer import ScoringConfig, compute_score, compute_violation_events
from tests.fixtures.builders import make_complaint_payload, make_sla_payload


def _make_signed_sla() -> SignedSLA:
    p = make_sla_payload()
    p = replace(p, sla_id=compute_sla_id(p))
    return SignedSLA(payload=p, signatures=[])  # signatures unused by the scorer


def _fake_sig(isd_as: str) -> Signature:
    return Signature(
        meta=SignatureMeta(isd_as=isd_as, key_id="default", trc_serial=0, trc_base=0),
        sig_bytes=b"\x00" * 64,
    )


def _complaint(
    *,
    sla_id: str,
    complainant: str,
    observed_at: str,
    metric: str = "latency_ms",
    measured_value: int = 22,
) -> SignedComplaint:
    p = make_complaint_payload(
        sla_id=sla_id,
        complainant=complainant,
        observed_at=observed_at,
        metric=metric,
        measured_value=measured_value,
    )
    p = replace(p, complaint_id=compute_complaint_id(p))
    return SignedComplaint(payload=p, signature=_fake_sig(complainant))


class TestComputeScoreBaseline:
    def test_no_events_returns_one_point_zero(self) -> None:
        now = datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)
        s = compute_score("1-ff00:0:110", [], ScoringConfig(), now)
        assert s.isd_as == "1-ff00:0:110"
        assert s.score == 1.0
        assert s.components == {}


class TestViolationEvents:
    def test_single_complaint_does_not_trigger(self) -> None:
        sla = _make_signed_sla()
        sid = sla.payload.sla_id
        complaints = [
            _complaint(sla_id=sid, complainant="1-ff00:0:201", observed_at="2026-04-19T12:00:00Z")
        ]
        now = datetime(2026, 4, 19, 12, 10, tzinfo=timezone.utc)
        events = compute_violation_events(complaints, sla, ScoringConfig(k=3), now)
        assert events == []

    def test_k_distinct_within_window_triggers(self) -> None:
        sla = _make_signed_sla()
        sid = sla.payload.sla_id
        complaints = [
            _complaint(sla_id=sid, complainant="1-ff00:0:201", observed_at="2026-04-19T12:00:00Z"),
            _complaint(sla_id=sid, complainant="1-ff00:0:202", observed_at="2026-04-19T12:02:00Z"),
            _complaint(sla_id=sid, complainant="1-ff00:0:203", observed_at="2026-04-19T12:04:00Z"),
        ]
        now = datetime(2026, 4, 19, 12, 10, tzinfo=timezone.utc)
        events = compute_violation_events(complaints, sla, ScoringConfig(k=3, window_minutes=5), now)
        assert len(events) == 1
        ev = events[0]
        assert ev.sla_id == sid
        assert ev.metric == "latency_ms"
        assert ev.window_end == datetime(2026, 4, 19, 12, 4, tzinfo=timezone.utc)
        assert sorted(ev.complainants) == ["1-ff00:0:201", "1-ff00:0:202", "1-ff00:0:203"]
        assert ev.attributed_to == list(sla.payload.cosigners)

    def test_same_complainant_twice_is_deduped(self) -> None:
        sla = _make_signed_sla()
        sid = sla.payload.sla_id
        # Same complainant files three times within the window — only one distinct complainant.
        complaints = [
            _complaint(sla_id=sid, complainant="1-ff00:0:201", observed_at="2026-04-19T12:00:00Z"),
            _complaint(sla_id=sid, complainant="1-ff00:0:201", observed_at="2026-04-19T12:01:00Z"),
            _complaint(sla_id=sid, complainant="1-ff00:0:201", observed_at="2026-04-19T12:02:00Z"),
        ]
        now = datetime(2026, 4, 19, 12, 10, tzinfo=timezone.utc)
        events = compute_violation_events(complaints, sla, ScoringConfig(k=3), now)
        assert events == []

    def test_complainants_outside_window_are_not_counted(self) -> None:
        sla = _make_signed_sla()
        sid = sla.payload.sla_id
        # Window is 5 minutes. Complaints spread over 20 minutes — at most two in any 5-min window.
        complaints = [
            _complaint(sla_id=sid, complainant="1-ff00:0:201", observed_at="2026-04-19T12:00:00Z"),
            _complaint(sla_id=sid, complainant="1-ff00:0:202", observed_at="2026-04-19T12:10:00Z"),
            _complaint(sla_id=sid, complainant="1-ff00:0:203", observed_at="2026-04-19T12:20:00Z"),
        ]
        now = datetime(2026, 4, 19, 12, 30, tzinfo=timezone.utc)
        events = compute_violation_events(complaints, sla, ScoringConfig(k=3, window_minutes=5), now)
        assert events == []

    def test_non_violating_measurements_are_ignored(self) -> None:
        sla = _make_signed_sla()
        sid = sla.payload.sla_id
        # SLA latency_max_ms = 10 (from builder default). Measured 9 is below bound.
        complaints = [
            _complaint(
                sla_id=sid,
                complainant="1-ff00:0:201",
                observed_at="2026-04-19T12:00:00Z",
                measured_value=9,
            ),
            _complaint(
                sla_id=sid,
                complainant="1-ff00:0:202",
                observed_at="2026-04-19T12:01:00Z",
                measured_value=9,
            ),
            _complaint(
                sla_id=sid,
                complainant="1-ff00:0:203",
                observed_at="2026-04-19T12:02:00Z",
                measured_value=9,
            ),
        ]
        now = datetime(2026, 4, 19, 12, 10, tzinfo=timezone.utc)
        events = compute_violation_events(complaints, sla, ScoringConfig(k=3), now)
        assert events == []

    def test_bandwidth_violation_polarity(self) -> None:
        # Build an SLA whose only bound is bandwidth_min_kbps = 1_000_000.
        p = make_sla_payload(bounds=SLABounds(None, None, 1_000_000))
        p = replace(p, sla_id=compute_sla_id(p))
        sla = SignedSLA(payload=p, signatures=[])
        sid = sla.payload.sla_id

        # "Violation" = measured < bound. 500_000 < 1_000_000 → violates.
        complaints = [
            _complaint(
                sla_id=sid,
                complainant="1-ff00:0:201",
                observed_at="2026-04-19T12:00:00Z",
                metric="bandwidth_kbps",
                measured_value=500_000,
            ),
            _complaint(
                sla_id=sid,
                complainant="1-ff00:0:202",
                observed_at="2026-04-19T12:01:00Z",
                metric="bandwidth_kbps",
                measured_value=500_000,
            ),
            _complaint(
                sla_id=sid,
                complainant="1-ff00:0:203",
                observed_at="2026-04-19T12:02:00Z",
                metric="bandwidth_kbps",
                measured_value=500_000,
            ),
        ]
        now = datetime(2026, 4, 19, 12, 10, tzinfo=timezone.utc)
        events = compute_violation_events(complaints, sla, ScoringConfig(k=3), now)
        assert len(events) == 1
        assert events[0].metric == "bandwidth_kbps"

    def test_complaints_for_other_slas_are_filtered_out(self) -> None:
        sla = _make_signed_sla()
        sid = sla.payload.sla_id
        # Three complaints but against a different SLA id.
        other = "sha256:" + "0" * 64
        complaints = [
            _complaint(sla_id=other, complainant="1-ff00:0:201", observed_at="2026-04-19T12:00:00Z"),
            _complaint(sla_id=other, complainant="1-ff00:0:202", observed_at="2026-04-19T12:01:00Z"),
            _complaint(sla_id=other, complainant="1-ff00:0:203", observed_at="2026-04-19T12:02:00Z"),
        ]
        now = datetime(2026, 4, 19, 12, 10, tzinfo=timezone.utc)
        events = compute_violation_events(complaints, sla, ScoringConfig(k=3), now)
        assert events == []


class TestScoreDecay:
    def test_score_decays_with_time_constant(self) -> None:
        """At t = tau after the event, penalty should be weight * 1/e."""
        tau_hours = 100.0
        config = ScoringConfig(violation_weight=0.1, decay_time_constant_hours=tau_hours)
        event_time = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        ev = ViolationEvent(
            sla_id="sha256:x",
            metric="latency_ms",
            window_end=event_time,
            complainants=["a", "b", "c"],
            attributed_to=["1-ff00:0:110"],
        )

        # At t = tau hours after the event: decay = 1/e.
        now = event_time + timedelta(hours=tau_hours)
        s = compute_score("1-ff00:0:110", [ev], config, now)
        expected_penalty = 0.1 * math.exp(-1.0)
        assert s.score == max(0.0, 1.0 - expected_penalty)
        assert s.components["recent_violations"] == 1

    def test_score_at_event_time_is_full_penalty(self) -> None:
        """At t = 0 after the event, decay = 1.0 so full violation_weight penalty applies."""
        config = ScoringConfig(violation_weight=0.1, decay_time_constant_hours=100.0)
        event_time = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        ev = ViolationEvent(
            sla_id="sha256:x",
            metric="latency_ms",
            window_end=event_time,
            complainants=["a", "b", "c"],
            attributed_to=["1-ff00:0:110"],
        )
        s = compute_score("1-ff00:0:110", [ev], config, event_time)
        assert s.score == 0.9

    def test_score_clamps_at_zero(self) -> None:
        """Many simultaneous events can't drive the score below 0."""
        config = ScoringConfig(violation_weight=0.1, decay_time_constant_hours=100.0)
        event_time = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        events = [
            ViolationEvent(
                sla_id=f"sha256:{i:064d}",
                metric="latency_ms",
                window_end=event_time,
                complainants=["a", "b", "c"],
                attributed_to=["1-ff00:0:110"],
            )
            for i in range(20)  # 20 * 0.1 = 2.0 penalty → clamps to 0.
        ]
        s = compute_score("1-ff00:0:110", events, config, event_time)
        assert s.score == 0.0
