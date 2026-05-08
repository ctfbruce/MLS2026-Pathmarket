"""Unit tests for canonical.py.

Covers the three requirements from §4.5 and §4.6:
1. Canonical JSON is deterministic and produces the exact bytes specified
   (sort_keys, compact separators, ASCII).
2. The "hash-with-empty-self" pattern works for all three ID computations.
3. Hashes are deterministic AND sensitive to any field change.
"""

from __future__ import annotations

from dataclasses import asdict, replace

import pytest

from pathmarket.canonical import (
    canonical_json,
    compute_claim_id,
    compute_complaint_id,
    compute_sla_id,
    iso8601_utc_now,
)
from pathmarket.schemas import Attachment, SLABounds
from tests.fixtures.builders import (
    make_claim_payload,
    make_complaint_payload,
    make_sla_payload,
)


class TestCanonicalJSON:
    def test_sorts_keys(self) -> None:
        a = canonical_json({"b": 1, "a": 2})
        b = canonical_json({"a": 2, "b": 1})
        assert a == b
        assert a == b'{"a":2,"b":1}'

    def test_compact_separators(self) -> None:
        # No whitespace around commas or colons.
        out = canonical_json({"a": 1, "b": [1, 2]})
        assert b" " not in out
        assert out == b'{"a":1,"b":[1,2]}'

    def test_ascii_escapes_non_ascii(self) -> None:
        # ensure_ascii=True → non-ASCII becomes \uXXXX.
        out = canonical_json({"note": "café"})
        assert b"caf\\u00e9" in out
        assert b"\xc3\xa9" not in out

    def test_nested_dict_and_list_sort(self) -> None:
        payload = {
            "z": 1,
            "a": {"y": 2, "x": 1},
            "m": [{"q": 1, "p": 2}, {"q": 3, "p": 4}],
        }
        out = canonical_json(payload)
        # top-level keys ordered a, m, z; inner dicts ordered x, y / p, q.
        assert out == b'{"a":{"x":1,"y":2},"m":[{"p":2,"q":1},{"p":4,"q":3}],"z":1}'


class TestSLAIdComputation:
    def test_is_sha256_prefixed_hex(self) -> None:
        p = make_sla_payload()
        sid = compute_sla_id(p)
        assert sid.startswith("sha256:")
        hex_part = sid[len("sha256:") :]
        assert len(hex_part) == 64
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_is_deterministic(self) -> None:
        p = make_sla_payload()
        assert compute_sla_id(p) == compute_sla_id(p)

    def test_ignores_caller_supplied_sla_id(self) -> None:
        """A non-empty sla_id on the input must be cleared before hashing."""
        p1 = make_sla_payload(sla_id="")
        p2 = make_sla_payload(sla_id="sha256:bogus")
        assert compute_sla_id(p1) == compute_sla_id(p2)

    def test_sensitive_to_every_field(self) -> None:
        """Changing any field changes the hash."""
        base = make_sla_payload()
        base_id = compute_sla_id(base)
        # price
        assert compute_sla_id(replace(base, price_per_gb="0.029")) != base_id
        # nonce
        assert compute_sla_id(replace(base, nonce="0" * 32)) != base_id
        # valid_from / valid_until
        assert compute_sla_id(replace(base, valid_from="2026-04-19T13:00:00Z")) != base_id
        assert compute_sla_id(replace(base, valid_until="2026-04-21T12:00:00Z")) != base_id
        # cosigners / path (shape-preserving swap)
        swapped_cos = replace(base, cosigners=list(reversed(base.cosigners)))
        assert compute_sla_id(swapped_cos) != base_id
        # schema_version
        assert compute_sla_id(replace(base, schema_version=1)) != base_id
        # bounds
        assert (
            compute_sla_id(
                replace(
                    base,
                    bounds=SLABounds(latency_max_ms=11, loss_max_ppm=500, bandwidth_min_kbps=None),
                )
            )
            != base_id
        )

    def test_roundtrip_set_id_then_reverify(self) -> None:
        """Submitter flow: set id to "", compute, write back, aggregator re-checks."""
        p0 = make_sla_payload(sla_id="")
        sid = compute_sla_id(p0)
        p1 = replace(p0, sla_id=sid)
        # Aggregator re-runs compute_sla_id, which strips sla_id back to "".
        assert compute_sla_id(p1) == sid


class TestClaimIdComputation:
    def test_is_deterministic(self) -> None:
        p = make_claim_payload()
        assert compute_claim_id(p) == compute_claim_id(p)

    def test_ignores_caller_supplied_claim_id(self) -> None:
        p1 = make_claim_payload(claim_id="")
        p2 = make_claim_payload(claim_id="sha256:bogus")
        assert compute_claim_id(p1) == compute_claim_id(p2)

    def test_sensitive_to_gb_and_price(self) -> None:
        base = make_claim_payload()
        base_id = compute_claim_id(base)
        assert compute_claim_id(replace(base, gb_purchased=501)) != base_id
        assert compute_claim_id(replace(base, price_paid_chf="14.01")) != base_id
        assert compute_claim_id(replace(base, claimant="1-ff00:0:999")) != base_id
        assert compute_claim_id(replace(base, sla_id="sha256:other")) != base_id
        assert compute_claim_id(replace(base, claimed_at="2026-04-19T14:00:00Z")) != base_id


class TestComplaintIdComputation:
    def test_is_deterministic(self) -> None:
        p = make_complaint_payload()
        assert compute_complaint_id(p) == compute_complaint_id(p)

    def test_note_participates_in_hash(self) -> None:
        """Note is part of the signed payload (v2 amendment); the hash must depend on it."""
        p0 = make_complaint_payload(note="")
        p1 = make_complaint_payload(note="Observed latency excursion")
        assert compute_complaint_id(p0) != compute_complaint_id(p1)

    def test_attachments_participate_in_hash(self) -> None:
        """Attachments are part of the signed payload; the hash must depend on them."""
        base = make_complaint_payload(attachments=[])
        with_att = make_complaint_payload(
            attachments=[Attachment(kind="text", filename="a.txt", content_b64="aGVsbG8=")]
        )
        assert compute_complaint_id(base) != compute_complaint_id(with_att)

        # Changing the attachment's content changes the hash.
        alt_att = make_complaint_payload(
            attachments=[Attachment(kind="text", filename="a.txt", content_b64="Ynll")]
        )
        assert compute_complaint_id(with_att) != compute_complaint_id(alt_att)


class TestISO8601Helper:
    def test_has_trailing_z_and_no_fractional_seconds(self) -> None:
        ts = iso8601_utc_now()
        assert ts.endswith("Z")
        assert "." not in ts
        # Format: YYYY-MM-DDTHH:MM:SSZ — exactly 20 chars.
        assert len(ts) == 20
