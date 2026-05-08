"""Verifier subpackage.

Public surface: the ``Verifier`` protocol and ``StaticKeyVerifier`` implementation.
"""

from pathmarket.verifier.protocol import Verifier
from pathmarket.verifier.static import StaticKeyVerifier

__all__ = ["StaticKeyVerifier", "Verifier"]
