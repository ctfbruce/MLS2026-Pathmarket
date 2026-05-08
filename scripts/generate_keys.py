"""Generate Ed25519 keypairs for PathMarket v2 ASes (``DESIGN.md`` §10.3).

Writes two files per ISD-AS into ``<keys_dir>``:

- ``<isd_as>.private`` — raw 32-byte Ed25519 seed, chmod 0600.
- ``<isd_as>.public``  — raw 32-byte Ed25519 public key.

Idempotent: if both files already exist for an ISD-AS, skip it.

CLI::

    python scripts/generate_keys.py 1-ff00:0:110 1-ff00:0:112 ...
    python scripts/generate_keys.py --keys-dir keys/ 1-ff00:0:110
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


DEFAULT_KEYS_DIR = Path("keys")


def generate_keypair(isd_as: str, keys_dir: Path) -> tuple[Path, Path, bool]:
    """Generate ``<isd_as>.{private,public}`` in ``keys_dir``.

    Returns ``(private_path, public_path, created)`` where ``created`` is
    False if both files already existed (idempotent skip).
    """

    keys_dir.mkdir(parents=True, exist_ok=True)
    private_path = keys_dir / f"{isd_as}.private"
    public_path = keys_dir / f"{isd_as}.public"

    if private_path.exists() and public_path.exists():
        return private_path, public_path, False

    sk = Ed25519PrivateKey.generate()
    private_bytes = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    private_path.write_bytes(private_bytes)
    os.chmod(private_path, 0o600)
    public_path.write_bytes(public_bytes)
    return private_path, public_path, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("isd_as", nargs="+", help="ISD-AS identifiers, e.g. 1-ff00:0:110")
    parser.add_argument(
        "--keys-dir",
        type=Path,
        default=DEFAULT_KEYS_DIR,
        help=f"Directory to write keys into (default: {DEFAULT_KEYS_DIR})",
    )
    args = parser.parse_args(argv)

    for isd_as in args.isd_as:
        _priv, _pub, created = generate_keypair(isd_as, args.keys_dir)
        status = "generated" if created else "exists — skipped"
        print(f"{isd_as}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
