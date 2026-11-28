"""Predictive Continuity (Step L): forecast what breaks before it breaks.

Slice 1 ships the pure forecast math in :mod:`forecast` only. Slice 2
adds the standing forecast watch in :mod:`watch`. Slice 3 adds
persisted forecast records + staleness in :mod:`records` and
:mod:`staleness`, with the read-only API in
``app.api.routes.predictive_continuity``. Slice 4 prep adds
calendar-aware forecasting in :mod:`calendar` (pure functions on
leave-record inputs; Step AG wiring is owed) and the standing-scan
entry point in :mod:`scheduler` (``forecast-scan`` CLI). Later slices
add live-work wiring, proposals, drill reconciliation, and UI.
"""

from app.predictive_continuity.calendar import (
    MAX_LEAVE_DAYS,
    PLANNED_STATUSES,
    backup_overlap_warnings,
    leave_window_forecasts,
    merge_leave_intervals,
    normalize_leave_records,
)
from app.predictive_continuity.forecast import (
    forecast_summary,
    forecast_unavailability,
    money_at_risk,
    snapshot_fingerprint,
)
from app.predictive_continuity.records import (
    filter_forecast_visibility,
    get_forecast,
    list_forecasts,
    prune_forecasts,
    record_scan,
    serialize_record,
)
from app.predictive_continuity.scheduler import (
    build_scan_report,
    format_scan_summary,
    load_snapshot,
    load_state,
    run_forecast_scan,
    save_state,
)
from app.predictive_continuity.staleness import (
    assess_staleness,
    staleness_summary,
    trust_for_score,
)
from app.predictive_continuity.watch import (
    DEFAULT_RULES,
    ForecastAlert,
    ForecastRule,
    evaluate_forecast_watch,
    find_critical_ids,
    fingerprint_changed,
    format_watch_message,
    is_quiet_hour,
    scan_forecasts,
    validate_forecast_rules,
    watch_fingerprint,
)

__all__ = [
    "DEFAULT_RULES",
    "ForecastAlert",
    "ForecastRule",
    "MAX_LEAVE_DAYS",
    "PLANNED_STATUSES",
    "assess_staleness",
    "backup_overlap_warnings",
    "build_scan_report",
    "evaluate_forecast_watch",
    "filter_forecast_visibility",
    "find_critical_ids",
    "fingerprint_changed",
    "forecast_summary",
    "forecast_unavailability",
    "format_scan_summary",
    "format_watch_message",
    "get_forecast",
    "is_quiet_hour",
    "leave_window_forecasts",
    "list_forecasts",
    "load_snapshot",
    "load_state",
    "merge_leave_intervals",
    "money_at_risk",
    "normalize_leave_records",
    "prune_forecasts",
    "record_scan",
    "run_forecast_scan",
    "save_state",
    "scan_forecasts",
    "serialize_record",
    "snapshot_fingerprint",
    "staleness_summary",
    "trust_for_score",
    "validate_forecast_rules",
    "watch_fingerprint",
]
