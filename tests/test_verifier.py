"""Tests for StaticKeyVerifier.

Matrix covered (§4, §15 security properties):
- Valid signature by matching key → verifies.
- Tampered payload bytes → rejected.
- Tampered signature bytes → rejected.
- Unknown ISD-AS (not in keyring) → rejected.
- Unknown key_id for a known AS → rejected.
- Signature by one AS with another AS's identity → rejected (key lookup fails).
- ``known_ases()`` lists every keyring entry, sorted.
- Round-trip through a JSON file on disk.
- End-to-end: ``scripts/generate_keys.py`` → ``scripts/generate_keyring.py`` →
  ``StaticKeyVerifier.from_file`` accepts real signatures (§10.3, Step A7).
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pathmarket.schemas import Signature, SignatureMeta
from pathmarket.verifier import StaticKeyVerifier
from tests.fixtures.crypto import keyring_from_test_keys, make_test_key


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str) -> ModuleType:
    """Import ``scripts/<name>.py`` as a module without polluting sys.path."""
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"pathmarket_scripts.{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sample_payload() -> bytes:
    # Any deterministic byte string; verification is agnostic to content.
    return b'{"example":"payload","schema_version":2}'


@pytest.fixture
def keys_and_verifier() -> tuple[dict, StaticKeyVerifier]:
    k_110 = make_test_key("1-ff00:0:110")
    k_111 = make_test_key("1-ff00:0:111")
    verifier = StaticKeyVerifier(keyring_from_test_keys([k_110, k_111]))
    return {"k_110": k_110, "k_111": k_111}, verifier


class TestVerify:
    def test_valid_signature_verifies(self, keys_and_verifier, sample_payload: bytes) -> None:
        keys, v = keys_and_verifier
        sig = keys["k_110"].sign(sample_payload)
        assert v.verify(signature=sig, payload_bytes=sample_payload) is True

    def test_tampered_payload_rejected(self, keys_and_verifier, sample_payload: bytes) -> None:
        keys, v = keys_and_verifier
        sig = keys["k_110"].sign(sample_payload)
        assert v.verify(signature=sig, payload_bytes=sample_payload + b"x") is False

    def test_tampered_signature_bytes_rejected(
        self, keys_and_verifier, sample_payload: bytes
    ) -> None:
        keys, v = keys_and_verifier
        sig = keys["k_110"].sign(sample_payload)
        tampered = Signature(
            meta=sig.meta,
            sig_bytes=sig.sig_bytes[:-1] + bytes([sig.sig_bytes[-1] ^ 0xFF]),
        )
        assert v.verify(signature=tampered, payload_bytes=sample_payload) is False

    def test_unknown_isd_as_rejected(self, keys_and_verifier, sample_payload: bytes) -> None:
        """Signature labels itself as from an AS whose key isn't in the keyring."""
        keys, v = keys_and_verifier
        real_sig = keys["k_110"].sign(sample_payload)
        relabeled = Signature(
            meta=SignatureMeta(
                isd_as="1-ff00:0:999",  # unknown
                key_id="default",
                trc_serial=0,
                trc_base=0,
            ),
            sig_bytes=real_sig.sig_bytes,
        )
        assert v.verify(signature=relabeled, payload_bytes=sample_payload) is False

    def test_unknown_key_id_rejected(self, keys_and_verifier, sample_payload: bytes) -> None:
        keys, v = keys_and_verifier
        real_sig = keys["k_110"].sign(sample_payload)
        wrong_kid = Signature(
            meta=SignatureMeta(
                isd_as="1-ff00:0:110",
                key_id="rotated-2030",  # no such key in keyring
                trc_serial=0,
                trc_base=0,
            ),
            sig_bytes=real_sig.sig_bytes,
        )
        assert v.verify(signature=wrong_kid, payload_bytes=sample_payload) is False

    def test_signature_from_wrong_as_rejected(
        self, keys_and_verifier, sample_payload: bytes
    ) -> None:
        """k_110 signs, but the Signature's meta claims it's from 1-ff00:0:111."""
        keys, v = keys_and_verifier
        real_sig = keys["k_110"].sign(sample_payload)
        mis_labeled = Signature(
            meta=SignatureMeta(
                isd_as="1-ff00:0:111",
                key_id="default",
                trc_serial=0,
                trc_base=0,
            ),
            sig_bytes=real_sig.sig_bytes,
        )
        # Verifier looks up 1-ff00:0:111's key and tries to verify with it; fails.
        assert v.verify(signature=mis_labeled, payload_bytes=sample_payload) is False


class TestKnownAses:
    def test_lists_all_ases_sorted(self, keys_and_verifier) -> None:
        _, v = keys_and_verifier
        assert v.known_ases() == ["1-ff00:0:110", "1-ff00:0:111"]

    def test_empty_keyring_yields_empty_list(self) -> None:
        v = StaticKeyVerifier(keyring={})
        assert v.known_ases() == []


class TestFromFile:
    def test_roundtrip_via_json_file(self, tmp_path: Path, sample_payload: bytes) -> None:
        k_110 = make_test_key("1-ff00:0:110")
        # Write the same JSON shape `scripts/generate_keyring.py` will produce.
        keyring_path = tmp_path / "keyring.json"
        keyring_path.write_text(
            json.dumps(
                {
                    k_110.isd_as: {
                        "default": base64.b64encode(k_110.public_bytes).decode("ascii")
                    }
                }
            ),
            encoding="utf-8",
        )
        v = StaticKeyVerifier.from_file(keyring_path)
        sig = k_110.sign(sample_payload)
        assert v.verify(signature=sig, payload_bytes=sample_payload) is True
        assert v.known_ases() == ["1-ff00:0:110"]


class TestKeyScriptsIntegration:
    """End-to-end: generate_keys → generate_keyring → StaticKeyVerifier."""

    def test_keypair_and_keyring_verify_real_signature(
        self, tmp_path: Path, sample_payload: bytes
    ) -> None:
        gen_keys = _load_script("generate_keys")
        gen_keyring = _load_script("generate_keyring")

        keys_dir = tmp_path / "keys"
        isd_as = "1-ff00:0:221"

        priv_path, pub_path, created = gen_keys.generate_keypair(isd_as, keys_dir)
        assert created is True
        assert priv_path.read_bytes().__len__() == 32
        assert pub_path.read_bytes().__len__() == 32
        # Private key file is chmod 0600.
        assert (priv_path.stat().st_mode & 0o777) == 0o600

        keyring_path = tmp_path / "keyring.json"
        keyring = gen_keyring.build_keyring(keys_dir)
        gen_keyring.write_keyring(keyring, keyring_path)

        # JSON shape matches what StaticKeyVerifier expects.
        data = json.loads(keyring_path.read_text(encoding="utf-8"))
        assert list(data.keys()) == [isd_as]
        assert list(data[isd_as].keys()) == ["default"]
        # Decodes to the exact public-key bytes on disk.
        assert base64.b64decode(data[isd_as]["default"]) == pub_path.read_bytes()

        # Load the generated private key, sign, and verify via the keyring.
        sk = Ed25519PrivateKey.from_private_bytes(priv_path.read_bytes())
        sig = Signature(
            meta=SignatureMeta(isd_as=isd_as, key_id="default", trc_serial=0, trc_base=0),
            sig_bytes=sk.sign(sample_payload),
        )
        verifier = StaticKeyVerifier.from_file(keyring_path)
        assert verifier.verify(signature=sig, payload_bytes=sample_payload) is True
        assert verifier.known_ases() == [isd_as]

    def test_generate_keypair_is_idempotent(self, tmp_path: Path) -> None:
        gen_keys = _load_script("generate_keys")
        keys_dir = tmp_path / "keys"
        isd_as = "1-ff00:0:130"

        priv_path, pub_path, created_first = gen_keys.generate_keypair(isd_as, keys_dir)
        assert created_first is True
        original_priv = priv_path.read_bytes()
        original_pub = pub_path.read_bytes()

        _, _, created_second = gen_keys.generate_keypair(isd_as, keys_dir)
        assert created_second is False
        # Key material is preserved — no clobber on re-run.
        assert priv_path.read_bytes() == original_priv
        assert pub_path.read_bytes() == original_pub

    def test_build_keyring_on_empty_dir_yields_empty(self, tmp_path: Path) -> None:
        gen_keyring = _load_script("generate_keyring")
        assert gen_keyring.build_keyring(tmp_path) == {}

    def test_main_cli_round_trip(self, tmp_path: Path) -> None:
        """Exercise the argparse ``main()`` entry points of both scripts."""
        gen_keys = _load_script("generate_keys")
        gen_keyring = _load_script("generate_keyring")

        keys_dir = tmp_path / "keys"
        keyring_path = tmp_path / "keyring.json"

        rc = gen_keys.main(
            ["--keys-dir", str(keys_dir), "1-ff00:0:100", "1-ff00:0:110"]
        )
        assert rc == 0
        assert (keys_dir / "1-ff00:0:100.public").exists()
        assert (keys_dir / "1-ff00:0:110.public").exists()

        rc = gen_keyring.main(["--keys-dir", str(keys_dir), "--out", str(keyring_path)])
        assert rc == 0
        data = json.loads(keyring_path.read_text(encoding="utf-8"))
        assert set(data.keys()) == {"1-ff00:0:100", "1-ff00:0:110"}
