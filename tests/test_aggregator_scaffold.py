"""Step B1 — aggregator scaffold tests.

Covers:
- ``create_app()`` returns a FastAPI app with `/health`.
- In-memory storage matches §5.4 shape.
- Pydantic ↔ dataclass round-trip is lossless for every signed artefact.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from pathmarket.aggregator.app import create_app
from pathmarket.aggregator.models import (
    ScoreModel,
    SignedClaimModel,
    SignedComplaintModel,
    SignedSLAModel,
)
from pathmarket.aggregator.storage import AggregatorStorage, TICKER_MAXLEN, TickerEvent
from pathmarket.canonical import (
    compute_claim_id,
    compute_complaint_id,
    compute_sla_id,
)
from pathmarket.schemas import (
    Attachment,
    Score,
    SignedClaim,
    SignedComplaint,
    SignedSLA,
    Signature,
    SignatureMeta,
)
from dataclasses import replace

from tests.fixtures.builders import make_claim_payload, make_complaint_payload, make_sla_payload
from tests.fixtures.crypto import make_test_key


def _sig(isd_as: str = "1-ff00:0:112") -> Signature:
    return Signature(
        meta=SignatureMeta(isd_as=isd_as, key_id="default", trc_serial=0, trc_base=0),
        sig_bytes=b"\x00" * 64,
    )


class TestHealthEndpoint:
    def test_health_returns_ok(self) -> None:
        client = TestClient(create_app())
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestStorage:
    def test_empty_on_construction(self) -> None:
        s = AggregatorStorage()
        assert s.slas == {}
        assert s.sla_ingestion_times == {}
        assert s.claims == []
        assert s.complaints == []
        assert len(s.ticker_events) == 0

    def test_add_sla_populates_log_and_ingestion_time(self) -> None:
        s = AggregatorStorage()
        payload = make_sla_payload()
        payload = replace(payload, sla_id=compute_sla_id(payload))
        sla = SignedSLA(payload=payload, signatures=[_sig()])
        ts = datetime(2026, 4, 19, 12, 30, tzinfo=timezone.utc)
        s.add_sla(sla, ts)
        assert s.get_sla(payload.sla_id) is sla
        assert s.sla_ingestion_times[payload.sla_id] == ts

    def test_ticker_is_bounded(self) -> None:
        s = AggregatorStorage()
        ts = datetime(2026, 4, 19, 12, 30, tzinfo=timezone.utc)
        for i in range(TICKER_MAXLEN + 50):
            s.append_ticker(TickerEvent(type="sla_signed", timestamp=ts, detail=f"#{i}"))
        assert len(s.ticker_events) == TICKER_MAXLEN
        # Oldest entries have been dropped.
        assert s.ticker_events[0].detail == f"#{50}"

    def test_reset_clears_everything(self) -> None:
        s = AggregatorStorage()
        payload = make_sla_payload()
        payload = replace(payload, sla_id=compute_sla_id(payload))
        sla = SignedSLA(payload=payload, signatures=[_sig()])
        ts = datetime(2026, 4, 19, 12, 30, tzinfo=timezone.utc)
        s.add_sla(sla, ts)
        s.append_ticker(TickerEvent(type="sla_signed", timestamp=ts, detail="x"))
        s.reset()
        assert s.slas == {}
        assert s.sla_ingestion_times == {}
        assert len(s.ticker_events) == 0


class TestPydanticRoundTrip:
    """Every model's from_dataclass → to_dataclass is the identity."""

    def test_signed_sla_roundtrip(self) -> None:
        key = make_test_key("1-ff00:0:112")
        payload = make_sla_payload(cosigners=[key.isd_as], path=[
            __import__("pathmarket.schemas", fromlist=["PathHop"]).PathHop(
                isd_as=key.isd_as, ingress=0, egress=0
            )
        ])
        payload = replace(payload, sla_id=compute_sla_id(payload))
        sig = key.sign(b"anything")
        sla = SignedSLA(payload=payload, signatures=[sig])

        model = SignedSLAModel.from_dataclass(sla)
        # JSON roundtrip — the real HTTP path.
        restored = SignedSLAModel.model_validate_json(model.model_dump_json()).to_dataclass()
        assert restored == sla

    def test_signed_claim_roundtrip(self) -> None:
        key = make_test_key("1-ff00:0:130")
        payload = make_claim_payload(claimant=key.isd_as)
        payload = replace(payload, claim_id=compute_claim_id(payload))
        sig = key.sign(b"anything")
        claim = SignedClaim(payload=payload, signature=sig)

        model = SignedClaimModel.from_dataclass(claim)
        restored = SignedClaimModel.model_validate_json(model.model_dump_json()).to_dataclass()
        assert restored == claim

    def test_signed_complaint_with_attachments_roundtrip(self) -> None:
        key = make_test_key("1-ff00:0:221")
        payload = make_complaint_payload(
            complainant=key.isd_as,
            note="measured 22ms, bound 10ms",
            attachments=[
                Attachment(kind="text", filename="trace.txt", content_b64="aGVsbG8="),
                Attachment(kind="json", filename="probe.json", content_b64="eyJhIjoxfQ=="),
            ],
        )
        payload = replace(payload, complaint_id=compute_complaint_id(payload))
        sig = key.sign(b"anything")
        complaint = SignedComplaint(payload=payload, signature=sig)

        model = SignedComplaintModel.from_dataclass(complaint)
        restored = SignedComplaintModel.model_validate_json(
            model.model_dump_json()
        ).to_dataclass()
        assert restored == complaint

    def test_score_roundtrip(self) -> None:
        s = Score(isd_as="1-ff00:0:130", score=0.76, components={"recent_violations": 1})
        restored = ScoreModel.from_dataclass(s).to_dataclass()
        assert restored == s

    def test_sig_bytes_base64_shape_on_wire(self) -> None:
        """sig_bytes must be base64 ASCII in JSON, raw bytes in the dataclass."""
        key = make_test_key("1-ff00:0:112")
        sig = key.sign(b"x")
        # Build a tiny JSON via SignedClaim just to exercise SignatureModel.
        payload = make_claim_payload(claimant=key.isd_as)
        payload = replace(payload, claim_id=compute_claim_id(payload))
        claim = SignedClaim(payload=payload, signature=sig)

        model = SignedClaimModel.from_dataclass(claim)
        dump = model.model_dump()
        # On the wire sig_bytes is a base64 string, not raw bytes.
        assert isinstance(dump["signature"]["sig_bytes"], str)
        import base64
        assert base64.b64decode(dump["signature"]["sig_bytes"]) == sig.sig_bytes
