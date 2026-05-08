"""Pydantic models mirroring ``pathmarket.schemas`` dataclasses 1:1 (``DESIGN.md`` §5.2).

Each model has ``to_dataclass()`` and ``from_dataclass()`` helpers so the HTTP
boundary handles JSON/pydantic and the core logic keeps working with frozen
dataclasses (hashing, signing, scoring).

The only non-trivial transformation is ``Signature.sig_bytes``: on the wire it's
base64-encoded, in the dataclass it's raw ``bytes``.
"""

from __future__ import annotations

import base64
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from pathmarket import schemas


class _Model(BaseModel):
    """Base class: forbid extras to catch typos at the HTTP boundary."""

    model_config = ConfigDict(extra="forbid")


class PathHopModel(_Model):
    isd_as: str
    ingress: int
    egress: int

    def to_dataclass(self) -> schemas.PathHop:
        return schemas.PathHop(isd_as=self.isd_as, ingress=self.ingress, egress=self.egress)

    @classmethod
    def from_dataclass(cls, h: schemas.PathHop) -> "PathHopModel":
        return cls(isd_as=h.isd_as, ingress=h.ingress, egress=h.egress)


class SLABoundsModel(_Model):
    latency_max_ms: Optional[int] = None
    loss_max_ppm: Optional[int] = None
    bandwidth_min_kbps: Optional[int] = None

    def to_dataclass(self) -> schemas.SLABounds:
        return schemas.SLABounds(
            latency_max_ms=self.latency_max_ms,
            loss_max_ppm=self.loss_max_ppm,
            bandwidth_min_kbps=self.bandwidth_min_kbps,
        )

    @classmethod
    def from_dataclass(cls, b: schemas.SLABounds) -> "SLABoundsModel":
        return cls(
            latency_max_ms=b.latency_max_ms,
            loss_max_ppm=b.loss_max_ppm,
            bandwidth_min_kbps=b.bandwidth_min_kbps,
        )


class SignatureMetaModel(_Model):
    isd_as: str
    key_id: str
    trc_serial: int
    trc_base: int

    def to_dataclass(self) -> schemas.SignatureMeta:
        return schemas.SignatureMeta(
            isd_as=self.isd_as,
            key_id=self.key_id,
            trc_serial=self.trc_serial,
            trc_base=self.trc_base,
        )

    @classmethod
    def from_dataclass(cls, m: schemas.SignatureMeta) -> "SignatureMetaModel":
        return cls(
            isd_as=m.isd_as, key_id=m.key_id, trc_serial=m.trc_serial, trc_base=m.trc_base
        )


class SignatureModel(_Model):
    meta: SignatureMetaModel
    sig_bytes: str = Field(..., description="base64-encoded Ed25519 signature bytes")

    def to_dataclass(self) -> schemas.Signature:
        return schemas.Signature(
            meta=self.meta.to_dataclass(),
            sig_bytes=base64.b64decode(self.sig_bytes),
        )

    @classmethod
    def from_dataclass(cls, s: schemas.Signature) -> "SignatureModel":
        return cls(
            meta=SignatureMetaModel.from_dataclass(s.meta),
            sig_bytes=base64.b64encode(s.sig_bytes).decode("ascii"),
        )


class SLAPayloadModel(_Model):
    sla_id: str
    path: list[PathHopModel]
    cosigners: list[str]
    bounds: SLABoundsModel
    price_per_gb: str
    valid_from: str
    valid_until: str
    nonce: str
    schema_version: int

    def to_dataclass(self) -> schemas.SLAPayload:
        return schemas.SLAPayload(
            sla_id=self.sla_id,
            path=[h.to_dataclass() for h in self.path],
            cosigners=list(self.cosigners),
            bounds=self.bounds.to_dataclass(),
            price_per_gb=self.price_per_gb,
            valid_from=self.valid_from,
            valid_until=self.valid_until,
            nonce=self.nonce,
            schema_version=self.schema_version,
        )

    @classmethod
    def from_dataclass(cls, p: schemas.SLAPayload) -> "SLAPayloadModel":
        return cls(
            sla_id=p.sla_id,
            path=[PathHopModel.from_dataclass(h) for h in p.path],
            cosigners=list(p.cosigners),
            bounds=SLABoundsModel.from_dataclass(p.bounds),
            price_per_gb=p.price_per_gb,
            valid_from=p.valid_from,
            valid_until=p.valid_until,
            nonce=p.nonce,
            schema_version=p.schema_version,
        )


class SignedSLAModel(_Model):
    payload: SLAPayloadModel
    signatures: list[SignatureModel]

    def to_dataclass(self) -> schemas.SignedSLA:
        return schemas.SignedSLA(
            payload=self.payload.to_dataclass(),
            signatures=[s.to_dataclass() for s in self.signatures],
        )

    @classmethod
    def from_dataclass(cls, s: schemas.SignedSLA) -> "SignedSLAModel":
        return cls(
            payload=SLAPayloadModel.from_dataclass(s.payload),
            signatures=[SignatureModel.from_dataclass(x) for x in s.signatures],
        )


class ClaimPayloadModel(_Model):
    claim_id: str
    sla_id: str
    claimant: str
    gb_purchased: int
    claimed_at: str
    price_paid_chf: str
    schema_version: int

    def to_dataclass(self) -> schemas.ClaimPayload:
        return schemas.ClaimPayload(
            claim_id=self.claim_id,
            sla_id=self.sla_id,
            claimant=self.claimant,
            gb_purchased=self.gb_purchased,
            claimed_at=self.claimed_at,
            price_paid_chf=self.price_paid_chf,
            schema_version=self.schema_version,
        )

    @classmethod
    def from_dataclass(cls, p: schemas.ClaimPayload) -> "ClaimPayloadModel":
        return cls(
            claim_id=p.claim_id,
            sla_id=p.sla_id,
            claimant=p.claimant,
            gb_purchased=p.gb_purchased,
            claimed_at=p.claimed_at,
            price_paid_chf=p.price_paid_chf,
            schema_version=p.schema_version,
        )


class SignedClaimModel(_Model):
    payload: ClaimPayloadModel
    signature: SignatureModel

    def to_dataclass(self) -> schemas.SignedClaim:
        return schemas.SignedClaim(
            payload=self.payload.to_dataclass(),
            signature=self.signature.to_dataclass(),
        )

    @classmethod
    def from_dataclass(cls, c: schemas.SignedClaim) -> "SignedClaimModel":
        return cls(
            payload=ClaimPayloadModel.from_dataclass(c.payload),
            signature=SignatureModel.from_dataclass(c.signature),
        )


class AttachmentModel(_Model):
    kind: str
    filename: str
    content_b64: str

    def to_dataclass(self) -> schemas.Attachment:
        return schemas.Attachment(kind=self.kind, filename=self.filename, content_b64=self.content_b64)

    @classmethod
    def from_dataclass(cls, a: schemas.Attachment) -> "AttachmentModel":
        return cls(kind=a.kind, filename=a.filename, content_b64=a.content_b64)


class ComplaintPayloadModel(_Model):
    complaint_id: str
    sla_id: str
    complainant: str
    path_used: list[PathHopModel]
    metric: str
    measured_value: int
    observed_at: str
    note: str
    attachments: list[AttachmentModel]
    schema_version: int

    def to_dataclass(self) -> schemas.ComplaintPayload:
        return schemas.ComplaintPayload(
            complaint_id=self.complaint_id,
            sla_id=self.sla_id,
            complainant=self.complainant,
            path_used=[h.to_dataclass() for h in self.path_used],
            metric=self.metric,
            measured_value=self.measured_value,
            observed_at=self.observed_at,
            note=self.note,
            attachments=[a.to_dataclass() for a in self.attachments],
            schema_version=self.schema_version,
        )

    @classmethod
    def from_dataclass(cls, p: schemas.ComplaintPayload) -> "ComplaintPayloadModel":
        return cls(
            complaint_id=p.complaint_id,
            sla_id=p.sla_id,
            complainant=p.complainant,
            path_used=[PathHopModel.from_dataclass(h) for h in p.path_used],
            metric=p.metric,
            measured_value=p.measured_value,
            observed_at=p.observed_at,
            note=p.note,
            attachments=[AttachmentModel.from_dataclass(a) for a in p.attachments],
            schema_version=p.schema_version,
        )


class SignedComplaintModel(_Model):
    payload: ComplaintPayloadModel
    signature: SignatureModel

    def to_dataclass(self) -> schemas.SignedComplaint:
        return schemas.SignedComplaint(
            payload=self.payload.to_dataclass(),
            signature=self.signature.to_dataclass(),
        )

    @classmethod
    def from_dataclass(cls, c: schemas.SignedComplaint) -> "SignedComplaintModel":
        return cls(
            payload=ComplaintPayloadModel.from_dataclass(c.payload),
            signature=SignatureModel.from_dataclass(c.signature),
        )


class ScoreModel(_Model):
    isd_as: str
    score: float
    components: dict

    def to_dataclass(self) -> schemas.Score:
        return schemas.Score(isd_as=self.isd_as, score=self.score, components=dict(self.components))

    @classmethod
    def from_dataclass(cls, s: schemas.Score) -> "ScoreModel":
        return cls(isd_as=s.isd_as, score=s.score, components=dict(s.components))


__all__ = [
    "AttachmentModel",
    "ClaimPayloadModel",
    "ComplaintPayloadModel",
    "PathHopModel",
    "SLABoundsModel",
    "SLAPayloadModel",
    "ScoreModel",
    "SignatureMetaModel",
    "SignatureModel",
    "SignedClaimModel",
    "SignedComplaintModel",
    "SignedSLAModel",
]
