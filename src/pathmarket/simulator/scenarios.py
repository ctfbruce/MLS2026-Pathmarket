"""Presenter scenario triggers (``DESIGN.md`` §9.11, §9.12).

Each scenario is a direct mutation of :class:`Orchestrator` state. The
mutation takes effect immediately; the orchestrator's next tick(s) produce
the visible consequences (new SLAs, claim re-shops, complaint cascades).

Scenarios are the *smallest possible* nudge to the simulation. The intent
is not to script the demo — the policies already do that. We just push the
world at one point and let the tick loop play it out.
"""

from __future__ import annotations

from datetime import timedelta

from pathmarket.agent.simulated_quality import QualityModel, model_for_profile
from pathmarket.schemas import PathHop, SLABounds
from pathmarket.simulator.orchestrator import Orchestrator, SLATemplate


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def reset_market(orchestrator: Orchestrator, *, preseed_count: int = 5) -> int:
    """Clear simulator-side state and re-run pre-seeding.

    Aggregator-side state is the caller's responsibility — the simulator
    main process calls ``POST /admin/reset`` on the aggregator before
    invoking this, per §9.12.

    Returns the number of SLAs published during re-seeding.
    """

    orchestrator.quality_models.clear()
    orchestrator.sla_bounds.clear()
    orchestrator.live_sla_ids.clear()
    for agent in orchestrator.agents.values():
        agent.portfolio.clear()
        agent.routing_decisions.clear()
    slas = orchestrator.pre_seed(count=preseed_count)
    return len(slas)


# ---------------------------------------------------------------------------
# Hospital reshops — a targeted SLA goes bad
# ---------------------------------------------------------------------------


def _always_violating_model(bounds: SLABounds) -> QualityModel:
    """Return a :class:`QualityModel` whose every sample violates ``bounds``.

    The model picks hard constants deliberately out-of-bounds; stddev is
    kept non-zero so samples are still varied enough to look realistic in
    the ticker.
    """

    def _badly(high: int | None, low: bool = False) -> tuple[float | None, float | None]:
        if high is None:
            return (None, None)
        if low:
            # For bandwidth: produce samples *below* the min bound.
            return (high * 0.25, max(high * 0.02, 1.0))
        # For latency/loss: produce samples well *above* the bound.
        return (high * 4.0, max(high * 0.10, 1.0))

    lat_m, lat_s = _badly(bounds.latency_max_ms)
    loss_m, loss_s = _badly(bounds.loss_max_ppm)
    bw_m, bw_s = _badly(bounds.bandwidth_min_kbps, low=True)
    return QualityModel(
        latency_mean_ms=lat_m,
        latency_stddev_ms=lat_s,
        loss_mean_ppm=loss_m,
        loss_stddev_ppm=loss_s,
        bandwidth_mean_kbps=bw_m,
        bandwidth_stddev_kbps=bw_s,
    )


def hospital_reshops(
    orchestrator: Orchestrator, *, target_sla_id: str | None = None
) -> str:
    """Degrade one SLA's simulated quality so the Hospital reshops.

    If ``target_sla_id`` is ``None``, the most recently published SLA is
    degraded. Returns the sla_id that was degraded.

    The Hospital's policy (``complaint_sensitivity = "strict"``, tight
    ``required_bounds``, high ``reputation_floor``) ensures that the next
    tick on which the Hospital evaluates quality produces a complaint, and
    the tick after its policy sees the reputation drop and looks elsewhere.
    """

    if target_sla_id is None:
        if not orchestrator.live_sla_ids:
            raise RuntimeError("no live SLAs to degrade")
        target_sla_id = orchestrator.live_sla_ids[-1]
    if target_sla_id not in orchestrator.sla_bounds:
        raise KeyError(f"unknown sla_id: {target_sla_id}")
    bounds = orchestrator.sla_bounds[target_sla_id]
    orchestrator.quality_models[target_sla_id] = _always_violating_model(bounds)
    return target_sla_id


# ---------------------------------------------------------------------------
# Cloud bargain — publish a very-cheap SLA
# ---------------------------------------------------------------------------


def cloud_bargain(
    orchestrator: Orchestrator,
    *,
    path: list[PathHop] | None = None,
    price_per_gb: str = "0.008",
) -> str:
    """Publish one very-cheap SLA on a popular route.

    If ``path`` is ``None``, the path of the first pre-seeded SLA is reused
    (the "most popular" approximation). Returns the new sla_id.
    """

    if path is None:
        if not orchestrator.sla_templates:
            raise RuntimeError("cloud_bargain needs either a path or a template")
        path = list(orchestrator.sla_templates[0].path)
    template = SLATemplate(
        path=list(path),
        bounds=SLABounds(latency_max_ms=20, loss_max_ppm=800, bandwidth_min_kbps=8_000_000),
        price_per_gb=price_per_gb,
        consortium_profile="transit-good",  # cheap *and* honest — tempting
        validity_hours=24,
    )
    now = orchestrator.now_fn()
    sla = orchestrator._publish_template(template, now)  # type: ignore[attr-defined]
    return sla.payload.sla_id


# ---------------------------------------------------------------------------
# Bad actor cascade — one AS silently goes bad on every SLA it cosigns
# ---------------------------------------------------------------------------


def degrade_as(orchestrator: Orchestrator, *, isd_as: str) -> list[str]:
    """Flip every live SLA cosigned by ``isd_as`` into always-violating mode.

    Presenter story: "network trouble on AS X". Buyers of SLAs that X
    cosigned start seeing violations; k-corroboration fires within a few
    ticks; attribution by convergence splits the reputation hit across
    every cosigner of the affected SLAs, so you can see that X is always
    on the chopping block while innocent cosigners take smaller dings.

    Returns the list of sla_ids that were flipped.
    """

    if isd_as not in orchestrator.agents:
        raise KeyError(f"unknown AS: {isd_as}")

    flipped: list[str] = []
    for sla_id in list(orchestrator.live_sla_ids):
        sla = orchestrator.aggregator_client.get_sla(sla_id)
        if sla is None:
            continue
        if isd_as not in sla.payload.cosigners:
            continue
        bounds = orchestrator.sla_bounds[sla_id]
        orchestrator.quality_models[sla_id] = _always_violating_model(bounds)
        flipped.append(sla_id)
    return flipped


# bad_actor_cascade retained as an alias for back-compat with existing tests /
# older UI builds. Prefer degrade_as.
bad_actor_cascade = degrade_as


def fix_as(orchestrator: Orchestrator, *, isd_as: str) -> list[str]:
    """Restore nominal quality on every live SLA cosigned by ``isd_as``.

    Presenter story: "network restored on AS X". With violations stopping,
    the exponential reputation decay (§6.2) stops accumulating and existing
    weights fade with the configured τ, so the leaderboard climbs back over
    the next minute or so — the "redemption" half of the demo.

    Returns the list of sla_ids that were restored.
    """

    if isd_as not in orchestrator.agents:
        raise KeyError(f"unknown AS: {isd_as}")

    restored: list[str] = []
    for sla_id in list(orchestrator.live_sla_ids):
        sla = orchestrator.aggregator_client.get_sla(sla_id)
        if sla is None:
            continue
        if isd_as not in sla.payload.cosigners:
            continue
        # Find the template this SLA was minted from so we can rebuild its
        # original quality model. Match on (path, bounds) — templates are
        # small enough that linear scan is fine.
        template = _template_for_sla(orchestrator, sla)
        if template is None:
            continue
        orchestrator.quality_models[sla_id] = model_for_profile(
            template.consortium_profile, template.bounds
        )
        restored.append(sla_id)
    return restored


def _template_for_sla(orchestrator: Orchestrator, sla) -> SLATemplate | None:  # type: ignore[no-untyped-def]
    """Best-effort match of a live SLA back to its originating template.

    We compare the SLA's path segment (list of (isd_as, ingress, egress))
    and bounds to the templates configured on the orchestrator. Collisions
    are possible when two templates share a path + bounds; in that case the
    first match wins, which is fine — same consortium_profile => same
    quality model anyway.
    """

    sla_path = [(h.isd_as, h.ingress, h.egress) for h in sla.payload.path]
    sla_bounds = sla.payload.bounds
    for t in orchestrator.sla_templates:
        t_path = [(h.isd_as, h.ingress, h.egress) for h in t.path]
        if t_path == sla_path and t.bounds == sla_bounds:
            return t
    return None


__all__ = [
    "bad_actor_cascade",  # deprecated alias
    "cloud_bargain",
    "degrade_as",
    "fix_as",
    "hospital_reshops",
    "reset_market",
]
