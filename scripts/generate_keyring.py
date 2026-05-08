"""Build ``keyring.json`` from ``keys/*.public`` (``DESIGN.md`` §10.3).

Walks ``<keys_dir>`` for every ``<isd_as>.public`` file and emits::

    {
      "<isd_as>": {"default": "<base64(32-byte raw pubkey)>"},
      ...
    }

This is the format consumed by ``StaticKeyVerifier.from_file``.

CLI::

    python scripts/generate_keyring.py
    python scripts/generate_keyring.py --keys-dir keys/ --out keyring.json
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path


DEFAULT_KEYS_DIR = Path("keys")
DEFAULT_OUTPUT = Path("keyring.json")
DEFAULT_KEY_ID = "default"


def build_keyring(keys_dir: Path, key_id: str = DEFAULT_KEY_ID) -> dict[str, dict[str, str]]:
    """Walk ``keys_dir/*.public`` and return the keyring mapping.

    The returned mapping is JSON-serialisable: base64-encoded ASCII strings,
    not raw bytes. Sorted by ISD-AS for reproducible output.
    """

    keyring: dict[str, dict[str, str]] = {}
    for public_path in sorted(keys_dir.glob("*.public")):
        isd_as = public_path.stem
        pubkey_bytes = public_path.read_bytes()
        keyring[isd_as] = {key_id: base64.b64encode(pubkey_bytes).decode("ascii")}
    return keyring


def write_keyring(keyring: dict[str, dict[str, str]], out_path: Path) -> None:
    out_path.write_text(json.dumps(keyring, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys-dir", type=Path, default=DEFAULT_KEYS_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--key-id", default=DEFAULT_KEY_ID)
    args = parser.parse_args(argv)

    if not args.keys_dir.is_dir():
        print(f"error: keys directory not found: {args.keys_dir}", file=sys.stderr)
        return 1

    keyring = build_keyring(args.keys_dir, key_id=args.key_id)
    if not keyring:
        print(f"warning: no *.public files found in {args.keys_dir}", file=sys.stderr)

    write_keyring(keyring, args.out)
    print(f"wrote {len(keyring)} key(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
