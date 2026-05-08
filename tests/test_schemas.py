"""Unit tests for schemas.py.

Covers: frozen-ness, the `replace(payload, id_field="")` pattern required by
§4.6, and that `dataclasses.asdict` round-trips through ``canonical_json``
cleanly for each signed-payload type.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, asdict

import pytest

from pathmarket.canonical import canonical_json
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
from tests.fixtures.builders import (
    make_claim_payload,
    make_complaint_payload,
    make_sla_payload,
)


class TestFrozen:
    """Every signed-payload dataclass must be frozen (§4 opening rule)."""

    def test_sla_payload_is_frozen(self) -> None:
        p = make_sla_payload()
        with pytest.raises(FrozenInstanceError):
            p.nonce = "other"  # type: ignore[misc]

    def test_claim_payload_is_frozen(self) -> None:
        p = make_claim_payload()
        with pytest.raises(FrozenInstanceError):
            p.gb_purchased = 1  # type: ignore[misc]

    def test_complaint_payload_is_frozen(self) -> None:
        p = make_complaint_payload()
        with pytest.raises(FrozenInstanceError):
            p.measured_value = 0  # type: ignore[misc]

    def test_path_hop_is_frozen(self) -> None:
        h = PathHop(isd_as="1-ff00:0:110", ingress=0, egress=1)
        with pytest.raises(FrozenInstanceError):
            h.ingress = 2  # type: ignore[misc]

    def test_policy_is_frozen(self) -> None:
        p = Policy(
            max_price_per_gb="0.08",
            min_reputation_floor=0.8,
            required_bounds=SLABounds(latency_max_ms=20, loss_max_ppm=None, bandwidth_min_kbps=None),
            alpha=1.5,
            beta=5.0,
            uncovered_tolerance="partial_ok",
            complaint_sensitivity="moderate",
            reshop_on_reputation_drop=0.15,
            portfolio_redundancy=1,
        )
        with pytest.raises(FrozenInstanceError):
            p.alpha = 2.0  # type: ignore[misc]


class TestReplaceEmptyIdPattern:
    """§4.6 requires `replace(payload, id_field="")` on frozen dataclasses."""

    def test_sla_payload_replace_sla_id(self) -> None:
        from dataclasses import replace

        p = make_sla_payload(sla_id="sha256:previous")
        p2 = replace(p, sla_id="")
        assert p2.sla_id == ""
        # Everything else is preserved.
        assert p2.price_per_gb == p.price_per_gb
        assert p2.path == p.path

    def test_claim_payload_replace_claim_id(self) -> None:
        from dataclasses import replace

        p = make_claim_payload(claim_id="sha256:previous")
        p2 = replace(p, claim_id="")
        assert p2.claim_id == ""
        assert p2.claimant == p.claimant

    def test_complaint_payload_replace_complaint_id(self) -> None:
        from dataclasses import replace

        p = make_complaint_payload(complaint_id="sha256:previous")
        p2 = replace(p, complaint_id="")
        assert p2.complaint_id == ""
        assert p2.note == p.note


class TestAsdictRoundTrip:
    """asdict → canonical_json → parse JSON → compare dict round-trip.

    Signed payloads never contain bytes fields (§4.5), so ``asdict`` should be
    lossless under canonical JSON.
    """

    def test_sla_payload_round_trip(self) -> None:
        p = make_sla_payload(sla_id="sha256:test")
        d1 = asdict(p)
        d2 = json.loads(canonical_json(d1))
        assert d1 == d2

    def test_claim_payload_round_trip(self) -> None:
        p = make_claim_payload(claim_id="sha256:test")
        d1 = asdict(p)
        d2 = json.loads(canonical_json(d1))
        assert d1 == d2

    def test_complaint_payload_round_trip_with_attachment(self) -> None:
        att = Attachment(kind="text", filename="t.txt", content_b64="aGVsbG8=")
        p = make_complaint_payload(complaint_id="sha256:test", attachments=[att])
        d1 = asdict(p)
        d2 = json.loads(canonical_json(d1))
        assert d1 == d2


class TestSignedContainers:
    """Sanity check for the outer signed dataclasses — they hold a payload plus signature(s)."""

    def _sig(self, who: str) -> Signature:
        return Signature(
            meta=SignatureMeta(isd_as=who, key_id="default", trc_serial=0, trc_base=0),
            sig_bytes=b"\x00" * 64,
        )

    def test_signed_sla_holds_list_of_signatures(self) -> None:
        p = make_sla_payload(sla_id="sha256:x")
        s = SignedSLA(payload=p, signatures=[self._sig("1-ff00:0:112"), self._sig("1-ff00:0:111")])
        assert len(s.signatures) == 2
        assert s.signatures[0].meta.isd_as == "1-ff00:0:112"

    def test_signed_claim_holds_single_signature(self) -> None:
        p = make_claim_payload(claim_id="sha256:x")
        s = SignedClaim(payload=p, signature=self._sig("1-ff00:0:130"))
        assert s.signature.meta.isd_as == "1-ff00:0:130"

    def test_signed_complaint_holds_single_signature(self) -> None:
        p = make_complaint_payload(complaint_id="sha256:x")
        s = SignedComplaint(payload=p, signature=self._sig("1-ff00:0:221"))
        assert s.signature.meta.isd_as == "1-ff00:0:221"


class TestRoutingDecisionShape:
    """RoutingDecision is the one v2-only persistent-state dataclass we must get right."""

    def test_applied_claims_is_dict_of_hop_index_to_claim_id(self) -> None:
        rd = RoutingDecision(
            dst_isd_as="1-ff00:0:130",
            path=[
                PathHop(isd_as="1-ff00:0:112", ingress=0, egress=495),
                PathHop(isd_as="1-ff00:0:111", ingress=113, egress=104),
                PathHop(isd_as="1-ff00:0:130", ingress=2, egress=0),
            ],
            applied_claims={0: "sha256:a", 1: "sha256:b"},
            updated_at="2026-04-19T12:00:00Z",
            manual_override=False,
        )
        assert rd.applied_claims[0] == "sha256:a"
        # Uncovered hops are omitted from the mapping, not stored as None.
        assert 2 not in rd.applied_claims
