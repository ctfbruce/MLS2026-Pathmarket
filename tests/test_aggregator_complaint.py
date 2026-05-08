"""Step B4 — ``POST /complaint`` tests (``DESIGN.md`` §5.2).

Happy path, attachments round-trip, one failure test per validation step,
dedup window behavior.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, replace

from fastapi.testclient import TestClient

from pathmarket.aggregator.app import create_app
from pathmarket.aggregator.models import SignedComplaintModel, SignedSLAModel
from pathmarket.aggregator.storage import AggregatorStorage
from pathmarket.canonical import canonical_json, compute_complaint_id
from pathmarket.schemas import (
    Attachment,
    PathHop,
    SLABounds,
    Signature,
    SignedComplaint,
)

from tests.fixtures.builders import make_complaint_payload
from tests.fixtures.crypto import make_test_key
from tests.fixtures.signing import build_signed_sla, make_keys_for, make_verifier


COSIGNERS = ["1-ff00:0:112", "1-ff00:0:111", "1-ff00:0:122"]
COMPLAINANT = "1-ff00:0:221"


def _fresh_setup(*, bounds: SLABounds | None = None):
    all_keys = make_keys_for(COSIGNERS + [COMPLAINANT])
    storage = AggregatorStorage()
    verifier = make_verifier(all_keys)
    app = create_app(storage=storage, verifier=verifier)
    client = TestClient(app)

    cosigner_keys = {a: all_keys[a] for a in COSIGNERS}
    sla = build_signed_sla(
        cosigner_keys, cosigners=COSIGNERS, bounds=bounds
    )
    r = client.post("/sla", json=SignedSLAModel.from_dataclass(sla).model_dump())
    assert r.status_code == 201, r.json()
    return client, storage, all_keys[COMPLAINANT], sla


def _build_complaint(
    sla,
    complainant_key,
    *,
    metric: str = "latency_ms",
    measured_value: int = 22,
    observed_at: str = "2026-04-18T09:14:00Z",
    note: str = "",
    attachments: list[Attachment] | None = None,
) -> SignedComplaint:
    payload = make_complaint_payload(
        sla_id=sla.payload.sla_id,
        complainant=complainant_key.isd_as,
        path_used=list(sla.payload.path),
        metric=metric,
        measured_value=measured_value,
        observed_at=observed_at,
        note=note,
        attachments=attachments or [],
    )
    payload = replace(payload, complaint_id=compute_complaint_id(payload))
    sig = complainant_key.sign(canonical_json(asdict(payload)))
    return SignedComplaint(payload=payload, signature=sig)


def _submit(client, complaint):
    return client.post(
        "/complaint", json=SignedComplaintModel.from_dataclass(complaint).model_dump()
    )


class TestHappyPath:
    def test_complaint_accepted_and_logged(self) -> None:
        client, storage, complainant_key, sla = _fresh_setup()
        c = _build_complaint(sla, complainant_key)
        r = _submit(client, c)
        assert r.status_code == 201
        assert r.json() == {
            "complaint_id": c.payload.complaint_id,
            "status": "accepted",
        }
        assert len(storage.complaints) == 1

    def test_attachments_and_note_round_trip(self) -> None:
        client, storage, complainant_key, sla = _fresh_setup()
        attachments = [
            Attachment(
                kind="text",
                filename="trace.txt",
                content_b64=base64.b64encode(b"hello world").decode("ascii"),
            ),
            Attachment(
                kind="json",
                filename="probe.json",
                content_b64=base64.b64encode(b'{"ms": 22}').decode("ascii"),
            ),
        ]
        note = "latency spike observed during 5-minute probe"
        c = _build_complaint(sla, complainant_key, note=note, attachments=attachments)
        r = _submit(client, c)
        assert r.status_code == 201
        stored = storage.complaints[0][1]
        assert stored.payload.note == note
        assert len(stored.payload.attachments) == 2
        assert stored.payload.attachments[0].filename == "trace.txt"
        assert base64.b64decode(stored.payload.attachments[0].content_b64) == b"hello world"


class TestComplaintValidationFailures:
    def test_parse_fails_on_bad_json(self) -> None:
        client, _, _, _ = _fresh_setup()
        r = client.post(
            "/complaint", content="{nope", headers={"content-type": "application/json"}
        )
        assert r.status_code == 400
        assert r.json()["error"].startswith("parse_json:")

    def test_wrong_schema_version(self) -> None:
        client, _, complainant_key, sla = _fresh_setup()
        c = _build_complaint(sla, complainant_key)
        bad = replace(c.payload, complaint_id="", schema_version=1)
        bad = replace(bad, complaint_id=compute_complaint_id(bad))
        sig = complainant_key.sign(canonical_json(asdict(bad)))
        r = _submit(client, SignedComplaint(payload=bad, signature=sig))
        assert r.status_code == 400
        assert r.json()["error"].startswith("schema_version:")

    def test_content_hash_mismatch(self) -> None:
        client, _, complainant_key, sla = _fresh_setup()
        c = _build_complaint(sla, complainant_key)
        tampered = SignedComplaint(
            payload=replace(c.payload, complaint_id="sha256:" + "0" * 64),
            signature=c.signature,
        )
        r = _submit(client, tampered)
        assert r.status_code == 400
        assert r.json()["error"].startswith("content_hash:")

    def test_unknown_sla(self) -> None:
        client, _, complainant_key, sla = _fresh_setup()
        c = _build_complaint(sla, complainant_key)
        bad_payload = replace(c.payload, complaint_id="", sla_id="sha256:" + "d" * 64)
        bad_payload = replace(bad_payload, complaint_id=compute_complaint_id(bad_payload))
        sig = complainant_key.sign(canonical_json(asdict(bad_payload)))
        r = _submit(client, SignedComplaint(payload=bad_payload, signature=sig))
        assert r.status_code == 400
        assert r.json()["error"].startswith("unknown_sla:")

    def test_path_used_mismatch(self) -> None:
        client, _, complainant_key, sla = _fresh_setup()
        wrong_path = [
            PathHop(isd_as="1-ff00:0:999", ingress=0, egress=0),
            PathHop(isd_as="1-ff00:0:998", ingress=0, egress=0),
        ]
        c = _build_complaint(sla, complainant_key)
        bp = replace(c.payload, complaint_id="", path_used=wrong_path)
        bp = replace(bp, complaint_id=compute_complaint_id(bp))
        sig = complainant_key.sign(canonical_json(asdict(bp)))
        r = _submit(client, SignedComplaint(payload=bp, signature=sig))
        assert r.status_code == 400
        assert r.json()["error"].startswith("path_mismatch:")

    def test_self_complaint_rejected(self) -> None:
        """A cosigner cannot complain about its own SLA."""
        all_keys = make_keys_for(COSIGNERS)
        storage = AggregatorStorage()
        app = create_app(storage=storage, verifier=make_verifier(all_keys))
        client = TestClient(app)
        sla = build_signed_sla(all_keys, cosigners=COSIGNERS)
        assert client.post(
            "/sla", json=SignedSLAModel.from_dataclass(sla).model_dump()
        ).status_code == 201
        cosigner_key = all_keys[COSIGNERS[0]]
        c = _build_complaint(sla, cosigner_key)
        r = _submit(client, c)
        assert r.status_code == 400
        assert r.json()["error"].startswith("self_complaint:")

    def test_unknown_metric(self) -> None:
        client, _, complainant_key, sla = _fresh_setup()
        c = _build_complaint(sla, complainant_key)
        bp = replace(c.payload, complaint_id="", metric="jitter_ms")
        bp = replace(bp, complaint_id=compute_complaint_id(bp))
        sig = complainant_key.sign(canonical_json(asdict(bp)))
        r = _submit(client, SignedComplaint(payload=bp, signature=sig))
        assert r.status_code == 400
        assert r.json()["error"].startswith("metric:")

    def test_bound_absent_for_metric(self) -> None:
        """SLA bound on the complained-about metric is None."""
        client, _, complainant_key, sla = _fresh_setup(
            bounds=SLABounds(latency_max_ms=10, loss_max_ppm=None, bandwidth_min_kbps=None)
        )
        c = _build_complaint(sla, complainant_key, metric="loss_ppm", measured_value=800)
        r = _submit(client, c)
        assert r.status_code == 400
        assert r.json()["error"].startswith("bound_absent:")

    def test_too_many_attachments(self) -> None:
        client, _, complainant_key, sla = _fresh_setup()
        atts = [
            Attachment(kind="text", filename=f"a{i}.txt", content_b64="aGk=")
            for i in range(4)
        ]
        c = _build_complaint(sla, complainant_key, attachments=atts)
        r = _submit(client, c)
        assert r.status_code == 400
        assert r.json()["error"].startswith("attachments_count:")

    def test_attachment_bad_kind(self) -> None:
        client, _, complainant_key, sla = _fresh_setup()
        atts = [Attachment(kind="binary", filename="x.bin", content_b64="AAAA")]
        c = _build_complaint(sla, complainant_key, attachments=atts)
        r = _submit(client, c)
        assert r.status_code == 400
        assert r.json()["error"].startswith("attachment[0].kind:")

    def test_attachment_filename_with_slash(self) -> None:
        client, _, complainant_key, sla = _fresh_setup()
        atts = [Attachment(kind="text", filename="dir/trace.txt", content_b64="aGk=")]
        c = _build_complaint(sla, complainant_key, attachments=atts)
        r = _submit(client, c)
        assert r.status_code == 400
        assert r.json()["error"].startswith("attachment[0].filename:")

    def test_attachment_empty_filename(self) -> None:
        client, _, complainant_key, sla = _fresh_setup()
        atts = [Attachment(kind="text", filename="", content_b64="aGk=")]
        c = _build_complaint(sla, complainant_key, attachments=atts)
        r = _submit(client, c)
        assert r.status_code == 400
        assert r.json()["error"].startswith("attachment[0].filename:")

    def test_attachment_oversize(self) -> None:
        client, _, complainant_key, sla = _fresh_setup()
        big = b"A" * (64 * 1024 + 1)
        atts = [
            Attachment(
                kind="log",
                filename="huge.log",
                content_b64=base64.b64encode(big).decode("ascii"),
            )
        ]
        c = _build_complaint(sla, complainant_key, attachments=atts)
        r = _submit(client, c)
        assert r.status_code == 400
        assert r.json()["error"].startswith("attachment[0].size:")

    def test_attachment_at_64kib_boundary_accepted(self) -> None:
        """Exactly 64 KiB decoded must pass (bound is inclusive)."""
        client, _, complainant_key, sla = _fresh_setup()
        payload = b"B" * (64 * 1024)
        atts = [
            Attachment(
                kind="log",
                filename="edge.log",
                content_b64=base64.b64encode(payload).decode("ascii"),
            )
        ]
        c = _build_complaint(sla, complainant_key, attachments=atts)
        r = _submit(client, c)
        assert r.status_code == 201

    def test_signature_meta_mismatch(self) -> None:
        client, _, complainant_key, sla = _fresh_setup()
        c = _build_complaint(sla, complainant_key)
        wrong = replace(c.signature.meta, isd_as="1-ff00:0:987")
        r = _submit(
            client,
            SignedComplaint(
                payload=c.payload,
                signature=Signature(meta=wrong, sig_bytes=c.signature.sig_bytes),
            ),
        )
        assert r.status_code == 400
        assert r.json()["error"].startswith("signature_meta:")

    def test_signature_verify_fails_on_tampered_bytes(self) -> None:
        client, _, complainant_key, sla = _fresh_setup()
        c = _build_complaint(sla, complainant_key)
        bad_sig = Signature(
            meta=c.signature.meta,
            sig_bytes=c.signature.sig_bytes[:-1] + bytes([c.signature.sig_bytes[-1] ^ 0xFF]),
        )
        r = _submit(client, SignedComplaint(payload=c.payload, signature=bad_sig))
        assert r.status_code == 400
        assert r.json()["error"].startswith("signature_verify:")

    def test_dedup_rejects_same_complainant_metric_within_window(self) -> None:
        client, _, complainant_key, sla = _fresh_setup()
        c1 = _build_complaint(
            sla, complainant_key, observed_at="2026-04-18T09:14:00Z"
        )
        assert _submit(client, c1).status_code == 201
        c2 = _build_complaint(
            sla, complainant_key, observed_at="2026-04-18T09:16:00Z"
        )  # 2 min later: inside 5-min window.
        r = _submit(client, c2)
        # Dedup returns 200 {dedup: true} so simulator buyers don't see 400
        # spam when they re-sample the same violation every tick.
        assert r.status_code == 200
        assert r.json() == {"dedup": True, "detail": r.json()["detail"]}
        assert r.json()["detail"].startswith("duplicate_complaint:")

    def test_dedup_allows_same_complainant_outside_window(self) -> None:
        client, _, complainant_key, sla = _fresh_setup()
        c1 = _build_complaint(
            sla, complainant_key, observed_at="2026-04-18T09:14:00Z"
        )
        assert _submit(client, c1).status_code == 201
        c2 = _build_complaint(
            sla, complainant_key, observed_at="2026-04-18T09:20:00Z"
        )  # 6 min later.
        r = _submit(client, c2)
        assert r.status_code == 201

    def test_dedup_allows_different_metrics_within_window(self) -> None:
        client, _, complainant_key, sla = _fresh_setup(
            bounds=SLABounds(latency_max_ms=10, loss_max_ppm=500, bandwidth_min_kbps=None)
        )
        c1 = _build_complaint(
            sla, complainant_key, metric="latency_ms",
            observed_at="2026-04-18T09:14:00Z",
        )
        assert _submit(client, c1).status_code == 201
        c2 = _build_complaint(
            sla, complainant_key, metric="loss_ppm", measured_value=800,
            observed_at="2026-04-18T09:15:00Z",
        )
        r = _submit(client, c2)
        assert r.status_code == 201
