"""Frozen dataclasses for every signed-payload and in-memory artefact in PathMarket v2.

Authoritative source: ``DESIGN.md`` §4. The ``schema_version`` on every signed
payload is ``2`` in v2. Any change to these dataclasses is a schema change and
must be accompanied by a DESIGN.md amendment.

All dataclasses are ``@dataclass(frozen=True)`` — they are hashed via the
canonical-JSON procedure in ``canonical.py``, and frozen-ness matches the
"set-id-field-to-empty-string-then-hash" pattern in §4.6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Path primitives (§4.1, §4.2) — unchanged from v1.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PathHop:
    """One AS on a path. ``ingress == 0`` at the source; ``egress == 0`` at the destination."""

    isd_as: str
    ingress: int
    egress: int


@dataclass(frozen=True)
class SLABounds:
    """Quality bounds an SLA commits to.

    At least one field must be non-``None`` in any valid SLA (§5.2 validation step 5).
    """

    latency_max_ms: Optional[int]
    loss_max_ppm: Optional[int]
    bandwidth_min_kbps: Optional[int]


# ---------------------------------------------------------------------------
# Signature metadata (§4.3) — unchanged from v1.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SignatureMeta:
    """Metadata about which key was used to sign."""

    isd_as: str
    key_id: str
    trc_serial: int
    trc_base: int


@dataclass(frozen=True)
class Signature:
    """Ed25519 signature bytes plus the metadata needed to look up the verifying key."""

    meta: SignatureMeta
    sig_bytes: bytes


# ---------------------------------------------------------------------------
# SLA (§4.3) — unchanged from v1 except schema_version bumped to 2.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SLAPayload:
    """Signed offer by a consortium of ASes committing to bounds on a path.

    Semantics (§4.3):
    - ``cosigners`` must equal ``[hop.isd_as for hop in path]`` in order.
    - ``valid_until > valid_from``.
    - At least one ``bounds`` field is non-``None``.
    - ``sla_id`` is the content hash per §4.6 (set to ``""`` before hashing).
    """

    sla_id: str
    path: list[PathHop]
    cosigners: list[str]
    bounds: SLABounds
    price_per_gb: str
    valid_from: str
    valid_until: str
    nonce: str
    schema_version: int


@dataclass(frozen=True)
class SignedSLA:
    """An SLA payload plus one signature per cosigner (order not significant)."""

    payload: SLAPayload
    signatures: list[Signature]


# ---------------------------------------------------------------------------
# Claim (§4.4) — NEW in v2. First-class economic act.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClaimPayload:
    """Signed statement that ``claimant`` purchased ``gb_purchased`` on ``sla_id``.

    Semantics (§4.4, §5.2):
    - ``claimant`` must not be in the referenced SLA's cosigners (no self-dealing).
    - ``price_paid_chf`` must equal ``gb_purchased * sla.price_per_gb`` by Decimal
      value equality (amendment, 2026-04-19).
    - Immutable once signed; no "release" mechanism in v2.
    """

    claim_id: str
    sla_id: str
    claimant: str
    gb_purchased: int
    claimed_at: str
    price_paid_chf: str
    schema_version: int


@dataclass(frozen=True)
class SignedClaim:
    """A claim payload plus exactly one signature by the claimant AS."""

    payload: ClaimPayload
    signature: Signature


# ---------------------------------------------------------------------------
# Complaint (§4.7, §4.8, §4.9) — carried from v1 with ``note``/``attachments`` added.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Attachment:
    """Tamper-evident attachment to a complaint (§4.9).

    Aggregator-enforced limits:
    - ``kind`` in ``{"text", "json", "log"}``.
    - ``filename`` non-empty, ≤ 128 chars, no path separators.
    - decoded ``content_b64`` ≤ 64 KiB (65536 bytes) (amendment, 2026-04-19).
    - at most 3 attachments per complaint.

    The aggregator does NOT parse or validate attachment contents; they are
    rendered in the UI with the mandatory disclaimer "display only · not
    cryptographically verified by viewer".
    """

    kind: str
    filename: str
    content_b64: str


@dataclass(frozen=True)
class ComplaintPayload:
    """Signed claim of a bound violation on an SLA.

    Semantics (§4.7, §4.8):
    - ``complainant`` must not be in the SLA's cosigners.
    - ``path_used`` must equal the SLA's ``path``.
    - ``metric`` in ``{"latency_ms", "loss_ppm", "bandwidth_kbps"}``; the SLA's
      corresponding bound must be non-``None``.
    - Violation polarity per §4.8: latency/loss violate when ``measured > bound``,
      bandwidth violates when ``measured < bound``.
    - ``note`` and ``attachments`` are NEW in v2 and are part of the signed
      payload (tamper-evident after the complainant signs).
    """

    complaint_id: str
    sla_id: str
    complainant: str
    path_used: list[PathHop]
    metric: str
    measured_value: int
    observed_at: str
    note: str
    attachments: list[Attachment]
    schema_version: int


@dataclass(frozen=True)
class SignedComplaint:
    """A complaint payload plus exactly one signature by the complainant AS."""

    payload: ComplaintPayload
    signature: Signature


# ---------------------------------------------------------------------------
# Score and ViolationEvent (§4.10) — unchanged from v1.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Score:
    """A reputation score for one AS in ``[0.0, 1.0]``.

    ASes with zero violations return ``score == 1.0`` and empty ``components``.
    ``components`` carries debug fields (recent_violations, decay_factor, etc.).
    """

    isd_as: str
    score: float
    components: dict


@dataclass(frozen=True)
class ViolationEvent:
    """Scorer-attributed event: k corroborated complaints on the same (sla, metric)
    within the configured window.

    ``attributed_to`` are the SLA's cosigners — the ASes whose scores drop.
    """

    sla_id: str
    metric: str
    window_end: datetime
    complainants: list[str]
    attributed_to: list[str]


# ---------------------------------------------------------------------------
# Policy (§4.11) — NEW in v2. Local configuration; never shared with aggregator.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Policy:
    """Declarative configuration of an Agent's buying, complaining, and routing.

    Fields map 1:1 to §4.11; ``uncovered_tolerance`` and ``complaint_sensitivity``
    are enum-like strings validated at policy-load time and at ``PUT /local/policy``.
    """

    max_price_per_gb: str
    min_reputation_floor: float
    required_bounds: SLABounds
    alpha: float
    beta: float
    uncovered_tolerance: str
    complaint_sensitivity: str
    reshop_on_reputation_drop: float
    portfolio_redundancy: int


# ---------------------------------------------------------------------------
# RoutingDecision (§4.12) — NEW in v2. Persistent server-side state for the user AS.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoutingDecision:
    """Persistent choice of which path (and which claims per hop) the user AS uses
    for a given destination.

    ``applied_claims`` maps ``hop_index`` (0-based into ``path``) to ``claim_id``;
    uncovered hops are simply omitted from the mapping.
    """

    dst_isd_as: str
    path: list[PathHop]
    applied_claims: dict
    updated_at: str
    manual_override: bool


__all__ = [
    "Attachment",
    "ClaimPayload",
    "ComplaintPayload",
    "PathHop",
    "Policy",
    "RoutingDecision",
    "SLABounds",
    "SLAPayload",
    "Score",
    "Signature",
    "SignatureMeta",
    "SignedClaim",
    "SignedComplaint",
    "SignedSLA",
    "ViolationEvent",
]
