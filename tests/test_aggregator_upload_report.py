"""Step B2 — verification-report shape tests (``DESIGN.md`` §5.2).

The UI renders the verification report verbatim, so its shape is part of the
contract. These tests assert the exact keys, types, and ordering of the report.
"""

from __future__ import annotations

from dataclasses import asdict, replace

from fastapi.testclient import TestClient

from pathmarket.aggregator.app import create_app
from pathmarket.aggregator.models import SignedSLAModel
from pathmarket.aggregator.storage import AggregatorStorage
from pathmarket.canonical import canonical_json, compute_sla_id
from pathmarket.schemas import SignedSLA

from tests.fixtures.signing import build_signed_sla, make_keys_for, make_verifier


THREE_ASES = ["1-ff00:0:112", "1-ff00:0:111", "1-ff00:0:122"]


def _client():
    keys = make_keys_for(THREE_ASES)
    app = create_app(storage=AggregatorStorage(), verifier=make_verifier(keys))
    return TestClient(app), keys


def _submit(client: TestClient, sla: SignedSLA):
    return client.post("/sla", json=SignedSLAModel.from_dataclass(sla).model_dump())


def _step_shape_ok(step: dict) -> None:
    """Each verification step must be exactly ``{step, ok, detail}``."""
    assert set(step.keys()) == {"step", "ok", "detail"}
    assert isinstance(step["step"], str)
    assert isinstance(step["ok"], bool)
    assert step["detail"] is None or isinstance(step["detail"], str)


class TestAcceptedShape:
    def test_top_level_keys(self) -> None:
        client, keys = _client()
        r = _submit(client, build_signed_sla(keys))
        assert r.status_code == 201
        body = r.json()
        assert set(body.keys()) == {"status", "sla_id", "verification_steps"}
        assert body["status"] == "accepted"
        assert body["sla_id"].startswith("sha256:")

    def test_verification_steps_expected_fixture(self) -> None:
        """For a 3-cosigner SLA, the report is exactly these 11 steps in order."""
        client, keys = _client()
        sla = build_signed_sla(keys, cosigners=THREE_ASES)
        r = _submit(client, sla)
        assert r.status_code == 201
        steps = r.json()["verification_steps"]

        expected = [
            {"step": "parse_json", "ok": True, "detail": None},
            {"step": "schema_version", "ok": True, "detail": "version 2"},
            {"step": "content_hash", "ok": True, "detail": "sla_id matches"},
            {"step": "validity_window", "ok": True, "detail": "valid for 24h"},
            {"step": "bounds_nonempty", "ok": True, "detail": "latency_max_ms=10"},
            {"step": "cosigners_match_path", "ok": True, "detail": "3 ASes"},
            {"step": "signature_count", "ok": True, "detail": "3 of 3"},
            {"step": "signature_verify", "ok": True, "detail": THREE_ASES[0]},
            {"step": "signature_verify", "ok": True, "detail": THREE_ASES[1]},
            {"step": "signature_verify", "ok": True, "detail": THREE_ASES[2]},
            {"step": "no_duplicate", "ok": True, "detail": "new sla_id"},
        ]
        assert steps == expected

    def test_every_step_has_expected_shape(self) -> None:
        client, keys = _client()
        r = _submit(client, build_signed_sla(keys))
        for step in r.json()["verification_steps"]:
            _step_shape_ok(step)


class TestRejectedShape:
    def test_top_level_keys(self) -> None:
        client, keys = _client()
        sla = build_signed_sla(keys)
        tampered = SignedSLA(
            payload=replace(sla.payload, sla_id="sha256:" + "f" * 64),
            signatures=sla.signatures,
        )
        r = _submit(client, tampered)
        assert r.status_code == 400
        body = r.json()
        assert set(body.keys()) == {"status", "sla_id", "verification_steps"}
        assert body["status"] == "rejected"
        # sla_id is None on rejection.
        assert body["sla_id"] is None

    def test_failing_step_is_last_and_not_ok(self) -> None:
        client, keys = _client()
        sla = build_signed_sla(keys)
        bad_payload = replace(sla.payload, sla_id="", schema_version=9)
        bad_payload = replace(bad_payload, sla_id=compute_sla_id(bad_payload))
        pbytes = canonical_json(asdict(bad_payload))
        sigs = [keys[c].sign(pbytes) for c in bad_payload.cosigners]
        bad = SignedSLA(payload=bad_payload, signatures=sigs)
        r = _submit(client, bad)
        body = r.json()
        steps = body["verification_steps"]
        assert steps[-1]["ok"] is False
        # All prior steps passed; only the last failed.
        assert all(s["ok"] for s in steps[:-1])

    def test_every_step_has_expected_shape(self) -> None:
        client, keys = _client()
        # Trigger a no_duplicate rejection — exercises a long step list on failure.
        sla = build_signed_sla(keys)
        _submit(client, sla)
        r = _submit(client, sla)
        for step in r.json()["verification_steps"]:
            _step_shape_ok(step)
