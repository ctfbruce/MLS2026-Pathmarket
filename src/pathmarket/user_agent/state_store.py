"""User-AS persistent state (``DESIGN.md`` §8.3).

Round-trips ``UserAgentState`` (identity + policy + portfolio + routing
decisions) through a single JSON file. Writes are atomic: serialize to a
sibling ``*.tmp``, then ``os.replace`` onto the target path so the file the
process reads is never half-written.

Corruption policy (§13 D1): if the existing file cannot be parsed or the
``schema_version`` does not match the current ``SCHEMA_VERSION``, log a
warning and initialize a fresh state with the caller-supplied defaults.
Everything here is one small dataclass and one load/save pair; the API
layer lives in :mod:`pathmarket.user_agent.app`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pathmarket.aggregator.models import (
    PathHopModel,
    SignedClaimModel,
)
from pathmarket.canonical import iso8601_utc_now
from pathmarket.schemas import (
    PathHop,
    Policy,
    RoutingDecision,
    SLABounds,
    SignedClaim,
)


log = logging.getLogger("pathmarket.user_agent.state_store")


SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class UserAgentState:
    """In-memory projection of ``runtime/user_as_state.json`` (§8.3).

    Mutable by design — the user-agent process keeps one instance alive for
    its lifetime and rewrites the file atomically after every mutation.
    """

    isd_as: str
    display_name: str
    policy: Policy
    portfolio: list[SignedClaim] = field(default_factory=list)
    routing_decisions: dict[str, RoutingDecision] = field(default_factory=dict)
    last_saved_at: str = ""
    schema_version: int = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# (de)serialization
# ---------------------------------------------------------------------------


_UNCOVERED_TOLERANCES = {"never", "partial_ok", "anywhere"}
_COMPLAINT_SENSITIVITIES = {"strict", "moderate", "tolerant"}


def _policy_to_dict(p: Policy) -> dict[str, Any]:
    return {
        "max_price_per_gb": p.max_price_per_gb,
        "min_reputation_floor": p.min_reputation_floor,
        "required_bounds": {
            "latency_max_ms": p.required_bounds.latency_max_ms,
            "loss_max_ppm": p.required_bounds.loss_max_ppm,
            "bandwidth_min_kbps": p.required_bounds.bandwidth_min_kbps,
        },
        "alpha": p.alpha,
        "beta": p.beta,
        "uncovered_tolerance": p.uncovered_tolerance,
        "complaint_sensitivity": p.complaint_sensitivity,
        "reshop_on_reputation_drop": p.reshop_on_reputation_drop,
        "portfolio_redundancy": p.portfolio_redundancy,
    }


def _policy_from_dict(d: dict[str, Any]) -> Policy:
    rb = d.get("required_bounds") or {}
    ut = d["uncovered_tolerance"]
    cs = d["complaint_sensitivity"]
    if ut not in _UNCOVERED_TOLERANCES:
        raise ValueError(f"invalid uncovered_tolerance: {ut!r}")
    if cs not in _COMPLAINT_SENSITIVITIES:
        raise ValueError(f"invalid complaint_sensitivity: {cs!r}")
    floor = float(d["min_reputation_floor"])
    if not 0.0 <= floor <= 1.0:
        raise ValueError(f"min_reputation_floor out of [0,1]: {floor}")
    return Policy(
        max_price_per_gb=str(d["max_price_per_gb"]),
        min_reputation_floor=floor,
        required_bounds=SLABounds(
            latency_max_ms=rb.get("latency_max_ms"),
            loss_max_ppm=rb.get("loss_max_ppm"),
            bandwidth_min_kbps=rb.get("bandwidth_min_kbps"),
        ),
        alpha=float(d["alpha"]),
        beta=float(d["beta"]),
        uncovered_tolerance=ut,
        complaint_sensitivity=cs,
        reshop_on_reputation_drop=float(d["reshop_on_reputation_drop"]),
        portfolio_redundancy=int(d["portfolio_redundancy"]),
    )


def _routing_to_dict(r: RoutingDecision) -> dict[str, Any]:
    return {
        "dst_isd_as": r.dst_isd_as,
        "path": [{"isd_as": h.isd_as, "ingress": h.ingress, "egress": h.egress} for h in r.path],
        "applied_claims": dict(r.applied_claims),
        "updated_at": r.updated_at,
        "manual_override": bool(r.manual_override),
    }


def _routing_from_dict(d: dict[str, Any]) -> RoutingDecision:
    path: list[PathHop] = [PathHopModel(**h).to_dataclass() for h in d["path"]]
    applied_raw = d.get("applied_claims") or {}
    # JSON keys are strings; we store hop-index → claim_id with int keys in memory.
    applied: dict[int, str] = {int(k): str(v) for k, v in applied_raw.items()}
    return RoutingDecision(
        dst_isd_as=str(d["dst_isd_as"]),
        path=path,
        applied_claims=applied,
        updated_at=str(d["updated_at"]),
        manual_override=bool(d.get("manual_override", False)),
    )


def _state_to_dict(s: UserAgentState) -> dict[str, Any]:
    return {
        "isd_as": s.isd_as,
        "display_name": s.display_name,
        "schema_version": SCHEMA_VERSION,
        "policy": _policy_to_dict(s.policy),
        "portfolio": [
            SignedClaimModel.from_dataclass(c).model_dump() for c in s.portfolio
        ],
        "routing_decisions": {
            dst: _routing_to_dict(r) for dst, r in s.routing_decisions.items()
        },
        "last_saved_at": s.last_saved_at,
    }


def _state_from_dict(d: dict[str, Any]) -> UserAgentState:
    if int(d.get("schema_version", 0)) != SCHEMA_VERSION:
        raise ValueError(
            f"schema_version mismatch: got {d.get('schema_version')!r}, expected {SCHEMA_VERSION}"
        )
    portfolio = [SignedClaimModel.model_validate(c).to_dataclass() for c in d.get("portfolio") or []]
    routing = {
        str(k): _routing_from_dict(v) for k, v in (d.get("routing_decisions") or {}).items()
    }
    return UserAgentState(
        isd_as=str(d["isd_as"]),
        display_name=str(d["display_name"]),
        policy=_policy_from_dict(d["policy"]),
        portfolio=portfolio,
        routing_decisions=routing,
        last_saved_at=str(d.get("last_saved_at", "")),
    )


# ---------------------------------------------------------------------------
# Disk I/O
# ---------------------------------------------------------------------------


def save_state(path: Path, state: UserAgentState) -> None:
    """Atomically persist ``state`` to ``path``.

    Stamps ``last_saved_at`` on the saved payload (but not on the in-memory
    dataclass — callers who want that should re-read).
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = _state_to_dict(state)
    payload["last_saved_at"] = iso8601_utc_now()

    # Write to a same-directory tmp then atomically replace. Using NamedTemporaryFile
    # with delete=False so os.replace can target the finalized temp path even on
    # filesystems where cross-handle writes would be flushed late.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def load_or_init_state(
    path: Path,
    *,
    isd_as: str,
    display_name: str,
    default_policy: Policy,
) -> UserAgentState:
    """Load state from ``path`` or create a fresh one from the defaults.

    Falls back to a fresh state (without overwriting the file) if ``path``
    doesn't exist, contains invalid JSON, or has a stale ``schema_version``.
    Corruption is logged at WARNING so demo operators notice it.
    """

    path = Path(path)
    if not path.exists():
        return UserAgentState(
            isd_as=isd_as, display_name=display_name, policy=default_policy
        )

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return _state_from_dict(data)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        log.warning("state file %s unreadable (%s); starting fresh", path, e)
        return UserAgentState(
            isd_as=isd_as, display_name=display_name, policy=default_policy
        )


__all__ = [
    "SCHEMA_VERSION",
    "UserAgentState",
    "load_or_init_state",
    "save_state",
]
