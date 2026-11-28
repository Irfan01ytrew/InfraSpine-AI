"""Company digital twin (Step S / v2 Step K).

Pure-function simulation over twin snapshots lives in
:mod:`app.digital_twin.impact`. The read-only snapshot builder lives
in :mod:`app.digital_twin.snapshot` (slice 2) and the outbox-driven
live sync feeds in :mod:`app.digital_twin.feeds` (slice 3). Horizon
predictions, the what-if engine, and the twin UI are later slices (see
``docs/step-s-digital-twin-plan.md``).
"""

# Slice 4 (Step K v2): the delegating what-if dispatcher lives in
# ``app.digital_twin.whatif`` (scenario library, ``run_whatif``
# delegating failure kinds to Step AL and absence kinds to Step S,
# drill comparison). At rebase onto Step S this module replaces the
# Step S ``whatif.py`` — add its names to the merged ``__init__.py``.
# Slice 5 adds the drill reconciliation loop in
# ``app.digital_twin.reconciliation`` (twin-vs-drill comparison
# unified into one review-queue finding format) and its persistence
# in ``app.digital_twin.reconciliation_records``.
# Slice 6 adds twin-driven proposals in ``app.digital_twin.proposals``
# (pure eligibility + drafting rules) and the scheduled
# reconciliation cycle in ``app.digital_twin.reconciliation_schedule``.
# The proposal service (``proposals_records``) needs sqlalchemy, so
# it is NOT imported here — same as ``health_records`` / ``versions``.

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

# Slice 7 adds multi-source trust scoring in
# ``app.digital_twin.trust`` (pure), cross-source conflict reporting
# in ``app.digital_twin.conflicts`` (pure), and the remaining live-sync
# feeds in ``app.digital_twin.feed_changes`` (pure translators) with their
# governed-change application in ``app.digital_twin.feed_records``
# (sqlalchemy — NOT imported here, same as ``proposals_records``).
from app.digital_twin.conflicts import (
    MAX_CONFLICTS,
    conflict_summary,
    detect_conflicts,
    redact_conflicts_for_viewer,
)
from app.digital_twin.feed_changes import (
    FEED_EVENTS,
    feed_change_fingerprint,
)
# NOTE: feed_changes.translate_event is NOT re-exported here —
# app.digital_twin.translate_event stays S's (feeds) version; the
# slice-7 translator is app.digital_twin.feed_changes.translate_event.
from app.digital_twin.trust import (
    TRUST_SOURCES,
    compute_trust,
    trust_band,
    trust_summary,
)
from app.digital_twin.proposals import (
    APPROVER_CLASSES,
    COVERAGE_ACTIONS,
    DUTY,
    ELIGIBILITY,
    ENTRY_SEVERITY,
    PROPOSAL_ACTIONS,
    REQUIRED_PERMISSION,
    ProposalAction,
    draft_proposal,
    eligible_actions,
    findings_from_simulation,
    map_entry_severity,
    proposal_fingerprint,
    proposal_summary,
    simulation_finding_fingerprint,
    validate_finding,
    validate_simulation_finding,
)
from app.digital_twin.reconciliation import (
    G_CODE_SEVERITY,
    MAX_FINDINGS,
    SEVERITIES,
    SOURCES,
    ReconciliationFinding,
    finding_fingerprint,
    findings_from_g_comparison,
    findings_from_twin_comparison,
    reconcile,
    reconciliation_summary,
)
from app.digital_twin.whatif import (
    ABSENCE_KINDS,
    FAILURE_KINDS,
    SCENARIOS,
    SCENARIO_KINDS,
    EngineUnavailable,
    build_scenario,
    compare_drill,
    list_templates,
    run_whatif,
    template_name,
)
from app.digital_twin.drift import (
    MAX_ENTITIES,
    MAX_RELATIONSHIPS,
    MONEY_KINDS,
    REVIEW_MAX_SCORE,
    TRUSTWORTHY_MAX_SCORE,
    diff_snapshots,
    drift_summary,
    snapshot_fingerprint,
    validate_snapshot,
)
from app.digital_twin.health import (
    DEGRADED_MIN_SCORE,
    HEALTHY_MIN_SCORE,
    compute_health,
    health_summary,
    prediction_is_stale,
)
from app.digital_twin.health_watch import (
    DEFAULT_QUIET_END,
    DEFAULT_QUIET_START,
    DEFAULT_RULES,
    HealthAlert,
    HealthRule,
    evaluate_health_watch,
    format_watch_message,
    is_quiet_hour,
    validate_health_rules,
    watch_fingerprint,
)

__all__ = [
    "ABSENCE_KINDS",
    "APPROVER_CLASSES",
    "COVERAGE_ACTIONS",
    "DEFAULT_HORIZON_RULES",
    "DEFAULT_QUIET_END",
    "DEFAULT_QUIET_START",
    "DEFAULT_RULES",
    "DEGRADED_MIN_SCORE",
    "DUTY",
    "ELIGIBILITY",
    "ENTRY_SEVERITY",
    "EngineUnavailable",
    "FAILURE_KINDS",
    "FEED_EVENTS",
    "G_CODE_SEVERITY",
    "HEALTHY_MIN_SCORE",
    "HealthAlert",
    "HealthRule",
    "MAX_CONFLICTS",
    "MAX_ENTITIES",
    "MAX_FINDINGS",
    "MAX_RELATIONSHIPS",
    "MONEY_KINDS",
    "PROPOSAL_ACTIONS",
    "ProposalAction",
    "REQUIRED_PERMISSION",
    "REVIEW_MAX_SCORE",
    "ReconciliationFinding",
    "SCENARIOS",
    "SCENARIO_KINDS",
    "SEVERITIES",
    "SOURCES",
    "SUPPORTED_EVENTS",
    "TRUSTWORTHY_MAX_SCORE",
    "TRUST_SOURCES",
    "apply_feed",
    "build_scenario",
    "build_snapshot",
    "build_twin_snapshot",
    "compare_drill",
    "compute_health",
    "compute_trust",
    "conflict_summary",
    "detect_conflicts",
    "diff_snapshots",
    "draft_proposal",
    "drift_summary",
    "eligible_actions",
    "evaluate_health_watch",
    "evaluate_horizon_watch",
    "feed_change_fingerprint",
    "finding_fingerprint",
    "findings_from_g_comparison",
    "findings_from_simulation",
    "findings_from_twin_comparison",
    "fingerprint_changed",
    "format_watch_message",
    "health_summary",
    "is_quiet_hour",
    "list_templates",
    "map_entry_severity",
    "money_at_risk",
    "predict_absence",
    "prediction_is_stale",
    "proposal_fingerprint",
    "proposal_summary",
    "reconcile",
    "reconciliation_summary",
    "redact_conflicts_for_viewer",
    "refresh_predictions",
    "run_whatif",
    "simulate_absence",
    "simulation_finding_fingerprint",
    "snapshot_fingerprint",
    "source_freshness",
    "template_name",
    "translate_event",
    "trust_band",
    "trust_summary",
    "validate_finding",
    "validate_health_rules",
    "validate_simulation_finding",
    "validate_snapshot",
    "watch_fingerprint",
]
