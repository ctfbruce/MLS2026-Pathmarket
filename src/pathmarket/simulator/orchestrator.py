"""Simulator orchestrator (``DESIGN.md`` §7.2, §7.6).

The orchestrator owns the simulator process's entire pool of :class:`Agent`
instances and drives them tick-by-tick. Agents do not know their "true
quality profile" (that's per-SLA state held inside the simulator only) and
do not negotiate cosigning across parties — the orchestrator composes
multi-cosigner SLAs atomically using the private keys it holds for every
simulated AS.

The orchestrator is deterministic under ``--seed`` (§7.6). All randomness
flows through a single :class:`random.Random` seeded at construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pathmarket.agent.agent import Agent
from pathmarket.agent.policy import (
    min_cosigner_score,
    policy_accepts_sla,
)
from pathmarket.agent.simulated_quality import (
    QualityModel,
    QualityProfile,
    model_for_profile,
    sample_violates,
)
from pathmarket.client import AggregatorClient
from pathmarket.path_discovery.protocol import PathDiscovery
from pathmarket.schemas import (
    PathHop,
    Policy,
    SLABounds,
    SignedClaim,
    SignedComplaint,
    SignedSLA,
)


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """One simulated AS's static configuration.

    ``role`` is the §7.4 category: ``transit-good``, ``transit-bad``,
    ``transit-premium``, ``edge-buyer``, ``edge-mixed``, or ``inter-isd-core``.
    ``quality_profile`` is the :class:`QualityProfile` used when this AS
    participates as a cosigner on an SLA; ``None`` for pure buyers.
    """

    isd_as: str
    role: str
    policy: Policy
    quality_profile: QualityProfile | None = None


@dataclass(frozen=True)
class SLATemplate:
    """A recipe for one SLA the orchestrator may publish.

    ``bounds`` and ``price_per_gb`` are fixed at template time; the
    orchestrator picks these up at pre-seed and at mid-run publish actions.
    ``consortium_profile`` determines the quality profile (and thus the
    per-metric mean/stddev) the simulator applies to the resulting SLA.
    """

    path: list[PathHop]
    bounds: SLABounds
    price_per_gb: str
    consortium_profile: QualityProfile
    validity_hours: int = 24


@dataclass
class TickSummary:
    """Counts of artifacts produced by one :meth:`Orchestrator.tick`."""

    tick_time: datetime
    new_slas: list[SignedSLA] = field(default_factory=list)
    new_claims: list[SignedClaim] = field(default_factory=list)
    new_complaints: list[SignedComplaint] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """Drives a pool of :class:`Agent` instances deterministically."""

    def __init__(
        self,
        *,
        specs: list[AgentSpec],
        private_keys: dict[str, Ed25519PrivateKey],
        sla_templates: list[SLATemplate],
        aggregator_client: AggregatorClient,
        path_discovery: PathDiscovery,
        rng,  # random.Random
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.specs = {s.isd_as: s for s in specs}
        self.private_keys = dict(private_keys)
        self.sla_templates = list(sla_templates)
        self.aggregator_client = aggregator_client
        self.path_discovery = path_discovery
        self.rng = rng
        self.now_fn = now_fn or (lambda: datetime.now(tz=timezone.utc))

        for isd_as in self.specs:
            if isd_as not in self.private_keys:
                raise ValueError(f"missing private key for {isd_as}")

        self.agents: dict[str, Agent] = {
            s.isd_as: Agent(
                isd_as=s.isd_as,
                private_key=self.private_keys[s.isd_as],
                policy=s.policy,
                aggregator_client=aggregator_client,
                path_discovery=path_discovery,
            )
            for s in specs
        }

        # SLA-level simulator state (never leaves this process)
        self.quality_models: dict[str, QualityModel] = {}
        self.sla_bounds: dict[str, SLABounds] = {}
        self.live_sla_ids: list[str] = []  # published-by-this-orchestrator, in publish order

    # ------------------------------------------------------------------
    # Pre-seed
    # ------------------------------------------------------------------

    def pre_seed(self, *, count: int, now: datetime | None = None) -> list[SignedSLA]:
        """Publish ``count`` SLAs from :attr:`sla_templates` to make the market non-empty.

        Templates are sampled without replacement until count is met (wrapping
        around if ``count`` > len(templates)). Each published SLA gets a
        fresh :class:`QualityModel` stored under its ``sla_id``.
        """

        now = now or self.now_fn()
        out: list[SignedSLA] = []
        for i in range(count):
            template = self.sla_templates[i % len(self.sla_templates)]
            sla = self._publish_template(template, now)
            out.append(sla)
        return out

    def _publish_template(self, template: SLATemplate, now: datetime) -> SignedSLA:
        cosigners = [h.isd_as for h in template.path]
        # Publisher is the first cosigner we actually have a signing agent for.
        # This lets templates include ASes (like the user's own AS) that live
        # outside the simulator process as long as *some* cosigner is a sim agent.
        publisher = next((a for a in cosigners if a in self.agents), None)
        if publisher is None:
            raise RuntimeError(
                f"no agent available to publish template with cosigners {cosigners}"
            )
        agent = self.agents[publisher]
        cosigner_keys = {a: self.private_keys[a] for a in cosigners if a != publisher}
        valid_from = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        valid_until = (now + timedelta(hours=template.validity_hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        nonce = f"{self.rng.randrange(2**64):016x}"
        sla = agent.publish_sla(
            path=list(template.path),
            bounds=template.bounds,
            price_per_gb=template.price_per_gb,
            valid_from=valid_from,
            valid_until=valid_until,
            nonce=nonce,
            cosigner_keys=cosigner_keys,
        )
        self.quality_models[sla.payload.sla_id] = model_for_profile(
            template.consortium_profile, template.bounds
        )
        self.sla_bounds[sla.payload.sla_id] = template.bounds
        self.live_sla_ids.append(sla.payload.sla_id)
        return sla

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def tick(self, *, publish_prob: float = 0.25, claim_max_per_tick: int = 3) -> TickSummary:
        """Run one tick of the simulator. Returns a :class:`TickSummary`.

        1. With probability ``publish_prob``, publish one new SLA using a
           randomly chosen template.
        2. Offer the current market (from the aggregator client) to a random
           subset of agents; each accepts if their policy accepts any SLA
           they can see. Up to ``claim_max_per_tick`` claims fire per tick.
        3. For each claim currently in each agent's portfolio, sample that
           SLA's simulated quality. If the sample violates the SLA's bound
           AND the agent's ``complaint_sensitivity`` threshold, file a
           complaint.
        """

        now = self.now_fn()
        summary = TickSummary(tick_time=now)

        # (1) Publish
        if self.sla_templates and self.rng.random() < publish_prob:
            template = self.rng.choice(self.sla_templates)
            sla = self._publish_template(template, now)
            summary.new_slas.append(sla)

        # (2) Claim
        claim_candidates = self._candidate_buyers()
        self.rng.shuffle(claim_candidates)
        scores_by_as = {s.isd_as: s.score for s in self.aggregator_client.get_scores()}
        market = self.aggregator_client.list_active_slas()
        claims_made = 0
        for buyer_as in claim_candidates:
            if claims_made >= claim_max_per_tick:
                break
            agent = self.agents[buyer_as]
            acceptable = [
                s for s in market
                if buyer_as not in s.payload.cosigners
                and policy_accepts_sla(
                    agent.policy, s, min_cosigner_score(s, scores_by_as)
                )
                and s.payload.sla_id not in {c.payload.sla_id for c in agent.portfolio}
            ]
            if not acceptable:
                continue
            chosen = self.rng.choice(acceptable)
            gb = self.rng.choice([100, 250, 500])
            claim = agent.claim_sla(
                chosen, gb=gb, claimed_at=now.strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            summary.new_claims.append(claim)
            claims_made += 1

        # (3) Quality sampling → complaints
        for buyer_as, agent in self.agents.items():
            for claim in list(agent.portfolio):
                sla_id = claim.payload.sla_id
                if sla_id not in self.quality_models:
                    continue
                model = self.quality_models[sla_id]
                bounds = self.sla_bounds[sla_id]
                sample = model.sample(self.rng)
                sla = self.aggregator_client.get_sla(sla_id)
                if sla is None:
                    continue
                for metric, measured in (
                    ("latency_ms", sample.latency_ms),
                    ("loss_ppm", sample.loss_ppm),
                    ("bandwidth_kbps", sample.bandwidth_kbps),
                ):
                    if measured is None:
                        continue
                    if not sample_violates(bounds, sample, metric):
                        continue
                    # Agent's own threshold still governs whether to complain.
                    from pathmarket.agent.policy import should_file_complaint
                    bound_val = getattr(
                        bounds,
                        {
                            "latency_ms": "latency_max_ms",
                            "loss_ppm": "loss_max_ppm",
                            "bandwidth_kbps": "bandwidth_min_kbps",
                        }[metric],
                    )
                    if not should_file_complaint(agent.policy, metric, bound_val, measured):
                        continue
                    complaint = agent.file_complaint(
                        sla,
                        metric=metric,
                        measured_value=measured,
                        observed_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    )
                    summary.new_complaints.append(complaint)
                    # One complaint per (agent, claim, tick) is enough —
                    # avoid spamming the aggregator from a single sample.
                    break

        return summary

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _candidate_buyers(self) -> list[str]:
        """Agents whose role makes them plausible buyers."""

        return [
            spec.isd_as
            for spec in self.specs.values()
            if spec.role in ("edge-buyer", "edge-mixed")
        ]


__all__ = ["AgentSpec", "Orchestrator", "SLATemplate", "TickSummary"]
