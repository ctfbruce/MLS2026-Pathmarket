"""SLA submission validation chain (``DESIGN.md`` §5.2 ``POST /sla``).

The response body is a **verification report**: a list of per-step results that
the UI renders verbatim. Short-circuits on the first failing step.
"""

from __future__ import annotations

import base64
from binascii import Error as _B64Error
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from pathmarket.aggregator.models import (
    SignedClaimModel,
    SignedComplaintModel,
    SignedSLAModel,
)
from pathmarket.aggregator.storage import AggregatorStorage
from pathmarket.canonical import (
    canonical_json,
    compute_claim_id,
    compute_complaint_id,
    compute_sla_id,
)
from pathmarket.schemas import SignedClaim, SignedComplaint, SignedSLA
from pathmarket.verifier.protocol import Verifier


_MAX_ATTACHMENT_BYTES = 64 * 1024
_MAX_ATTACHMENTS = 3
_MAX_FILENAME_LEN = 128
_ALLOWED_ATTACHMENT_KINDS = frozenset({"text", "json", "log"})
_METRIC_TO_BOUND_FIELD: dict[str, str] = {
    "latency_ms": "latency_max_ms",
    "loss_ppm": "loss_max_ppm",
    "bandwidth_kbps": "bandwidth_min_kbps",
}


@dataclass(frozen=True)
class VerificationStep:
    """One line in the ``verification_steps`` array returned by ``POST /sla``."""

    step: str
    ok: bool
    detail: str | None


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of the validation chain: all steps run so far, plus the parsed SLA on success."""

    steps: list[VerificationStep]
    sla: SignedSLA | None

    @property
    def accepted(self) -> bool:
        return self.sla is not None and all(s.ok for s in self.steps)


_ALLOWED_METRICS = frozenset({"latency_ms", "loss_ppm", "bandwidth_kbps"})


def _parse_iso(ts: str) -> datetime:
    # Both ``...Z`` and ``...+00:00`` are supported; our producers emit ``Z``.
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def validate_sla_submission(
    body: object, *, storage: AggregatorStorage, verifier: Verifier
) -> ValidationResult:
    """Run the §5.2 validation chain.

    Returns a ``ValidationResult`` whose ``steps`` list is appended to until
    the first failing step (short-circuit). ``sla`` is populated iff every
    step passes.
    """

    steps: list[VerificationStep] = []

    # Step 1 — parse_json.
    try:
        model = SignedSLAModel.model_validate(body)
    except ValidationError as e:
        steps.append(VerificationStep("parse_json", False, _first_error(e)))
        return ValidationResult(steps, None)
    except (TypeError, ValueError) as e:
        steps.append(VerificationStep("parse_json", False, str(e)))
        return ValidationResult(steps, None)
    steps.append(VerificationStep("parse_json", True, None))

    sla = model.to_dataclass()
    payload = sla.payload

    # Step 2 — schema_version.
    if payload.schema_version != 2:
        steps.append(
            VerificationStep(
                "schema_version", False, f"expected 2, got {payload.schema_version}"
            )
        )
        return ValidationResult(steps, None)
    steps.append(VerificationStep("schema_version", True, "version 2"))

    # Step 3 — content_hash.
    expected_id = compute_sla_id(payload)
    if payload.sla_id != expected_id:
        steps.append(
            VerificationStep(
                "content_hash",
                False,
                f"sla_id mismatch: got {payload.sla_id}, expected {expected_id}",
            )
        )
        return ValidationResult(steps, None)
    steps.append(VerificationStep("content_hash", True, "sla_id matches"))

    # Step 4 — validity_window.
    try:
        v_from = _parse_iso(payload.valid_from)
        v_until = _parse_iso(payload.valid_until)
    except ValueError as e:
        steps.append(VerificationStep("validity_window", False, f"bad timestamp: {e}"))
        return ValidationResult(steps, None)
    if v_until <= v_from:
        steps.append(
            VerificationStep(
                "validity_window",
                False,
                f"valid_until ({payload.valid_until}) not after valid_from ({payload.valid_from})",
            )
        )
        return ValidationResult(steps, None)
    span_hours = (v_until - v_from).total_seconds() / 3600.0
    steps.append(
        VerificationStep("validity_window", True, f"valid for {span_hours:g}h")
    )

    # Step 5 — bounds_nonempty.
    bounds = payload.bounds
    if (
        bounds.latency_max_ms is None
        and bounds.loss_max_ppm is None
        and bounds.bandwidth_min_kbps is None
    ):
        steps.append(
            VerificationStep("bounds_nonempty", False, "all bounds are null")
        )
        return ValidationResult(steps, None)
    # Detail names the first non-None bound for operator readability.
    detail_bits = []
    if bounds.latency_max_ms is not None:
        detail_bits.append(f"latency_max_ms={bounds.latency_max_ms}")
    if bounds.loss_max_ppm is not None:
        detail_bits.append(f"loss_max_ppm={bounds.loss_max_ppm}")
    if bounds.bandwidth_min_kbps is not None:
        detail_bits.append(f"bandwidth_min_kbps={bounds.bandwidth_min_kbps}")
    steps.append(VerificationStep("bounds_nonempty", True, detail_bits[0]))

    # Step 6 — cosigners_match_path.
    path_ases = [h.isd_as for h in payload.path]
    if len(payload.cosigners) < 2:
        steps.append(
            VerificationStep(
                "cosigners_match_path",
                False,
                f"need at least 2 cosigners, got {len(payload.cosigners)}",
            )
        )
        return ValidationResult(steps, None)
    if payload.cosigners != path_ases:
        steps.append(
            VerificationStep(
                "cosigners_match_path",
                False,
                f"cosigners {payload.cosigners} != path ASes {path_ases}",
            )
        )
        return ValidationResult(steps, None)
    steps.append(
        VerificationStep(
            "cosigners_match_path", True, f"{len(payload.cosigners)} ASes"
        )
    )

    # Step 7 — signature_count.
    cosigner_set = set(payload.cosigners)
    sig_cosigners = [s.meta.isd_as for s in sla.signatures]
    if len(sla.signatures) != len(payload.cosigners):
        steps.append(
            VerificationStep(
                "signature_count",
                False,
                f"expected {len(payload.cosigners)} signatures, got {len(sla.signatures)}",
            )
        )
        return ValidationResult(steps, None)
    if set(sig_cosigners) != cosigner_set or len(sig_cosigners) != len(cosigner_set):
        steps.append(
            VerificationStep(
                "signature_count",
                False,
                "signatures must have exactly one entry per cosigner (no dups/missing)",
            )
        )
        return ValidationResult(steps, None)
    steps.append(
        VerificationStep(
            "signature_count",
            True,
            f"{len(sla.signatures)} of {len(payload.cosigners)}",
        )
    )

    # Step 8 — signature_verify (one per cosigner, in cosigners order).
    payload_bytes = canonical_json(asdict(payload))
    sig_by_as = {s.meta.isd_as: s for s in sla.signatures}
    for cosigner in payload.cosigners:
        sig = sig_by_as[cosigner]
        if not verifier.verify(signature=sig, payload_bytes=payload_bytes):
            steps.append(
                VerificationStep(
                    "signature_verify", False, f"{cosigner} signature rejected"
                )
            )
            return ValidationResult(steps, None)
        steps.append(VerificationStep("signature_verify", True, cosigner))

    # Step 9 — no_duplicate.
    if payload.sla_id in storage.slas:
        steps.append(
            VerificationStep("no_duplicate", False, f"sla_id {payload.sla_id} already ingested")
        )
        return ValidationResult(steps, None)
    steps.append(VerificationStep("no_duplicate", True, "new sla_id"))

    return ValidationResult(steps, sla)


def _first_error(e: ValidationError) -> str:
    """Compact one-line summary of the first pydantic validation error."""
    errs = e.errors()
    if not errs:
        return "invalid payload shape"
    first = errs[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = first.get("msg", "invalid")
    return f"{loc}: {msg}" if loc else msg


def steps_as_dicts(steps: list[VerificationStep]) -> list[dict]:
    """JSON-serialisable form of the step list (for HTTP responses)."""

    return [{"step": s.step, "ok": s.ok, "detail": s.detail} for s in steps]


@dataclass(frozen=True)
class ClaimValidationResult:
    """Outcome of POST /claim validation. Either ``claim`` or ``error`` is set."""

    claim: SignedClaim | None
    error: str | None


def validate_claim_submission(
    body: object,
    *,
    storage: AggregatorStorage,
    verifier: Verifier,
    now: datetime,
) -> ClaimValidationResult:
    """Run the §5.2 POST /claim validation chain; short-circuits on first failure."""

    # Step 1 — Parseable.
    try:
        model = SignedClaimModel.model_validate(body)
    except ValidationError as e:
        return ClaimValidationResult(None, f"parse_json: {_first_error(e)}")
    except (TypeError, ValueError) as e:
        return ClaimValidationResult(None, f"parse_json: {e}")

    claim = model.to_dataclass()
    payload = claim.payload

    # Step 2 — schema_version.
    if payload.schema_version != 2:
        return ClaimValidationResult(
            None, f"schema_version: expected 2, got {payload.schema_version}"
        )

    # Step 3 — content_hash.
    expected_id = compute_claim_id(payload)
    if payload.claim_id != expected_id:
        return ClaimValidationResult(
            None, f"content_hash: claim_id mismatch (expected {expected_id})"
        )

    # Step 4 — referenced SLA exists and is currently within its validity window.
    sla = storage.get_sla(payload.sla_id)
    if sla is None:
        return ClaimValidationResult(None, f"unknown_sla: {payload.sla_id}")
    try:
        sla_from = _parse_iso(sla.payload.valid_from)
        sla_until = _parse_iso(sla.payload.valid_until)
    except ValueError as e:
        return ClaimValidationResult(None, f"sla_timestamps: {e}")
    if not (sla_from <= now < sla_until):
        return ClaimValidationResult(
            None,
            f"sla_expired: now {now.isoformat()} outside [{sla.payload.valid_from}, {sla.payload.valid_until})",
        )

    # Step 5 — no self-dealing, except for the source AS (first cosigner).
    # The source AS of a path is the natural buyer of SLAs covering its
    # outbound egress; allowing it to claim the SLA it also cosigns at
    # position 0 is legitimate, not self-dealing.
    if (
        payload.claimant in sla.payload.cosigners
        and sla.payload.cosigners[0] != payload.claimant
    ):
        return ClaimValidationResult(
            None, f"self_dealing: claimant {payload.claimant} is a cosigner"
        )

    # Step 6 — gb_purchased > 0.
    if payload.gb_purchased <= 0:
        return ClaimValidationResult(
            None, f"gb_purchased: must be > 0, got {payload.gb_purchased}"
        )

    # Step 7 — price arithmetic (Decimal value equality per 2026-04-19 amendment).
    try:
        paid = Decimal(payload.price_paid_chf)
        expected_total = Decimal(sla.payload.price_per_gb) * Decimal(payload.gb_purchased)
    except (InvalidOperation, ValueError) as e:
        return ClaimValidationResult(None, f"price_arithmetic: bad decimal: {e}")
    if paid != expected_total:
        return ClaimValidationResult(
            None,
            f"price_arithmetic: paid {paid} != expected {expected_total} "
            f"({sla.payload.price_per_gb} * {payload.gb_purchased})",
        )

    # Step 8 — signature meta matches claimant.
    if claim.signature.meta.isd_as != payload.claimant:
        return ClaimValidationResult(
            None,
            f"signature_meta: meta.isd_as={claim.signature.meta.isd_as} != claimant={payload.claimant}",
        )

    # Step 9 — signature verifies.
    payload_bytes = canonical_json(asdict(payload))
    if not verifier.verify(signature=claim.signature, payload_bytes=payload_bytes):
        return ClaimValidationResult(
            None, f"signature_verify: bad signature from {payload.claimant}"
        )

    return ClaimValidationResult(claim, None)


@dataclass(frozen=True)
class ComplaintValidationResult:
    complaint: SignedComplaint | None
    error: str | None


def validate_complaint_submission(
    body: object,
    *,
    storage: AggregatorStorage,
    verifier: Verifier,
    now: datetime,
    dedup_window_minutes: int = 5,
) -> ComplaintValidationResult:
    """Run the §5.2 POST /complaint validation chain; short-circuits on first failure."""

    # Step 1 — Parseable.
    try:
        model = SignedComplaintModel.model_validate(body)
    except ValidationError as e:
        return ComplaintValidationResult(None, f"parse_json: {_first_error(e)}")
    except (TypeError, ValueError) as e:
        return ComplaintValidationResult(None, f"parse_json: {e}")

    complaint = model.to_dataclass()
    payload = complaint.payload

    # Step 2 — schema_version.
    if payload.schema_version != 2:
        return ComplaintValidationResult(
            None, f"schema_version: expected 2, got {payload.schema_version}"
        )

    # Step 3 — content_hash.
    expected_id = compute_complaint_id(payload)
    if payload.complaint_id != expected_id:
        return ComplaintValidationResult(
            None, f"content_hash: complaint_id mismatch (expected {expected_id})"
        )

    # Step 4 — referenced SLA exists and is within its validity window.
    sla = storage.get_sla(payload.sla_id)
    if sla is None:
        return ComplaintValidationResult(None, f"unknown_sla: {payload.sla_id}")
    try:
        sla_from = _parse_iso(sla.payload.valid_from)
        sla_until = _parse_iso(sla.payload.valid_until)
    except ValueError as e:
        return ComplaintValidationResult(None, f"sla_timestamps: {e}")
    if not (sla_from <= now < sla_until):
        return ComplaintValidationResult(
            None, f"sla_expired: now outside SLA validity window"
        )

    # Step 5 — path_used matches SLA's path.
    if list(payload.path_used) != list(sla.payload.path):
        return ComplaintValidationResult(
            None, "path_mismatch: path_used differs from SLA path"
        )

    # Step 6 — complainant not in cosigners.
    if payload.complainant in sla.payload.cosigners:
        return ComplaintValidationResult(
            None,
            f"self_complaint: complainant {payload.complainant} is a cosigner",
        )

    # Step 7 — allowed metric.
    if payload.metric not in _METRIC_TO_BOUND_FIELD:
        return ComplaintValidationResult(
            None, f"metric: unknown metric {payload.metric!r}"
        )

    # Step 8 — SLA has a non-None bound for this metric.
    bound_field = _METRIC_TO_BOUND_FIELD[payload.metric]
    if getattr(sla.payload.bounds, bound_field) is None:
        return ComplaintValidationResult(
            None, f"bound_absent: SLA has no {bound_field} bound"
        )

    # Step 9 — attachment count.
    if len(payload.attachments) > _MAX_ATTACHMENTS:
        return ComplaintValidationResult(
            None,
            f"attachments_count: {len(payload.attachments)} > {_MAX_ATTACHMENTS}",
        )

    # Step 10 — per-attachment validation.
    for idx, att in enumerate(payload.attachments):
        if att.kind not in _ALLOWED_ATTACHMENT_KINDS:
            return ComplaintValidationResult(
                None, f"attachment[{idx}].kind: unknown kind {att.kind!r}"
            )
        if not att.filename:
            return ComplaintValidationResult(
                None, f"attachment[{idx}].filename: empty"
            )
        if len(att.filename) > _MAX_FILENAME_LEN:
            return ComplaintValidationResult(
                None,
                f"attachment[{idx}].filename: length {len(att.filename)} > {_MAX_FILENAME_LEN}",
            )
        if "/" in att.filename or "\\" in att.filename:
            return ComplaintValidationResult(
                None,
                f"attachment[{idx}].filename: path separators not allowed",
            )
        try:
            decoded = base64.b64decode(att.content_b64, validate=True)
        except (ValueError, _B64Error) as e:
            return ComplaintValidationResult(
                None, f"attachment[{idx}].content_b64: invalid base64: {e}"
            )
        if len(decoded) > _MAX_ATTACHMENT_BYTES:
            return ComplaintValidationResult(
                None,
                f"attachment[{idx}].size: {len(decoded)} > {_MAX_ATTACHMENT_BYTES}",
            )

    # Step 11 — signature meta matches complainant.
    if complaint.signature.meta.isd_as != payload.complainant:
        return ComplaintValidationResult(
            None,
            f"signature_meta: meta.isd_as={complaint.signature.meta.isd_as} "
            f"!= complainant={payload.complainant}",
        )

    # Step 12 — signature verifies.
    payload_bytes = canonical_json(asdict(payload))
    if not verifier.verify(signature=complaint.signature, payload_bytes=payload_bytes):
        return ComplaintValidationResult(
            None, f"signature_verify: bad signature from {payload.complainant}"
        )

    # Step 13 — dedup on (complainant, sla_id, metric) within window.
    try:
        new_observed = _parse_iso(payload.observed_at)
    except ValueError as e:
        return ComplaintValidationResult(None, f"observed_at: bad timestamp: {e}")
    window_seconds = dedup_window_minutes * 60
    for _, stored in storage.complaints:
        sp = stored.payload
        if (
            sp.complainant == payload.complainant
            and sp.sla_id == payload.sla_id
            and sp.metric == payload.metric
        ):
            try:
                stored_observed = _parse_iso(sp.observed_at)
            except ValueError:
                continue
            if abs((new_observed - stored_observed).total_seconds()) <= window_seconds:
                return ComplaintValidationResult(
                    None,
                    f"duplicate_complaint: within {dedup_window_minutes}min of existing complaint",
                )

    return ComplaintValidationResult(complaint, None)


__all__ = [
    "ClaimValidationResult",
    "ComplaintValidationResult",
    "ValidationResult",
    "VerificationStep",
    "steps_as_dicts",
    "validate_claim_submission",
    "validate_complaint_submission",
    "validate_sla_submission",
]
