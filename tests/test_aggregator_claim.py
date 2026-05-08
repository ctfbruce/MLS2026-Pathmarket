"""Step B3 — ``POST /claim`` tests (``DESIGN.md`` §5.2).

Happy path plus one failure test per validation step. Response shape is
``{"claim_id": ..., "status": "accepted"}`` on 201 and ``{"error": "<reason>"}``
on 400 (distinct from the SLA verification-report shape).
"""

from __future__ import annotations

from dataclasses import asdict, replace

from fastapi.testclient import TestClient

from pathmarket.aggregator.app import create_app
from pathmarket.aggregator.models import SignedClaimModel, SignedSLAModel
from pathmarket.aggregator.storage import AggregatorStorage
from pathmarket.canonical import canonical_json, compute_claim_id
from pathmarket.schemas import (
    Signature,
    SignatureMeta,
    SignedClaim,
)

from tests.fixtures.builders import make_claim_payload
from tests.fixtures.crypto import make_test_key
from tests.fixtures.signing import build_signed_sla, make_keys_for, make_verifier


COSIGNERS = ["1-ff00:0:112", "1-ff00:0:111", "1-ff00:0:122"]
CLAIMANT = "1-ff00:0:130"


def _fresh_setup():
    """Return (client, storage, cosigner_keys, claimant_key, posted_sla)."""
    all_keys = make_keys_for(COSIGNERS + [CLAIMANT])
    storage = AggregatorStorage()
    verifier = make_verifier(all_keys)
    app = create_app(storage=storage, verifier=verifier)
    client = TestClient(app)

    cosigner_keys = {a: all_keys[a] for a in COSIGNERS}
    sla = build_signed_sla(
        cosigner_keys,
        cosigners=COSIGNERS,
        price_per_gb="0.02",  # cheap round number for decimal arithmetic.
    )
    r = client.post("/sla", json=SignedSLAModel.from_dataclass(sla).model_dump())
    assert r.status_code == 201
    return client, storage, cosigner_keys, all_keys[CLAIMANT], sla


def _build_claim(
    sla, claimant_key, *, gb_purchased: int = 500, price_paid_chf: str | None = None
) -> SignedClaim:
    if price_paid_chf is None:
        from decimal import Decimal

        price_paid_chf = str(Decimal(sla.payload.price_per_gb) * Decimal(gb_purchased))
    payload = make_claim_payload(
        sla_id=sla.payload.sla_id,
        claimant=claimant_key.isd_as,
        gb_purchased=gb_purchased,
        price_paid_chf=price_paid_chf,
    )
    payload = replace(payload, claim_id=compute_claim_id(payload))
    sig = claimant_key.sign(canonical_json(asdict(payload)))
    return SignedClaim(payload=payload, signature=sig)


def _submit_claim(client, claim: SignedClaim):
    return client.post("/claim", json=SignedClaimModel.from_dataclass(claim).model_dump())


class TestHappyPath:
    def test_accepted_claim_appears_in_storage_and_ticker(self) -> None:
        client, storage, _, claimant_key, sla = _fresh_setup()
        claim = _build_claim(sla, claimant_key, gb_purchased=500)
        r = _submit_claim(client, claim)
        assert r.status_code == 201
        body = r.json()
        assert body == {"claim_id": claim.payload.claim_id, "status": "accepted"}
        assert len(storage.claims) == 1
        ingested = storage.claims[0][1]
        assert ingested.payload.claim_id == claim.payload.claim_id
        # Two ticker events: one from POST /sla, one from POST /claim.
        assert [e.type for e in storage.ticker_events] == ["sla_signed", "claim"]


class TestClaimValidationFailures:
    def test_parse_fails_on_bad_json(self) -> None:
        client, _, _, _, _ = _fresh_setup()
        r = client.post(
            "/claim", content="{bad", headers={"content-type": "application/json"}
        )
        assert r.status_code == 400
        assert r.json()["error"].startswith("parse_json:")

    def test_parse_fails_on_missing_fields(self) -> None:
        client, _, _, _, _ = _fresh_setup()
        r = client.post("/claim", json={"payload": {}, "signature": {}})
        assert r.status_code == 400
        assert r.json()["error"].startswith("parse_json:")

    def test_wrong_schema_version(self) -> None:
        client, _, _, claimant_key, sla = _fresh_setup()
        claim = _build_claim(sla, claimant_key)
        bad_payload = replace(claim.payload, claim_id="", schema_version=1)
        bad_payload = replace(bad_payload, claim_id=compute_claim_id(bad_payload))
        sig = claimant_key.sign(canonical_json(asdict(bad_payload)))
        bad = SignedClaim(payload=bad_payload, signature=sig)
        r = _submit_claim(client, bad)
        assert r.status_code == 400
        assert r.json()["error"].startswith("schema_version:")

    def test_content_hash_mismatch(self) -> None:
        client, _, _, claimant_key, sla = _fresh_setup()
        claim = _build_claim(sla, claimant_key)
        tampered = SignedClaim(
            payload=replace(claim.payload, claim_id="sha256:" + "0" * 64),
            signature=claim.signature,
        )
        r = _submit_claim(client, tampered)
        assert r.status_code == 400
        assert r.json()["error"].startswith("content_hash:")

    def test_unknown_sla_rejected(self) -> None:
        client, _, _, claimant_key, sla = _fresh_setup()
        # Reference an SLA id that doesn't exist.
        payload = make_claim_payload(
            sla_id="sha256:" + "a" * 64,
            claimant=claimant_key.isd_as,
            gb_purchased=100,
            price_paid_chf="2.00",
        )
        payload = replace(payload, claim_id=compute_claim_id(payload))
        sig = claimant_key.sign(canonical_json(asdict(payload)))
        claim = SignedClaim(payload=payload, signature=sig)
        r = _submit_claim(client, claim)
        assert r.status_code == 400
        assert r.json()["error"].startswith("unknown_sla:")

    def test_expired_sla_rejected(self) -> None:
        """Build an SLA valid only in the past; its claim should be rejected."""
        all_keys = make_keys_for(COSIGNERS + [CLAIMANT])
        storage = AggregatorStorage()
        app = create_app(storage=storage, verifier=make_verifier(all_keys))
        client = TestClient(app)
        cosigner_keys = {a: all_keys[a] for a in COSIGNERS}
        sla = build_signed_sla(
            cosigner_keys,
            cosigners=COSIGNERS,
            valid_from="2024-01-01T00:00:00Z",
            valid_until="2024-01-02T00:00:00Z",  # long past.
        )
        r = client.post("/sla", json=SignedSLAModel.from_dataclass(sla).model_dump())
        assert r.status_code == 201

        claim = _build_claim(sla, all_keys[CLAIMANT])
        r = _submit_claim(client, claim)
        assert r.status_code == 400
        assert r.json()["error"].startswith("sla_expired:")

    def test_self_dealing_rejected(self) -> None:
        client, _, cosigner_keys, _, sla = _fresh_setup()
        # A non-source cosigner tries to claim its own SLA → self-dealing.
        claimant_key = cosigner_keys[COSIGNERS[1]]
        claim = _build_claim(sla, claimant_key, gb_purchased=100, price_paid_chf="2.00")
        r = _submit_claim(client, claim)
        assert r.status_code == 400
        assert r.json()["error"].startswith("self_dealing:")

    def test_source_cosigner_can_claim(self) -> None:
        # The first cosigner (path source AS) is allowed to claim — it's the
        # natural buyer of an SLA covering its outbound egress.
        client, _, cosigner_keys, _, sla = _fresh_setup()
        claimant_key = cosigner_keys[COSIGNERS[0]]
        claim = _build_claim(sla, claimant_key, gb_purchased=100, price_paid_chf="2.00")
        r = _submit_claim(client, claim)
        assert r.status_code == 201

    def test_gb_purchased_zero_rejected(self) -> None:
        client, _, _, claimant_key, sla = _fresh_setup()
        claim = _build_claim(sla, claimant_key, gb_purchased=0, price_paid_chf="0")
        r = _submit_claim(client, claim)
        assert r.status_code == 400
        assert r.json()["error"].startswith("gb_purchased:")

    def test_price_arithmetic_mismatch(self) -> None:
        client, _, _, claimant_key, sla = _fresh_setup()
        # 0.02 * 500 = 10.00, paid 9.99.
        claim = _build_claim(sla, claimant_key, gb_purchased=500, price_paid_chf="9.99")
        r = _submit_claim(client, claim)
        assert r.status_code == 400
        assert r.json()["error"].startswith("price_arithmetic:")

    def test_price_arithmetic_accepts_alternative_decimal_shapes(self) -> None:
        """Per 2026-04-19 amendment: value equality via Decimal.

        0.02 * 500 = 10, and ``10`` / ``10.0`` / ``10.00`` are all accepted.
        The claim_id already pins the exact string via the content hash.
        """
        client, _, _, claimant_key, sla = _fresh_setup()
        for shape in ("10", "10.0", "10.00", "1.0E+1"):
            claim = _build_claim(sla, claimant_key, gb_purchased=500, price_paid_chf=shape)
            # Every submission has a unique nonce via claimed_at default; but
            # claimed_at is fixed in builder, so later submissions would be
            # duplicates. Rebuild with a unique ``nonce``-like tweak by using
            # a tweaked claimed_at.
            p = replace(claim.payload, claim_id="", claimed_at=f"2026-04-19T13:{shape.replace('.', '-').replace('+', 'x')}:00Z")
            # Not all those tweaks produce valid ISO strings, so just skip the
            # dedup concern by resetting storage via a fresh client per shape.
            client2, _, _, ck2, sla2 = _fresh_setup()
            claim2 = _build_claim(sla2, ck2, gb_purchased=500, price_paid_chf=shape)
            r = _submit_claim(client2, claim2)
            assert r.status_code == 201, f"shape {shape!r} rejected: {r.json()}"

    def test_signature_meta_mismatch(self) -> None:
        """meta.isd_as differs from payload.claimant."""
        client, _, _, claimant_key, sla = _fresh_setup()
        claim = _build_claim(sla, claimant_key)
        wrong_meta = replace(claim.signature.meta, isd_as="1-ff00:0:999")
        tampered = SignedClaim(
            payload=claim.payload,
            signature=Signature(meta=wrong_meta, sig_bytes=claim.signature.sig_bytes),
        )
        r = _submit_claim(client, tampered)
        assert r.status_code == 400
        assert r.json()["error"].startswith("signature_meta:")

    def test_signature_verify_fails_on_tampered_sig_bytes(self) -> None:
        client, _, _, claimant_key, sla = _fresh_setup()
        claim = _build_claim(sla, claimant_key)
        # Flip (not zero) the last byte so tampering is guaranteed non-identity.
        last = claim.signature.sig_bytes[-1]
        bad_sig = Signature(
            meta=claim.signature.meta,
            sig_bytes=claim.signature.sig_bytes[:-1] + bytes([last ^ 0xFF]),
        )
        tampered = SignedClaim(payload=claim.payload, signature=bad_sig)
        r = _submit_claim(client, tampered)
        assert r.status_code == 400
        assert r.json()["error"].startswith("signature_verify:")

    def test_signature_verify_fails_on_unknown_claimant_key(self) -> None:
        """Claimant signs with a key the verifier doesn't know."""
        # Build a setup where the keyring is missing the claimant entirely.
        cosigner_keys = make_keys_for(COSIGNERS)
        storage = AggregatorStorage()
        app = create_app(
            storage=storage, verifier=make_verifier(cosigner_keys)
        )  # no claimant key in keyring
        client = TestClient(app)
        sla = build_signed_sla(cosigner_keys, cosigners=COSIGNERS, price_per_gb="0.02")
        assert client.post(
            "/sla", json=SignedSLAModel.from_dataclass(sla).model_dump()
        ).status_code == 201

        stray = make_test_key(CLAIMANT)
        claim = _build_claim(sla, stray)
        r = _submit_claim(client, claim)
        assert r.status_code == 400
        assert r.json()["error"].startswith("signature_verify:")
