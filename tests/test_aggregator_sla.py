"""Step B2 — ``POST /sla`` validation chain tests (``DESIGN.md`` §5.2).

Happy path plus one failure test per validation step. The verification report
shape is also asserted lightly; deeper fixture-based shape tests live in
``test_aggregator_upload_report.py``.
"""

from __future__ import annotations

from dataclasses import asdict, replace

from fastapi.testclient import TestClient

from pathmarket.aggregator.app import create_app
from pathmarket.aggregator.models import SignedSLAModel
from pathmarket.aggregator.storage import AggregatorStorage
from pathmarket.canonical import canonical_json, compute_sla_id
from pathmarket.schemas import (
    PathHop,
    SLABounds,
    Signature,
    SignatureMeta,
    SignedSLA,
)

from tests.fixtures.signing import build_signed_sla, make_keys_for, make_verifier


THREE_ASES = ["1-ff00:0:112", "1-ff00:0:111", "1-ff00:0:122"]


def _client_with(keys_ases: list[str] = THREE_ASES):
    keys = make_keys_for(keys_ases)
    storage = AggregatorStorage()
    verifier = make_verifier(keys)
    app = create_app(storage=storage, verifier=verifier)
    return TestClient(app), keys, storage


def _submit(client: TestClient, sla: SignedSLA):
    return client.post("/sla", json=SignedSLAModel.from_dataclass(sla).model_dump())


def _step_names(resp_json: dict) -> list[str]:
    return [s["step"] for s in resp_json["verification_steps"]]


def _last_step(resp_json: dict) -> dict:
    return resp_json["verification_steps"][-1]


class TestHappyPath:
    def test_accepted_with_all_steps_ok(self) -> None:
        client, keys, storage = _client_with()
        sla = build_signed_sla(keys)
        r = _submit(client, sla)
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == "accepted"
        assert body["sla_id"] == sla.payload.sla_id
        assert all(s["ok"] for s in body["verification_steps"])
        # Verification-step sequence (one signature_verify per cosigner).
        assert _step_names(body) == [
            "parse_json",
            "schema_version",
            "content_hash",
            "validity_window",
            "bounds_nonempty",
            "cosigners_match_path",
            "signature_count",
            "signature_verify",
            "signature_verify",
            "signature_verify",
            "no_duplicate",
        ]
        assert sla.payload.sla_id in storage.slas
        # One ticker event appended.
        assert len(storage.ticker_events) == 1
        assert storage.ticker_events[0].type == "sla_signed"


class TestValidationFailures:
    def test_parse_json_fails_on_invalid_json_body(self) -> None:
        client, _, _ = _client_with()
        r = client.post("/sla", content="not json", headers={"content-type": "application/json"})
        assert r.status_code == 400
        body = r.json()
        assert body["status"] == "rejected"
        assert body["sla_id"] is None
        assert _last_step(body) == {"step": "parse_json", "ok": False, "detail": _last_step(body)["detail"]}
        assert not _last_step(body)["ok"]

    def test_parse_json_fails_on_missing_fields(self) -> None:
        client, _, _ = _client_with()
        r = client.post("/sla", json={"payload": {}, "signatures": []})
        assert r.status_code == 400
        body = r.json()
        assert body["verification_steps"][0]["step"] == "parse_json"
        assert not body["verification_steps"][0]["ok"]

    def test_wrong_schema_version_rejected(self) -> None:
        client, keys, _ = _client_with()
        sla = build_signed_sla(keys)
        # Force schema_version=1 and re-hash + re-sign so every prior step passes.
        bad_payload = replace(sla.payload, sla_id="", schema_version=1)
        bad_payload = replace(bad_payload, sla_id=compute_sla_id(bad_payload))
        payload_bytes = canonical_json(asdict(bad_payload))
        signatures = [keys[c].sign(payload_bytes) for c in bad_payload.cosigners]
        bad_sla = SignedSLA(payload=bad_payload, signatures=signatures)

        r = _submit(client, bad_sla)
        assert r.status_code == 400
        body = r.json()
        assert _last_step(body)["step"] == "schema_version"
        assert not _last_step(body)["ok"]

    def test_content_hash_fails_when_sla_id_tampered(self) -> None:
        client, keys, _ = _client_with()
        sla = build_signed_sla(keys)
        tampered = SignedSLA(
            payload=replace(sla.payload, sla_id="sha256:" + "0" * 64),
            signatures=sla.signatures,
        )
        r = _submit(client, tampered)
        assert r.status_code == 400
        body = r.json()
        assert _last_step(body)["step"] == "content_hash"
        assert not _last_step(body)["ok"]

    def test_validity_window_fails_when_until_not_after_from(self) -> None:
        client, keys, _ = _client_with()
        sla = build_signed_sla(
            keys,
            valid_from="2026-04-20T12:00:00Z",
            valid_until="2026-04-19T12:00:00Z",
        )
        r = _submit(client, sla)
        assert r.status_code == 400
        body = r.json()
        assert _last_step(body)["step"] == "validity_window"
        assert not _last_step(body)["ok"]

    def test_bounds_nonempty_fails_when_all_null(self) -> None:
        client, keys, _ = _client_with()
        sla = build_signed_sla(
            keys,
            bounds=SLABounds(latency_max_ms=None, loss_max_ppm=None, bandwidth_min_kbps=None),
        )
        r = _submit(client, sla)
        assert r.status_code == 400
        body = r.json()
        assert _last_step(body)["step"] == "bounds_nonempty"
        assert not _last_step(body)["ok"]

    def test_cosigners_less_than_two_rejected(self) -> None:
        client, keys, _ = _client_with(keys_ases=["1-ff00:0:112"])
        sla = build_signed_sla(keys, cosigners=["1-ff00:0:112"])
        r = _submit(client, sla)
        assert r.status_code == 400
        body = r.json()
        assert _last_step(body)["step"] == "cosigners_match_path"
        assert not _last_step(body)["ok"]

    def test_cosigners_do_not_match_path(self) -> None:
        client, keys, _ = _client_with()
        sla = build_signed_sla(keys)
        # Reorder the path without matching cosigners — canonical hash stays
        # valid because we re-compute it, but the check still fires.
        swapped_path = [sla.payload.path[0], sla.payload.path[2], sla.payload.path[1]]
        new_payload = replace(sla.payload, sla_id="", path=swapped_path)
        new_payload = replace(new_payload, sla_id=compute_sla_id(new_payload))
        pbytes = canonical_json(asdict(new_payload))
        sigs = [keys[c].sign(pbytes) for c in new_payload.cosigners]
        bad = SignedSLA(payload=new_payload, signatures=sigs)
        r = _submit(client, bad)
        assert r.status_code == 400
        body = r.json()
        assert _last_step(body)["step"] == "cosigners_match_path"
        assert not _last_step(body)["ok"]

    def test_signature_count_wrong(self) -> None:
        client, keys, _ = _client_with()
        sla = build_signed_sla(keys)
        too_few = SignedSLA(payload=sla.payload, signatures=sla.signatures[:-1])
        r = _submit(client, too_few)
        assert r.status_code == 400
        body = r.json()
        assert _last_step(body)["step"] == "signature_count"
        assert not _last_step(body)["ok"]

    def test_signature_duplicate_cosigner_rejected(self) -> None:
        """Right count, but two signatures from the same AS — step 7 catches it."""
        client, keys, _ = _client_with()
        sla = build_signed_sla(keys)
        dup_sigs = [sla.signatures[0], sla.signatures[0], sla.signatures[2]]
        bad = SignedSLA(payload=sla.payload, signatures=dup_sigs)
        r = _submit(client, bad)
        assert r.status_code == 400
        body = r.json()
        assert _last_step(body)["step"] == "signature_count"
        assert not _last_step(body)["ok"]

    def test_signature_verify_fails_when_sig_bytes_tampered(self) -> None:
        client, keys, _ = _client_with()
        sla = build_signed_sla(keys)
        good = sla.signatures[1]
        bad_sig = Signature(
            meta=good.meta,
            sig_bytes=good.sig_bytes[:-1] + bytes([good.sig_bytes[-1] ^ 0xFF]),
        )
        tampered = SignedSLA(
            payload=sla.payload,
            signatures=[sla.signatures[0], bad_sig, sla.signatures[2]],
        )
        r = _submit(client, tampered)
        assert r.status_code == 400
        body = r.json()
        assert _last_step(body)["step"] == "signature_verify"
        assert not _last_step(body)["ok"]

    def test_signature_from_unknown_as_rejected(self) -> None:
        """Cosigner exists, signature is labelled for that AS, but keyring lacks the key."""
        client, keys, _ = _client_with(keys_ases=THREE_ASES[:2])  # drop key for THREE_ASES[2]
        # build_signed_sla needs real keys for every cosigner to sign, so generate
        # a throwaway key for the unknown AS and use it to sign. The verifier
        # lookup will fail because that AS isn't in the keyring.
        from tests.fixtures.crypto import make_test_key
        keys[THREE_ASES[2]] = make_test_key(THREE_ASES[2])
        sla = build_signed_sla(keys, cosigners=THREE_ASES)
        r = _submit(client, sla)
        assert r.status_code == 400
        body = r.json()
        assert _last_step(body)["step"] == "signature_verify"
        assert not _last_step(body)["ok"]

    def test_duplicate_sla_rejected_on_second_submission(self) -> None:
        client, keys, _ = _client_with()
        sla = build_signed_sla(keys)
        assert _submit(client, sla).status_code == 201
        r = _submit(client, sla)
        assert r.status_code == 400
        body = r.json()
        assert _last_step(body)["step"] == "no_duplicate"
        assert not _last_step(body)["ok"]


class TestVerificationStepOrdering:
    def test_first_failing_step_short_circuits_rest(self) -> None:
        """If schema_version fails, no subsequent step should appear."""
        client, keys, _ = _client_with()
        sla = build_signed_sla(keys)
        bad_payload = replace(sla.payload, sla_id="", schema_version=3)
        bad_payload = replace(bad_payload, sla_id=compute_sla_id(bad_payload))
        pbytes = canonical_json(asdict(bad_payload))
        sigs = [keys[c].sign(pbytes) for c in bad_payload.cosigners]
        bad = SignedSLA(payload=bad_payload, signatures=sigs)
        r = _submit(client, bad)
        body = r.json()
        names = _step_names(body)
        # parse_json passed; schema_version is last (failed); nothing after it.
        assert names == ["parse_json", "schema_version"]
