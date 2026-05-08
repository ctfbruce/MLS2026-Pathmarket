"""FastAPI app factory for the aggregator (``DESIGN.md`` §5)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pathmarket.aggregator.scoring import (
    all_cosigner_ases,
    score_for_as,
    scores_for_all,
)
from pathmarket.aggregator.storage import AggregatorStorage, TickerEvent
from pathmarket.aggregator.validation import (
    VerificationStep,
    steps_as_dicts,
    validate_claim_submission,
    validate_complaint_submission,
    validate_sla_submission,
)
from pathmarket.scorer.scorer import ScoringConfig
from pathmarket.verifier import StaticKeyVerifier
from pathmarket.verifier.protocol import Verifier


REPUTATION_CHANGE_THRESHOLD = 0.01


def create_app(
    *,
    storage: Optional[AggregatorStorage] = None,
    verifier: Optional[Verifier] = None,
    enable_admin_endpoints: bool = False,
    dedup_window_minutes: int = 5,
    scoring_config: Optional[ScoringConfig] = None,
) -> FastAPI:
    """Build a fresh FastAPI app with isolated storage.

    Pass ``storage`` and ``verifier`` for deterministic tests; defaults create
    empty instances. ``enable_admin_endpoints`` gates ``POST /admin/reset``
    per the amendment to §5.2.
    """

    if storage is None:
        storage = AggregatorStorage()
    if verifier is None:
        verifier = StaticKeyVerifier(keyring={})
    if scoring_config is None:
        scoring_config = ScoringConfig()

    app = FastAPI(title="PathMarket aggregator", version="2.0.0")
    # Local-demo CORS: the UI served by the user agent (127.0.0.1:8090) fetches
    # directly from here. Permissive because everything is loopback-only.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.storage = storage
    app.state.verifier = verifier
    app.state.enable_admin_endpoints = enable_admin_endpoints
    app.state.scoring_config = scoring_config

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/sla")
    async def post_sla(request: Request) -> JSONResponse:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            return _reject_sla(
                [VerificationStep("parse_json", False, f"invalid JSON: {e.msg}")]
            )

        result = validate_sla_submission(body, storage=storage, verifier=verifier)
        if not result.accepted:
            return _reject_sla(result.steps)

        now = datetime.now(timezone.utc)
        storage.add_sla(result.sla, now)
        storage.append_ticker(
            TickerEvent(
                type="sla_signed",
                timestamp=now,
                detail=_sla_ticker_detail(result.sla),
            )
        )
        return JSONResponse(
            {
                "status": "accepted",
                "sla_id": result.sla.payload.sla_id,
                "verification_steps": steps_as_dicts(result.steps),
            },
            status_code=201,
        )

    @app.post("/claim")
    async def post_claim(request: Request) -> JSONResponse:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            return JSONResponse({"error": f"parse_json: invalid JSON: {e.msg}"}, status_code=400)

        now = datetime.now(timezone.utc)
        result = validate_claim_submission(
            body, storage=storage, verifier=verifier, now=now
        )
        if result.claim is None:
            return JSONResponse({"error": result.error}, status_code=400)

        claim = result.claim
        storage.add_claim(claim, now)
        storage.append_ticker(
            TickerEvent(
                type="claim",
                timestamp=now,
                detail=_claim_ticker_detail(claim, storage),
            )
        )
        return JSONResponse(
            {"claim_id": claim.payload.claim_id, "status": "accepted"},
            status_code=201,
        )

    @app.post("/complaint")
    async def post_complaint(request: Request) -> JSONResponse:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            return JSONResponse(
                {"error": f"parse_json: invalid JSON: {e.msg}"}, status_code=400
            )

        now = datetime.now(timezone.utc)
        result = validate_complaint_submission(
            body,
            storage=storage,
            verifier=verifier,
            now=now,
            dedup_window_minutes=dedup_window_minutes,
        )
        if result.complaint is None:
            # Dedup is idempotent: the first identical complaint is already
            # recorded, so return 200 instead of 400. Simulator buyers
            # re-sample every tick and would otherwise spam 400s.
            if result.error and result.error.startswith("duplicate_complaint"):
                return JSONResponse({"dedup": True, "detail": result.error}, status_code=200)
            return JSONResponse({"error": result.error}, status_code=400)

        complaint = result.complaint
        sla = storage.get_sla(complaint.payload.sla_id)
        # Snapshot cosigner scores BEFORE ingestion so we can detect changes.
        affected = list(sla.payload.cosigners) if sla is not None else []
        before = {a: score_for_as(a, storage, scoring_config, now).score for a in affected}

        storage.add_complaint(complaint, now)
        storage.append_ticker(
            TickerEvent(
                type="complaint",
                timestamp=now,
                detail=_complaint_ticker_detail(complaint),
            )
        )

        # Synthesize reputation_change events per §5.2 /ticker.
        after = {a: score_for_as(a, storage, scoring_config, now).score for a in affected}
        for isd_as in affected:
            delta = abs(after[isd_as] - before[isd_as])
            if delta >= REPUTATION_CHANGE_THRESHOLD:
                storage.append_ticker(
                    TickerEvent(
                        type="reputation_change",
                        timestamp=now,
                        detail=(
                            f"AS {isd_as} reputation: "
                            f"{before[isd_as]:.2f} → {after[isd_as]:.2f}"
                        ),
                    )
                )

        return JSONResponse(
            {"complaint_id": complaint.payload.complaint_id, "status": "accepted"},
            status_code=201,
        )

    # ------------------------------------------------------------------
    # Query endpoints (§5.2)
    # ------------------------------------------------------------------

    @app.get("/slas")
    def get_slas(
        src_isd_as: Optional[str] = None,
        dst_isd_as: Optional[str] = None,
        covers_hop: Optional[str] = None,
        active_only: bool = True,
    ) -> dict:
        now = datetime.now(timezone.utc)
        out = []
        for sla in storage.slas.values():
            p = sla.payload
            if src_isd_as is not None and p.path[0].isd_as != src_isd_as:
                continue
            if dst_isd_as is not None and p.path[-1].isd_as != dst_isd_as:
                continue
            if covers_hop is not None:
                if ">" not in covers_hop:
                    continue
                src, dst = covers_hop.split(">", 1)
                found = any(
                    p.path[i].isd_as == src and p.path[i + 1].isd_as == dst
                    for i in range(len(p.path) - 1)
                )
                if not found:
                    continue
            if active_only and not _sla_active(sla, now):
                continue
            out.append(_sla_json(sla))
        return {"slas": out}

    @app.get("/slas/{sla_id:path}")
    def get_sla(sla_id: str) -> JSONResponse:
        sla = storage.get_sla(sla_id)
        if sla is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(_sla_json(sla))

    @app.get("/claims")
    def get_claims(
        claimant: Optional[str] = None,
        sla_id: Optional[str] = None,
        active_only: bool = True,
    ) -> dict:
        now = datetime.now(timezone.utc)
        out = []
        for _, claim in storage.claims:
            p = claim.payload
            if claimant is not None and p.claimant != claimant:
                continue
            if sla_id is not None and p.sla_id != sla_id:
                continue
            if active_only:
                sla = storage.get_sla(p.sla_id)
                if sla is None or not _sla_active(sla, now):
                    continue
            out.append(_claim_json(claim))
        return {"claims": out}

    @app.get("/complaints")
    def get_complaints(
        sla_id: Optional[str] = None,
        complainant: Optional[str] = None,
        metric: Optional[str] = None,
        since: Optional[str] = None,
        search: Optional[str] = None,
    ) -> dict:
        out = []
        since_dt = _parse_iso_opt(since)
        needle = search.lower() if search else None
        for _, c in storage.complaints:
            p = c.payload
            if sla_id is not None and p.sla_id != sla_id:
                continue
            if complainant is not None and p.complainant != complainant:
                continue
            if metric is not None and p.metric != metric:
                continue
            if since_dt is not None:
                try:
                    observed = _parse_iso(p.observed_at)
                except ValueError:
                    continue
                if observed < since_dt:
                    continue
            if needle is not None and needle not in p.note.lower():
                continue
            out.append(_complaint_json(c))
        return {"complaints": out}

    @app.get("/score/{isd_as}")
    def get_score(isd_as: str) -> dict:
        now = datetime.now(timezone.utc)
        s = score_for_as(isd_as, storage, scoring_config, now)
        return _score_json(s)

    @app.get("/scores")
    def get_scores() -> dict:
        now = datetime.now(timezone.utc)
        return {"scores": [_score_json(s) for s in scores_for_all(storage, scoring_config, now)]}

    @app.get("/market/summary")
    def market_summary() -> dict:
        from decimal import Decimal

        now = datetime.now(timezone.utc)
        active_slas = [s for s in storage.slas.values() if _sla_active(s, now)]
        active_claims = [
            c for _, c in storage.claims
            if _sla_active(storage.get_sla(c.payload.sla_id), now)
            if storage.get_sla(c.payload.sla_id) is not None
        ]

        one_hour_ago = now.timestamp() - 3600
        complaints_last_hour = sum(
            1 for ts, _ in storage.complaints if ts.timestamp() >= one_hour_ago
        )

        # Tier classification by price per §5.2.
        tiers = {"premium": [], "mid": [], "commodity": []}
        tier_ases: dict[str, set[str]] = {"premium": set(), "mid": set(), "commodity": set()}
        for sla in active_slas:
            price = Decimal(sla.payload.price_per_gb)
            tier = _price_tier(price)
            tiers[tier].append(price)
            tier_ases[tier].update(sla.payload.cosigners)

        # Mean reputation by tier: average score across the cosigners in that tier.
        all_scores = {s.isd_as: s.score for s in scores_for_all(storage, scoring_config, now)}
        mean_rep = {}
        for tier, ases in tier_ases.items():
            vals = [all_scores.get(a, 1.0) for a in ases]
            mean_rep[tier] = round(sum(vals) / len(vals), 4) if vals else None

        mean_price = {}
        for tier, prices in tiers.items():
            if prices:
                mean_price[tier] = str((sum(prices) / len(prices)).quantize(Decimal("0.001")))
            else:
                mean_price[tier] = None

        return {
            "total_slas_active": len(active_slas),
            "total_claims_active": len(active_claims),
            "total_complaints_last_hour": complaints_last_hour,
            "mean_reputation_by_tier": mean_rep,
            "mean_price_by_tier": mean_price,
        }

    @app.get("/ticker")
    def ticker(limit: int = 20) -> dict:
        # Clamp per §5.2: default 20, max 100.
        limit = max(0, min(limit, 100))
        # newest-first (2026-04-19 amendment).
        events = sorted(storage.ticker_events, key=lambda e: e.timestamp, reverse=True)
        return {
            "events": [
                {
                    "type": e.type,
                    "timestamp": e.timestamp.isoformat().replace("+00:00", "Z"),
                    "detail": e.detail,
                }
                for e in events[:limit]
            ]
        }

    if enable_admin_endpoints:
        @app.post("/admin/reset")
        def admin_reset() -> dict:
            storage.reset()
            return {"status": "reset"}

    return app


def _reject_sla(steps: list[VerificationStep]) -> JSONResponse:
    return JSONResponse(
        {
            "status": "rejected",
            "sla_id": None,
            "verification_steps": steps_as_dicts(steps),
        },
        status_code=400,
    )


def _sla_ticker_detail(sla) -> str:  # type: ignore[no-untyped-def]
    p = sla.payload
    short_id = p.sla_id.split(":", 1)[-1][:8]
    hop_count = max(len(p.path) - 1, 0)
    return (
        f"SLA {short_id} signed by {len(p.cosigners)}-AS consortium · "
        f"{hop_count} hops · {p.price_per_gb} CHF/GB"
    )


def _claim_ticker_detail(claim, storage: AggregatorStorage) -> str:  # type: ignore[no-untyped-def]
    p = claim.payload
    short_id = p.sla_id.split(":", 1)[-1][:8]
    return (
        f"AS {p.claimant} claimed SLA {short_id} · "
        f"{p.gb_purchased} GB · {p.price_paid_chf} CHF"
    )


def _complaint_ticker_detail(complaint) -> str:  # type: ignore[no-untyped-def]
    p = complaint.payload
    short_id = p.sla_id.split(":", 1)[-1][:8]
    return (
        f"Complaint filed by AS {p.complainant} against SLA {short_id} · "
        f"{p.metric} violation"
    )


def _sla_active(sla, now: datetime) -> bool:  # type: ignore[no-untyped-def]
    if sla is None:
        return False
    try:
        v_from = _parse_iso(sla.payload.valid_from)
        v_until = _parse_iso(sla.payload.valid_until)
    except ValueError:
        return False
    return v_from <= now < v_until


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _parse_iso_opt(ts: Optional[str]) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        return _parse_iso(ts)
    except ValueError:
        return None


def _price_tier(price) -> str:  # type: ignore[no-untyped-def]
    from decimal import Decimal
    if price >= Decimal("0.10"):
        return "premium"
    if price < Decimal("0.03"):
        return "commodity"
    return "mid"


def _sla_json(sla) -> dict:  # type: ignore[no-untyped-def]
    from pathmarket.aggregator.models import SignedSLAModel
    return SignedSLAModel.from_dataclass(sla).model_dump()


def _claim_json(claim) -> dict:  # type: ignore[no-untyped-def]
    from pathmarket.aggregator.models import SignedClaimModel
    return SignedClaimModel.from_dataclass(claim).model_dump()


def _complaint_json(complaint) -> dict:  # type: ignore[no-untyped-def]
    from pathmarket.aggregator.models import SignedComplaintModel
    return SignedComplaintModel.from_dataclass(complaint).model_dump()


def _score_json(score) -> dict:  # type: ignore[no-untyped-def]
    from pathmarket.aggregator.models import ScoreModel
    return ScoreModel.from_dataclass(score).model_dump()


def main() -> None:  # pragma: no cover — CLI entry point used in prod, not tests.
    """``pathmarket-aggregator`` console script (see ``pyproject.toml``, §11.1).

    Reads ``config.toml`` for bind host/port, keyring path, scoring knobs, and
    the admin-endpoints gate. Without this, a default empty keyring would
    reject every signed artifact, which is exactly the symptom we hit when the
    aggregator was first wired up end-to-end.
    """

    import argparse
    import tomllib
    from pathlib import Path

    import uvicorn

    from pathmarket.scorer.scorer import ScoringConfig
    from pathmarket.verifier import StaticKeyVerifier

    p = argparse.ArgumentParser(prog="pathmarket-aggregator")
    p.add_argument("--config", type=Path, default=Path("config.toml"))
    args = p.parse_args()

    cfg = tomllib.loads(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}
    server = cfg.get("server") or {}
    paths = cfg.get("paths") or {}
    scoring = cfg.get("scoring") or {}
    admin = cfg.get("admin") or {}

    keyring_file = Path(paths.get("keyring_file", "keyring.json"))
    if keyring_file.exists():
        verifier = StaticKeyVerifier.from_file(keyring_file)
    else:
        verifier = StaticKeyVerifier(keyring={})

    scoring_config = ScoringConfig(
        k=int(scoring.get("k", 3)),
        window_minutes=int(scoring.get("window_minutes", 5)),
        violation_weight=float(scoring.get("violation_weight", 0.1)),
        decay_time_constant_hours=float(scoring.get("decay_time_constant_hours", 100)),
    )

    app = create_app(
        verifier=verifier,
        scoring_config=scoring_config,
        enable_admin_endpoints=bool(admin.get("enable_admin_endpoints", False)),
    )
    uvicorn.run(
        app,
        host=str(server.get("bind_host", "127.0.0.1")),
        port=int(server.get("bind_port", 8080)),
    )


__all__ = ["create_app", "main"]
