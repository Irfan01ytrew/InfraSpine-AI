"""Company digital twin (Step S / v2 Step K).

Pure-function simulation over twin snapshots lives in
:mod:`app.digital_twin.impact`. The read-only snapshot builder lives
in :mod:`app.digital_twin.snapshot` (slice 2) and the outbox-driven
live sync feeds in :mod:`app.digital_twin.feeds` (slice 3). Horizon
predictions, the what-if engine, and the twin UI are later slices (see
``docs/step-s-digital-twin-plan.md``).
"""

from app.digital_twin.feeds import SUPPORTED_EVENTS, apply_feed, translate_event
from app.digital_twin.horizon import (
    DEFAULT_HORIZON_RULES,
    evaluate_horizon_watch,
    fingerprint_changed,
    predict_absence,
    refresh_predictions,
    snapshot_fingerprint,
)
from app.digital_twin.impact import money_at_risk, simulate_absence
from app.digital_twin.snapshot import build_snapshot, build_twin_snapshot, source_freshness
from app.digital_twin.whatif import build_scenario, compare_drill, run_whatif

__all__ = [
    "DEFAULT_HORIZON_RULES", "SUPPORTED_EVENTS", "apply_feed", "build_scenario",
    "build_snapshot", "build_twin_snapshot", "compare_drill",
    "evaluate_horizon_watch", "fingerprint_changed", "money_at_risk",
    "predict_absence", "refresh_predictions", "run_whatif",
    "simulate_absence", "snapshot_fingerprint", "source_freshness",
    "translate_event",
]
