"""Cold-start record mode (``DESIGN.md`` §7.7).

Ticks the orchestrator as fast as possible for ``duration_hours`` of
*simulated* time, and appends every signed artifact emitted by each tick to
a JSONL file wrapped as::

    {"sim_time": "<ISO>", "artifact": {"type": "sla|claim|complaint", "payload": {...}}}

The resulting ``assets/cold_start.jsonl`` is replayed by the UI's pitch
opener (§9.5); the demo itself does not generate it live. One JSONL line
per artifact keeps the UI's streaming consumer trivial.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pathmarket.aggregator.models import (
    SignedClaimModel,
    SignedComplaintModel,
    SignedSLAModel,
)
from pathmarket.schemas import SignedClaim, SignedComplaint, SignedSLA
from pathmarket.simulator.orchestrator import Orchestrator


def _sla_to_wire(sla: SignedSLA) -> dict[str, Any]:
    return SignedSLAModel.from_dataclass(sla).model_dump()


def _claim_to_wire(claim: SignedClaim) -> dict[str, Any]:
    return SignedClaimModel.from_dataclass(claim).model_dump()


def _complaint_to_wire(complaint: SignedComplaint) -> dict[str, Any]:
    return SignedComplaintModel.from_dataclass(complaint).model_dump()


def run_record_mode(
    orchestrator: Orchestrator,
    *,
    record_file: Path,
    duration_hours: float,
    tick_interval_seconds: float,
) -> int:
    """Record ``duration_hours`` of simulated time to ``record_file``.

    Returns the total number of artifacts written. Real wall-clock time
    is whatever it takes; the simulator does not sleep between ticks.
    """

    if duration_hours <= 0:
        raise ValueError("duration_hours must be positive")
    if tick_interval_seconds <= 0:
        raise ValueError("tick_interval_seconds must be positive")

    total_ticks = max(1, int((duration_hours * 3600.0) / tick_interval_seconds))
    record_file.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with record_file.open("w", encoding="utf-8") as fh:
        for _ in range(total_ticks):
            summary = orchestrator.tick()
            sim_time = summary.tick_time.isoformat().replace("+00:00", "Z")
            for sla in summary.new_slas:
                fh.write(
                    json.dumps(
                        {"sim_time": sim_time, "artifact": {"type": "sla", "payload": _sla_to_wire(sla)}}
                    )
                    + "\n"
                )
                count += 1
            for claim in summary.new_claims:
                fh.write(
                    json.dumps(
                        {
                            "sim_time": sim_time,
                            "artifact": {"type": "claim", "payload": _claim_to_wire(claim)},
                        }
                    )
                    + "\n"
                )
                count += 1
            for complaint in summary.new_complaints:
                fh.write(
                    json.dumps(
                        {
                            "sim_time": sim_time,
                            "artifact": {
                                "type": "complaint",
                                "payload": _complaint_to_wire(complaint),
                            },
                        }
                    )
                    + "\n"
                )
                count += 1
    return count


__all__ = ["run_record_mode"]
