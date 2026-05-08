"""The shared ``Agent`` class (``DESIGN.md`` §7.1).

Every AS in v2 — the user AS and all 31 simulated ASes — runs one of these.
The class encapsulates: identity (ISD-AS + Ed25519 key), policy, aggregator
client, path discovery, and the agent-local portfolio / routing decisions.

This module deliberately keeps the class *stateful but dumb*: it exposes the
four §7.1 action methods (``publish_sla``, ``claim_sla``, ``file_complaint``,
``choose_route``) and leaves the tick loop / tick scheduling to the simulator
(§7.2, Step C3).
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pathmarket.agent.policy import choose_best_sla, compose_bounds
from pathmarket.canonical import (
    canonical_json,
    compute_claim_id,
    compute_complaint_id,
    compute_sla_id,
)
from pathmarket.client import AggregatorClient
from pathmarket.path_discovery.protocol import PathDiscovery
from pathmarket.schemas import (
    Attachment,
    ClaimPayload,
    ComplaintPayload,
    PathHop,
    Policy,
    RoutingDecision,
    SLABounds,
    SLAPayload,
    Signature,
    SignatureMeta,
    SignedClaim,
    SignedComplaint,
    SignedSLA,
)


def _sign(
    private_key: Ed25519PrivateKey, isd_as: str, payload_bytes: bytes
) -> Signature:
    return Signature(
        meta=SignatureMeta(isd_as=isd_as, key_id="default", trc_serial=0, trc_base=0),
        sig_bytes=private_key.sign(payload_bytes),
    )


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Agent:
    """One AS's policy-driven actor. See §7.1.

    The agent holds ``portfolio`` (claims it signed) and ``routing_decisions``
    (current dst → RoutingDecision) in memory. For the user agent, those are
    persisted via the state-store in Phase D; for simulated agents, they stay
    in-process.
    """

    def __init__(
        self,
        *,
        isd_as: str,
        private_key: Ed25519PrivateKey,
        policy: Policy,
        aggregator_client: AggregatorClient,
        path_discovery: PathDiscovery,
    ) -> None:
        self.isd_as = isd_as
        self.private_key = private_key
        self.policy = policy
        self.aggregator_client = aggregator_client
        self.path_discovery = path_discovery
        self.portfolio: list[SignedClaim] = []
        self.routing_decisions: dict[str, RoutingDecision] = {}

    # ------------------------------------------------------------------
    # publish_sla
    # ------------------------------------------------------------------

    def publish_sla(
        self,
        *,
        path: list[PathHop],
        bounds: SLABounds,
        price_per_gb: str,
        valid_from: str,
        valid_until: str,
        nonce: str,
        cosigner_keys: Mapping[str, Ed25519PrivateKey],
        post: bool = True,
    ) -> SignedSLA:
        """Build a fully-signed SLA for ``path`` and optionally POST it.

        ``cosigner_keys`` must contain one Ed25519 private key for every
        cosigner AS (i.e. every ``hop.isd_as`` in ``path``); this agent's own
        key is used for its own cosigner entry and need not appear in the
        dict. In the simulator, the orchestrator passes in the keys of the
        other simulated cosigners so an SLA can be composed atomically.
        """

        cosigners = [h.isd_as for h in path]
        payload = SLAPayload(
            sla_id="",
            path=list(path),
            cosigners=cosigners,
            bounds=bounds,
            price_per_gb=price_per_gb,
            valid_from=valid_from,
            valid_until=valid_until,
            nonce=nonce,
            schema_version=2,
        )
        payload = replace(payload, sla_id=compute_sla_id(payload))
        payload_bytes = canonical_json(asdict(payload))

        signatures: list[Signature] = []
        for c in cosigners:
            if c == self.isd_as:
                signatures.append(_sign(self.private_key, self.isd_as, payload_bytes))
            else:
                try:
                    sk = cosigner_keys[c]
                except KeyError as e:
                    raise ValueError(f"missing cosigner key for {c}") from e
                signatures.append(_sign(sk, c, payload_bytes))

        sla = SignedSLA(payload=payload, signatures=signatures)
        if post:
            self.aggregator_client.post_sla(sla)
        return sla

    # ------------------------------------------------------------------
    # claim_sla
    # ------------------------------------------------------------------

    def claim_sla(
        self,
        sla: SignedSLA,
        *,
        gb: int,
        claimed_at: str | None = None,
        post: bool = True,
    ) -> SignedClaim:
        """Sign a claim on ``sla`` for ``gb`` gigabytes and optionally POST it."""

        # Self-dealing: claimant may not appear among cosigners, EXCEPT when
        # they are the first cosigner (the path's source AS). Source ASes are
        # the natural buyers of SLAs covering their outbound egress, so we
        # allow them to claim SLAs they also cosign at position 0.
        if (
            self.isd_as in sla.payload.cosigners
            and sla.payload.cosigners[0] != self.isd_as
        ):
            raise ValueError(
                f"self-dealing: {self.isd_as} cannot claim its own SLA {sla.payload.sla_id}"
            )

        price_paid = str(Decimal(sla.payload.price_per_gb) * Decimal(gb))
        payload = ClaimPayload(
            claim_id="",
            sla_id=sla.payload.sla_id,
            claimant=self.isd_as,
            gb_purchased=gb,
            claimed_at=claimed_at or _utc_now_iso(),
            price_paid_chf=price_paid,
            schema_version=2,
        )
        payload = replace(payload, claim_id=compute_claim_id(payload))
        payload_bytes = canonical_json(asdict(payload))
        signature = _sign(self.private_key, self.isd_as, payload_bytes)
        claim = SignedClaim(payload=payload, signature=signature)

        if post:
            self.aggregator_client.post_claim(claim)
        self.portfolio.append(claim)
        return claim

    # ------------------------------------------------------------------
    # file_complaint
    # ------------------------------------------------------------------

    def file_complaint(
        self,
        sla: SignedSLA,
        *,
        metric: str,
        measured_value: int,
        observed_at: str,
        note: str = "",
        attachments: list[Attachment] | None = None,
        post: bool = True,
    ) -> SignedComplaint:
        """Sign and (optionally) POST a complaint about ``sla``."""

        if self.isd_as in sla.payload.cosigners:
            raise ValueError(
                f"complainant {self.isd_as} cannot be a cosigner of {sla.payload.sla_id}"
            )

        payload = ComplaintPayload(
            complaint_id="",
            sla_id=sla.payload.sla_id,
            complainant=self.isd_as,
            path_used=list(sla.payload.path),
            metric=metric,
            measured_value=measured_value,
            observed_at=observed_at,
            note=note,
            attachments=list(attachments) if attachments else [],
            schema_version=2,
        )
        payload = replace(payload, complaint_id=compute_complaint_id(payload))
        payload_bytes = canonical_json(asdict(payload))
        signature = _sign(self.private_key, self.isd_as, payload_bytes)
        complaint = SignedComplaint(payload=payload, signature=signature)

        if post:
            self.aggregator_client.post_complaint(complaint)
        return complaint

    # ------------------------------------------------------------------
    # choose_route
    # ------------------------------------------------------------------

    def choose_route(
        self,
        dst_isd_as: str,
        *,
        candidate_slas: list[SignedSLA] | None = None,
        scores_by_as: dict[str, float] | None = None,
        now: datetime | None = None,
    ) -> RoutingDecision | None:
        """Pick a path to ``dst_isd_as`` under current market state.

        Algorithm:
        1. Enumerate candidate paths via :attr:`path_discovery`.
        2. If no SLA pool was provided, fetch the active market.
        3. For each candidate path, find the single-SLA that covers the whole
           path (``sla.payload.path == candidate_path``) AND is accepted by
           policy. The policy-best among those SLAs wins.
        4. Persist/update the :class:`RoutingDecision` and return it.

        Returns ``None`` if no candidate path has any policy-acceptable SLA.

        This is the "simple" variant — multi-SLA composition per hop is the
        domain of :mod:`pathmarket.user_agent.app` in Phase D, which builds
        on the policy helpers here.
        """

        paths = self.path_discovery.paths_from(self.isd_as, dst_isd_as)
        if not paths:
            return None

        if candidate_slas is None:
            candidate_slas = self.aggregator_client.list_active_slas()
        if scores_by_as is None:
            scores_by_as = {s.isd_as: s.score for s in self.aggregator_client.get_scores()}

        best_pair: tuple[list[PathHop], SignedSLA] | None = None
        best_utility: float | None = None
        for path in paths:
            slas_on_path = [s for s in candidate_slas if list(s.payload.path) == list(path)]
            choice = choose_best_sla(self.policy, slas_on_path, scores_by_as)
            if choice is None:
                continue
            # Re-compute utility here for head-to-head comparison across paths.
            from pathmarket.agent.policy import mean_cosigner_score, sla_utility
            u = sla_utility(self.policy, choice, mean_cosigner_score(choice, scores_by_as))
            if best_utility is None or u > best_utility:
                best_pair = (path, choice)
                best_utility = u

        if best_pair is None:
            return None

        path, sla = best_pair
        # Map every hop to the winning SLA's claim_id placeholder — the
        # caller (or Phase D logic) will refine this once a claim is signed.
        applied_claims: dict[int, str] = {i: sla.payload.sla_id for i in range(len(path))}
        decision = RoutingDecision(
            dst_isd_as=dst_isd_as,
            path=list(path),
            applied_claims=applied_claims,
            updated_at=(now or datetime.now(tz=timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            manual_override=False,
        )
        self.routing_decisions[dst_isd_as] = decision
        return decision


__all__ = ["Agent"]
