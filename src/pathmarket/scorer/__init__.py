"""Scoring subpackage — public surface only.

See ``DESIGN.md`` §6 for rationale and formulas.
"""

from pathmarket.scorer.scorer import ScoringConfig, compute_score, compute_violation_events

__all__ = ["ScoringConfig", "compute_score", "compute_violation_events"]
