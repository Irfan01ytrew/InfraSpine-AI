"""Step W — Self-Healing: fault detection and safe auto-remedies.

Slice 1 ships the pure-function fault detector only (see detector.py). Later
slices add signal collection, the remedy executor, escalation delivery, the
auto-test loop, watchdog integration, and the dashboard. No remedy here ever
touches business data, permissions, approvals, or payments.
"""

from app.self_healing.collector import (
    Signals,
    attempt_history,
    collect_and_store,
    read_signals,
    record_incident,
    store_probes,
)
from app.self_healing.detector import (
    AttemptHistory,
    Decision,
    Incident,
    Probe,
    classify_incident,
    decide_action,
    escalation_report,
    summarize_incidents,
)
from app.self_healing.executor import (
    DUTY_SELF_HEALING_REMEDY,
    REMEDY_HANDLERS,
    RemedyHandler,
    RemedyOutcome,
    execute_remedy,
)

__all__ = [
    "AttemptHistory",
    "Decision",
    "Incident",
    "Probe",
    "Signals",
    "DUTY_SELF_HEALING_REMEDY",
    "REMEDY_HANDLERS",
    "RemedyHandler",
    "RemedyOutcome",
    "attempt_history",
    "classify_incident",
    "collect_and_store",
    "decide_action",
    "escalation_report",
    "execute_remedy",
    "read_signals",
    "record_incident",
    "store_probes",
    "summarize_incidents",
]
