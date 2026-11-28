"""Company Resilience AI (Step AH) — advisory dependency-impact math.

Pure functions only: no I/O, no database, no network, no secrets.
Nothing here executes a recovery step, grants access, or moves money.
"""

from app.resilience.impact import (
    assess_risk,
    rank_risks,
    recovery_progress,
    score_disruption,
)
from app.resilience.plans import build_plan, plan_fingerprint, plan_progress_from_steps, plan_spec

__all__ = [
    "assess_risk",
    "build_plan",
    "plan_fingerprint",
    "plan_progress_from_steps",
    "plan_spec",
    "rank_risks",
    "recovery_progress",
    "score_disruption",
]
