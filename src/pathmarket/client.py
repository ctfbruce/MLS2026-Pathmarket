"""Aggregator client (``DESIGN.md`` §12.0 layout).

Defines :class:`AggregatorClient` — the Protocol the :class:`Agent` talks to —
and :class:`HttpAggregatorClient`, the httpx-backed implementation used by the
simulator and user agent against a running aggregator.

Unit tests substitute a fake in-memory client that implements the same
Protocol, so no HTTP is involved in the fast test tier.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from pathmarket.aggregator.models import (
    SignedClaimModel,
    SignedComplaintModel,
    SignedSLAModel,
)
from pathmarket.schemas import Score, SignedClaim, SignedComplaint, SignedSLA


class AggregatorClient(Protocol):
    """Minimal Protocol the :class:`Agent` depends on."""

    def list_active_slas(self) -> list[SignedSLA]: ...

    def get_sla(self, sla_id: str) -> SignedSLA | None: ...

    def get_scores(self) -> list[Score]: ...

    def get_score(self, isd_as: str) -> Score: ...

    def post_sla(self, sla: SignedSLA) -> dict[str, Any]: ...

    def post_claim(self, claim: SignedClaim) -> dict[str, Any]: ...

    def post_complaint(self, complaint: SignedComplaint) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Real HTTP implementation
# ---------------------------------------------------------------------------


def _score_from_json(js: dict[str, Any]) -> Score:
    return Score(isd_as=js["isd_as"], score=float(js["score"]), components=dict(js.get("components") or {}))


def _sla_from_json(js: dict[str, Any]) -> SignedSLA:
    return SignedSLAModel.model_validate(js).to_dataclass()


class HttpAggregatorClient:
    """httpx-backed AggregatorClient."""

    def __init__(self, base_url: str, *, timeout: float = 5.0) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    # -- reads -------------------------------------------------------------

    def list_active_slas(self) -> list[SignedSLA]:
        r = self._client.get("/slas", params={"active_only": "true"})
        r.raise_for_status()
        return [_sla_from_json(s) for s in r.json()["slas"]]

    def get_sla(self, sla_id: str) -> SignedSLA | None:
        r = self._client.get(f"/slas/{sla_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return _sla_from_json(r.json())

    def get_scores(self) -> list[Score]:
        r = self._client.get("/scores")
        r.raise_for_status()
        return [_score_from_json(s) for s in r.json()["scores"]]

    def get_score(self, isd_as: str) -> Score:
        r = self._client.get(f"/score/{isd_as}")
        r.raise_for_status()
        return _score_from_json(r.json())

    # -- writes ------------------------------------------------------------

    def post_sla(self, sla: SignedSLA) -> dict[str, Any]:
        r = self._client.post("/sla", json=SignedSLAModel.from_dataclass(sla).model_dump())
        return r.json()

    def post_claim(self, claim: SignedClaim) -> dict[str, Any]:
        r = self._client.post("/claim", json=SignedClaimModel.from_dataclass(claim).model_dump())
        return r.json()

    def post_complaint(self, complaint: SignedComplaint) -> dict[str, Any]:
        r = self._client.post(
            "/complaint", json=SignedComplaintModel.from_dataclass(complaint).model_dump()
        )
        return r.json()

    def close(self) -> None:
        self._client.close()


__all__ = ["AggregatorClient", "HttpAggregatorClient"]
