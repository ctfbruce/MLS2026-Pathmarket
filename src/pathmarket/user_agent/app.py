"""User-AS local HTTP API (``DESIGN.md`` §8.4).

FastAPI app served at ``http://127.0.0.1:8090``:

- ``GET /`` serves the terminal UI export.
- ``GET|POST|PUT /local/*`` is the user-AS control plane used by the UI.

Design split:

- Pure state + agent actions live on :class:`UserAgentService`; the endpoints
  are thin translation-and-persist wrappers.
- Every mutation persists atomically via :func:`state_store.save_state`
  before the response returns — the state on disk is always what the UI
  observes via ``GET /local/state``.

Candidate routes (:func:`candidate_routes`) are added in D3.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from pathmarket.aggregator.models import (
    AttachmentModel,
    SignedSLAModel,
)
from pathmarket.agent.agent import Agent
from pathmarket.canonical import iso8601_utc_now
from pathmarket.client import AggregatorClient
from pathmarket.path_discovery.protocol import PathDiscovery
from pathmarket.schemas import (
    Attachment,
    PathHop,
    Policy,
    RoutingDecision,
    SignedClaim,
)
from pathmarket.user_agent.consumed_walk import ConsumedGBWalk
from pathmarket.user_agent.routing import build_candidate_routes
from pathmarket.user_agent.state_store import (
    UserAgentState,
    _policy_from_dict,
    _policy_to_dict,
    _routing_to_dict,
    _state_to_dict,
    save_state,
)


log = logging.getLogger("pathmarket.user_agent.app")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class UserAgentService:
    """Stateful facade for local mutations. Persists to disk on every change."""

    def __init__(
        self,
        *,
        state: UserAgentState,
        state_file: Path,
        aggregator_client: AggregatorClient,
        private_key: Ed25519PrivateKey,
        path_discovery: PathDiscovery,
    ) -> None:
        self.state = state
        self.state_file = Path(state_file)
        self.aggregator_client = aggregator_client
        self.private_key = private_key
        self.path_discovery = path_discovery
        # The underlying Agent; we rebuild the policy field on the fly because
        # operator policy edits must take effect without re-wiring the object.
        # Per §8.6, the user AS never auto-files complaints. The user-agent
        # process drives the Agent only via explicit local-API calls (claim,
        # complaint, upload_sla, routing) — there is no tick-driven quality
        # simulation here — so no extra `is_user_as` flag is needed on Agent.
        self.agent = Agent(
            isd_as=state.isd_as,
            private_key=private_key,
            policy=state.policy,
            aggregator_client=aggregator_client,
            path_discovery=path_discovery,
        )
        # Keep the agent's portfolio aligned with the state's portfolio so
        # existing claims survive a restart.
        self.agent.portfolio = list(state.portfolio)
        self.agent.routing_decisions = dict(state.routing_decisions)
        # In-memory consumed-GB walk (§8.5). Not persisted.
        self.consumed_walk = ConsumedGBWalk()

    # -- persistence ------------------------------------------------------

    def _persist(self) -> None:
        self.state.policy = self.agent.policy
        self.state.portfolio = list(self.agent.portfolio)
        self.state.routing_decisions = dict(self.agent.routing_decisions)
        save_state(self.state_file, self.state)

    # -- policy -----------------------------------------------------------

    def set_policy(self, new_policy: Policy) -> None:
        self.agent.policy = new_policy
        self.state.policy = new_policy
        self._persist()

    # -- actions ----------------------------------------------------------

    def claim_sla(self, *, sla_id: str, gb: int) -> SignedClaim:
        sla = self.aggregator_client.get_sla(sla_id)
        if sla is None:
            raise KeyError(f"unknown sla_id: {sla_id}")
        claim = self.agent.claim_sla(sla, gb=gb, claimed_at=iso8601_utc_now())
        self._persist()
        return claim

    def file_complaint(
        self,
        *,
        sla_id: str,
        metric: str,
        measured_value: int,
        note: str = "",
        attachments: list[Attachment] | None = None,
    ) -> Any:
        sla = self.aggregator_client.get_sla(sla_id)
        if sla is None:
            raise KeyError(f"unknown sla_id: {sla_id}")
        complaint = self.agent.file_complaint(
            sla,
            metric=metric,
            measured_value=measured_value,
            observed_at=iso8601_utc_now(),
            note=note,
            attachments=attachments,
        )
        # Complaints aren't stored locally (per §8.4) — no persist needed.
        return complaint

    def upload_sla(self, body: dict[str, Any]) -> dict[str, Any]:
        sla = SignedSLAModel.model_validate(body).to_dataclass()
        return self.aggregator_client.post_sla(sla)

    def set_routing(self, dst_isd_as: str, decision: RoutingDecision) -> None:
        self._validate_routing(decision)
        self.agent.routing_decisions[dst_isd_as] = decision
        self._persist()

    def _validate_routing(self, decision: RoutingDecision) -> None:
        """Each referenced claim_id must exist in the portfolio and cover its hop."""

        by_id = {c.payload.claim_id: c for c in self.agent.portfolio}
        for hop_index, claim_id in decision.applied_claims.items():
            if claim_id not in by_id:
                raise ValueError(
                    f"applied_claims references unknown claim_id {claim_id}"
                )
            if not isinstance(hop_index, int) or not 0 <= hop_index < len(decision.path):
                raise ValueError(f"hop_index {hop_index} out of range for path")
            claim = by_id[claim_id]
            sla = self.aggregator_client.get_sla(claim.payload.sla_id)
            if sla is None:
                raise ValueError(
                    f"claim {claim_id} references SLA not on aggregator"
                )
            hop = decision.path[hop_index]
            sla_hops = [h.isd_as for h in sla.payload.path]
            if hop.isd_as not in sla_hops:
                raise ValueError(
                    f"claim {claim_id}'s SLA does not cover hop {hop.isd_as}"
                )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ClaimRequest(BaseModel):
    sla_id: str
    gb_purchased: int


class ComplaintRequest(BaseModel):
    sla_id: str
    metric: str
    measured_value: int
    note: str = ""
    attachments: list[AttachmentModel] | None = None


class PolicyRequest(BaseModel):
    max_price_per_gb: str
    min_reputation_floor: float
    required_bounds: dict[str, Any] = {}
    alpha: float
    beta: float
    uncovered_tolerance: str
    complaint_sensitivity: str
    reshop_on_reputation_drop: float
    portfolio_redundancy: int


class RoutingRequest(BaseModel):
    path: list[dict[str, Any]]
    applied_claims: dict[str, str] = {}
    manual_override: bool = True


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(service: UserAgentService) -> FastAPI:
    app = FastAPI(title="pathmarket-user-agent", version="2.0.0")

    @app.get("/local/state")
    def get_state() -> dict[str, Any]:
        return _state_to_dict(service.state)

    @app.get("/local/portfolio")
    def get_portfolio() -> dict[str, Any]:
        consumed = service.consumed_walk.advance(service.agent.portfolio)
        return {
            "claims": [
                {
                    "claim": _serialize_claim(c),
                    "consumed_gb": consumed.get(c.payload.claim_id, 0.0),
                }
                for c in service.agent.portfolio
            ]
        }

    @app.put("/local/policy")
    def put_policy(req: PolicyRequest) -> dict[str, Any]:
        try:
            new_policy = _policy_from_dict(req.model_dump())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        service.set_policy(new_policy)
        return {"policy": _policy_to_dict(service.state.policy)}

    @app.post("/local/actions/claim")
    def post_claim(req: ClaimRequest) -> dict[str, Any]:
        try:
            claim = service.claim_sla(sla_id=req.sla_id, gb=req.gb_purchased)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _serialize_claim(claim)

    @app.post("/local/actions/complaint")
    def post_complaint(req: ComplaintRequest) -> dict[str, Any]:
        atts = [a.to_dataclass() for a in req.attachments] if req.attachments else None
        try:
            complaint = service.file_complaint(
                sla_id=req.sla_id,
                metric=req.metric,
                measured_value=req.measured_value,
                note=req.note,
                attachments=atts,
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _serialize_complaint(complaint)

    @app.post("/local/actions/upload_sla")
    def post_upload_sla(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return service.upload_sla(body)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid SLA: {e}")

    @app.get("/local/candidate_routes")
    def get_candidate_routes(dst: str) -> dict[str, Any]:
        return build_candidate_routes(
            src_isd_as=service.state.isd_as,
            dst_isd_as=dst,
            portfolio=service.agent.portfolio,
            path_discovery=service.path_discovery,
            aggregator_client=service.aggregator_client,
        )

    @app.put("/local/routing/{dst_isd_as}")
    def put_routing(dst_isd_as: str, req: RoutingRequest) -> dict[str, Any]:
        try:
            path = [PathHop(isd_as=h["isd_as"], ingress=int(h["ingress"]),
                            egress=int(h["egress"])) for h in req.path]
            decision = RoutingDecision(
                dst_isd_as=dst_isd_as,
                path=path,
                applied_claims={int(k): str(v) for k, v in req.applied_claims.items()},
                updated_at=iso8601_utc_now(),
                manual_override=bool(req.manual_override),
            )
            service.set_routing(dst_isd_as, decision)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"dst_isd_as": dst_isd_as, "routing": _routing_to_dict(decision)}

    return app


# ---------------------------------------------------------------------------
# (de)serializer helpers
# ---------------------------------------------------------------------------


def _serialize_claim(claim: SignedClaim) -> dict[str, Any]:
    from pathmarket.aggregator.models import SignedClaimModel
    return SignedClaimModel.from_dataclass(claim).model_dump()


def _serialize_complaint(complaint: Any) -> dict[str, Any]:
    from pathmarket.aggregator.models import SignedComplaintModel
    return SignedComplaintModel.from_dataclass(complaint).model_dump()


__all__ = ["UserAgentService", "create_app"]
