"""In-memory fake :class:`AggregatorClient` for agent-layer tests.

Implements the Protocol in :mod:`pathmarket.client` without touching HTTP.
The aggregator's validation chain is intentionally *not* re-run here — these
are unit tests of agent-side logic, not end-to-end tests. The full chain is
exercised by the TestClient-driven aggregator tests.
"""

from __future__ import annotations

from typing import Any

from pathmarket.schemas import Score, SignedClaim, SignedComplaint, SignedSLA


class FakeAggregatorClient:
    def __init__(
        self,
        *,
        slas: list[SignedSLA] | None = None,
        scores: list[Score] | None = None,
    ) -> None:
        self._slas: dict[str, SignedSLA] = {s.payload.sla_id: s for s in (slas or [])}
        self._scores: dict[str, Score] = {s.isd_as: s for s in (scores or [])}
        self.posted_slas: list[SignedSLA] = []
        self.posted_claims: list[SignedClaim] = []
        self.posted_complaints: list[SignedComplaint] = []

    # Protocol surface -----------------------------------------------------

    def list_active_slas(self) -> list[SignedSLA]:
        return list(self._slas.values())

    def get_sla(self, sla_id: str) -> SignedSLA | None:
        return self._slas.get(sla_id)

    def get_scores(self) -> list[Score]:
        return list(self._scores.values())

    def get_score(self, isd_as: str) -> Score:
        return self._scores.get(
            isd_as, Score(isd_as=isd_as, score=1.0, components={})
        )

    def post_sla(self, sla: SignedSLA) -> dict[str, Any]:
        self.posted_slas.append(sla)
        self._slas[sla.payload.sla_id] = sla
        return {"status": "accepted", "sla_id": sla.payload.sla_id}

    def post_claim(self, claim: SignedClaim) -> dict[str, Any]:
        self.posted_claims.append(claim)
        return {"status": "accepted", "claim_id": claim.payload.claim_id}

    def post_complaint(self, complaint: SignedComplaint) -> dict[str, Any]:
        self.posted_complaints.append(complaint)
        return {"status": "accepted", "complaint_id": complaint.payload.complaint_id}


__all__ = ["FakeAggregatorClient"]
