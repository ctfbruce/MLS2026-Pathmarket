"""Coverage-scheme enumeration for ``GET /local/candidate_routes`` (§8.4).

Given the user's portfolio of ``SignedClaim`` and a candidate path from
``PathDiscovery``, enumerate non-overlapping assignments of claims to
contiguous hop ranges and compute an aggregate guarantee per scheme
(§A.5).

Semantics:

- A claim *can apply* to ``path[start:end]`` iff the SLA's cosigner
  sequence equals ``[hop.isd_as for hop in path[start:end]]`` — we match
  on ISD-AS identity only, not on ingress/egress, because the path
  discovered by the user AS may have different link indices than the
  originally-published SLA.
- A *coverage option* is a set of non-overlapping placements. The empty
  set is not returned: the UI handles the "no coverage" case via the
  default ``applied_claims = {}`` path in §9.3.
- Aggregates per §9.3: summed latency/loss, min-bandwidth, summed price,
  min-cosigner-score reputation. Partial coverage still emits an aggregate
  — the UI renders reputation as ``—`` in that case (§9.3).

Enumeration is bounded by ``MAX_SCHEMES`` to keep the endpoint snappy
under large portfolios; in practice the user AS holds O(10) claims.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from pathmarket.schemas import PathHop, Score, SignedClaim, SignedSLA


MAX_SCHEMES = 32


@dataclass(frozen=True)
class _Placement:
    claim_id: str
    sla_id: str
    hop_start: int  # inclusive
    hop_end: int  # exclusive


def _claim_placements(
    path: Sequence[PathHop],
    claims: Sequence[SignedClaim],
    slas_by_id: Mapping[str, SignedSLA],
) -> list[_Placement]:
    """Every (claim, hop_start, hop_end) where the claim's SLA can apply."""

    path_ases = [h.isd_as for h in path]
    placements: list[_Placement] = []
    for claim in claims:
        sla = slas_by_id.get(claim.payload.sla_id)
        if sla is None:
            continue
        seq = sla.payload.cosigners
        if not seq:
            continue
        n = len(seq)
        # Sliding-window match over path_ases.
        for start in range(0, len(path_ases) - n + 1):
            if path_ases[start : start + n] == list(seq):
                placements.append(
                    _Placement(
                        claim_id=claim.payload.claim_id,
                        sla_id=sla.payload.sla_id,
                        hop_start=start,
                        hop_end=start + n,
                    )
                )
    # Stable order makes tests deterministic.
    placements.sort(key=lambda p: (p.hop_start, p.hop_end, p.claim_id))
    return placements


def _enumerate_schemes(
    placements: list[_Placement], path_len: int
) -> list[list[_Placement]]:
    """Non-overlapping subsets of placements, bounded by ``MAX_SCHEMES``.

    Greedy-ish: we explore in placement order and at each step either skip
    a placement or include it (if it doesn't overlap with already-chosen
    placements). The empty scheme is excluded; callers who want to model
    "no coverage" do so via ``applied_claims = {}``.
    """

    out: list[list[_Placement]] = []

    def _rec(i: int, chosen: list[_Placement], covered: list[bool]) -> None:
        if len(out) >= MAX_SCHEMES:
            return
        if i == len(placements):
            if chosen:
                out.append(list(chosen))
            return
        p = placements[i]
        # Skip this placement.
        _rec(i + 1, chosen, covered)
        if len(out) >= MAX_SCHEMES:
            return
        # Include it iff its hop range is currently uncovered.
        if not any(covered[p.hop_start : p.hop_end]):
            for h in range(p.hop_start, p.hop_end):
                covered[h] = True
            chosen.append(p)
            _rec(i + 1, chosen, covered)
            chosen.pop()
            for h in range(p.hop_start, p.hop_end):
                covered[h] = False

    _rec(0, [], [False] * path_len)
    return out


def _score_of(isd_as: str, scores: Mapping[str, Score]) -> float:
    s = scores.get(isd_as)
    return 1.0 if s is None else float(s.score)


def _aggregate_for_scheme(
    scheme: Sequence[_Placement],
    slas_by_id: Mapping[str, SignedSLA],
    path: Sequence[PathHop],
    scores: Mapping[str, Score],
) -> dict[str, Any]:
    """Per-scheme aggregate guarantee, §9.3 semantics."""

    path_len = len(path)
    covered_hops = sum(p.hop_end - p.hop_start for p in scheme)

    total_latency: int | None = 0
    total_loss: int | None = 0
    min_bandwidth: int | None = None
    price = Decimal("0")
    cosigner_scores: list[float] = []

    for p in scheme:
        sla = slas_by_id[p.sla_id]
        bounds = sla.payload.bounds
        if bounds.latency_max_ms is None:
            total_latency = None if total_latency is None else total_latency
        elif total_latency is not None:
            total_latency += int(bounds.latency_max_ms)
        if bounds.loss_max_ppm is None:
            total_loss = None if total_loss is None else total_loss
        elif total_loss is not None:
            total_loss += int(bounds.loss_max_ppm)
        if bounds.bandwidth_min_kbps is not None:
            bw = int(bounds.bandwidth_min_kbps)
            min_bandwidth = bw if min_bandwidth is None else min(min_bandwidth, bw)
        price += Decimal(sla.payload.price_per_gb)
        for cosigner in sla.payload.cosigners:
            cosigner_scores.append(_score_of(cosigner, scores))

    reputation: float | None
    if covered_hops == path_len and cosigner_scores:
        reputation = min(cosigner_scores)
    else:
        reputation = None

    return {
        "latency_ms": total_latency,
        "loss_ppm": total_loss,
        "bandwidth_kbps": min_bandwidth,
        "price_per_gb": format(price, "f"),
        "reputation": reputation,
        "covered_hops": covered_hops,
        "total_hops": path_len,
    }


def _scheme_to_wire(scheme: Sequence[_Placement]) -> list[dict[str, Any]]:
    return [
        {"hop_start": p.hop_start, "hop_end": p.hop_end, "claim_id": p.claim_id}
        for p in scheme
    ]


def _path_to_wire(path: Sequence[PathHop]) -> list[dict[str, Any]]:
    return [{"isd_as": h.isd_as, "ingress": h.ingress, "egress": h.egress} for h in path]


def build_candidate_routes(
    *,
    src_isd_as: str,
    dst_isd_as: str,
    portfolio: Sequence[SignedClaim],
    path_discovery,
    aggregator_client,
) -> dict[str, Any]:
    """Assemble the full response for ``GET /local/candidate_routes``.

    Resolves each claim's SLA against ``aggregator_client``; SLAs the
    aggregator no longer remembers are silently dropped (the claim is
    still valid, just not usable for composition).
    """

    paths = path_discovery.paths_from(src_isd_as, dst_isd_as)

    slas_by_id: dict[str, SignedSLA] = {}
    for claim in portfolio:
        sla = aggregator_client.get_sla(claim.payload.sla_id)
        if sla is not None:
            slas_by_id[sla.payload.sla_id] = sla

    scores_list = aggregator_client.get_scores()
    scores: dict[str, Score] = {s.isd_as: s for s in scores_list}

    candidates: list[dict[str, Any]] = []
    for path in paths:
        placements = _claim_placements(path, portfolio, slas_by_id)
        schemes = _enumerate_schemes(placements, len(path))
        coverage_options = [
            {
                "scheme": _scheme_to_wire(s),
                "aggregate": _aggregate_for_scheme(s, slas_by_id, path, scores),
            }
            for s in schemes
        ]
        candidates.append({"path": _path_to_wire(path), "coverage_options": coverage_options})

    return {"dst_isd_as": dst_isd_as, "candidates": candidates}


__all__ = ["build_candidate_routes"]
