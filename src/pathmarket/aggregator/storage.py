"""In-memory aggregator storage (``DESIGN.md`` §5.4).

No persistence. Restart resets to empty. Linear scans are fine at v2 scale
(≲100 SLAs, ≲300 claims, ≲100 complaints during a demo).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque, Optional

from pathmarket.schemas import SignedClaim, SignedComplaint, SignedSLA


TICKER_MAXLEN = 500


@dataclass(frozen=True)
class TickerEvent:
    """One ticker line as produced by the aggregator (``DESIGN.md`` §5.2 `/ticker`)."""

    type: str  # one of: sla_signed, claim, complaint, reputation_change
    timestamp: datetime
    detail: str


@dataclass
class AggregatorStorage:
    """Mutable in-memory state. See §5.4 for the authoritative shape."""

    slas: dict[str, SignedSLA] = field(default_factory=dict)
    sla_ingestion_times: dict[str, datetime] = field(default_factory=dict)
    claims: list[tuple[datetime, SignedClaim]] = field(default_factory=list)
    complaints: list[tuple[datetime, SignedComplaint]] = field(default_factory=list)
    ticker_events: Deque[TickerEvent] = field(
        default_factory=lambda: deque(maxlen=TICKER_MAXLEN)
    )

    def reset(self) -> None:
        """Clear everything. Backs ``POST /admin/reset`` (§5.2)."""
        self.slas.clear()
        self.sla_ingestion_times.clear()
        self.claims.clear()
        self.complaints.clear()
        self.ticker_events.clear()

    def add_sla(self, sla: SignedSLA, ingested_at: datetime) -> None:
        self.slas[sla.payload.sla_id] = sla
        self.sla_ingestion_times[sla.payload.sla_id] = ingested_at

    def get_sla(self, sla_id: str) -> Optional[SignedSLA]:
        return self.slas.get(sla_id)

    def add_claim(self, claim: SignedClaim, ingested_at: datetime) -> None:
        self.claims.append((ingested_at, claim))

    def add_complaint(self, complaint: SignedComplaint, ingested_at: datetime) -> None:
        self.complaints.append((ingested_at, complaint))

    def append_ticker(self, event: TickerEvent) -> None:
        self.ticker_events.append(event)


__all__ = ["AggregatorStorage", "TickerEvent", "TICKER_MAXLEN"]
