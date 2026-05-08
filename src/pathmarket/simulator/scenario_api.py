"""Scenario control API (``DESIGN.md`` §9.12).

FastAPI app bound on ``127.0.0.1:8081`` in the live simulator, exposing one
POST endpoint per presenter scenario. Handlers delegate to
:mod:`pathmarket.simulator.scenarios`; all heavy lifting is in those
functions. This module only wires HTTP routing and JSON shaping.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from pathmarket.simulator.orchestrator import Orchestrator
from pathmarket.simulator.scenarios import (
    bad_actor_cascade,
    cloud_bargain,
    degrade_as,
    fix_as,
    hospital_reshops,
    reset_market,
)


class ResetRequest(BaseModel):
    preseed_count: int | None = None


class HospitalReshopsRequest(BaseModel):
    target_sla_id: str | None = None


class CloudBargainRequest(BaseModel):
    price_per_gb: str | None = None


class BadActorRequest(BaseModel):
    isd_as: str


def create_scenario_app(
    orchestrator: Orchestrator,
    *,
    on_reset: Any | None = None,
    default_preseed_count: int = 5,
) -> FastAPI:
    """Build a :class:`FastAPI` app that manipulates ``orchestrator``.

    ``on_reset`` is an optional zero-arg callable the reset handler invokes
    *before* re-seeding — the simulator main.py plugs in a lambda that hits
    the aggregator's ``POST /admin/reset`` here, so a full reset chain is a
    single button press in the UI.
    """

    app = FastAPI(title="pathmarket-simulator-scenarios", version="2.0.0")
    # Permissive CORS — UI is served by the user agent at a different port.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/scenarios/reset")
    def reset(req: ResetRequest) -> dict[str, Any]:
        count = req.preseed_count if req.preseed_count is not None else default_preseed_count
        if on_reset is not None:
            on_reset()
        n = reset_market(orchestrator, preseed_count=count)
        return {"triggered": "reset", "preseeded": n}

    @app.post("/scenarios/hospital_reshops")
    def hospital(req: HospitalReshopsRequest) -> dict[str, Any]:
        try:
            sla_id = hospital_reshops(orchestrator, target_sla_id=req.target_sla_id)
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"triggered": "hospital_reshops", "sla_id": sla_id}

    @app.post("/scenarios/cloud_bargain")
    def cloud(req: CloudBargainRequest) -> dict[str, Any]:
        try:
            sla_id = cloud_bargain(
                orchestrator,
                price_per_gb=req.price_per_gb or "0.008",
            )
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"triggered": "cloud_bargain", "sla_id": sla_id}

    @app.post("/scenarios/bad_actor_cascade")
    def bad(req: BadActorRequest) -> dict[str, Any]:
        # Back-compat alias — prefer /scenarios/degrade_as.
        try:
            flipped = bad_actor_cascade(orchestrator, isd_as=req.isd_as)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"triggered": "bad_actor_cascade", "sla_ids": flipped}

    @app.post("/scenarios/degrade_as")
    def degrade(req: BadActorRequest) -> dict[str, Any]:
        try:
            flipped = degrade_as(orchestrator, isd_as=req.isd_as)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"triggered": "degrade_as", "isd_as": req.isd_as, "sla_ids": flipped}

    @app.post("/scenarios/fix_as")
    def fix(req: BadActorRequest) -> dict[str, Any]:
        try:
            restored = fix_as(orchestrator, isd_as=req.isd_as)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"triggered": "fix_as", "isd_as": req.isd_as, "sla_ids": restored}

    return app


__all__ = ["create_scenario_app"]
