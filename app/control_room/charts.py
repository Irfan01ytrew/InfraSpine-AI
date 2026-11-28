"""Control Room live charts (Step AF slice 2).

One read-only payload for the dashboard page: spend series + invoice status
counts (both permission-filtered through ``FinanceService``), run counts for
the scope, a health strip, and a live preview of the alerts that would fire
right now (slice 1 evaluator, no sending, no persistence — delivery is
slice 4's worker).

Authorization lives in the route (``require_scope`` → 404 for a non-member,
403 for a member without ``workflow.access``). Finance data never widens
visibility: ``FinanceService`` returns only the groups the caller may read
with ``finance.read``.

Honest gaps, kept visible rather than faked:
- There is no persisted workflow-engine worker heartbeat yet, so
  ``last_tick_age_seconds`` is fed as ``None`` (the ``worker_down`` rule is
  skipped for a stale tick and only fires when the engine is off). The
  health strip reports the last run activity instead, labelled as such.
- ``failed_payments_last_hour`` / ``oldest_approval_age_hours`` /
  ``failed_ingestions_last_hour`` have no cheap live source yet; they are
  ``None`` until slice 4 builds the signal collector.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import desc, func, select

from app.control_room.alerts import (
    DEFAULT_RULES,
    MetricsSnapshot,
    alert_summary,
    evaluate_alerts,
)
from app.db.workflow_engine_models import Workflow, WorkflowRun
from app.finance.service import FinanceService
from app.rag.ingestion import workers_online
from app.services.operational_health import get_department_quotas

logger = logging.getLogger(__name__)

_FAILED_WINDOW = timedelta(hours=1)
_RECENT_FAILED_LIMIT = 5


def _alert_out(alert) -> dict:
    return {
        "rule_key": alert.rule_key,
        "severity": alert.severity,
        "title": alert.title,
        "detail": alert.detail,
        "evaluated_at": alert.evaluated_at.isoformat(),
    }


async def _runs_summary(db, scope_id: UUID, *, now: datetime) -> dict:
    by_status = dict(
        (
            await db.execute(
                select(WorkflowRun.status, func.count())
                .where(WorkflowRun.scope_id == scope_id)
                .group_by(WorkflowRun.status)
            )
        ).all()
    )
    failed_last_hour = int(
        await db.scalar(
            select(func.count()).where(
                WorkflowRun.scope_id == scope_id,
                WorkflowRun.status == "failed",
                WorkflowRun.finished_at >= now - _FAILED_WINDOW,
            )
        )
        or 0
    )
    recent_failed = (
        await db.execute(
            select(WorkflowRun, Workflow.name)
            .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
            .where(WorkflowRun.scope_id == scope_id, WorkflowRun.status == "failed")
            .order_by(desc(WorkflowRun.finished_at), desc(WorkflowRun.created_at))
            .limit(_RECENT_FAILED_LIMIT)
        )
    ).all()
    last_activity = await db.scalar(
        select(
            func.max(
                func.coalesce(
                    WorkflowRun.finished_at,
                    WorkflowRun.started_at,
                    WorkflowRun.created_at,
                )
            )
        ).where(WorkflowRun.scope_id == scope_id)
    )
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "failed_last_hour": failed_last_hour,
        "recent_failed": [
            {
                "id": str(run.id),
                "workflow_id": str(run.workflow_id),
                "workflow_name": name,
                "failure_code": run.failure_code,
                "blocked_reason": run.blocked_reason,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "created_at": run.created_at.isoformat(),
            }
            for run, name in recent_failed
        ],
        "last_activity_at": last_activity.isoformat() if last_activity else None,
    }


async def _health(db, settings, runs: dict, *, now: datetime) -> dict:
    worker_enabled = bool(getattr(settings, "workflow_engine_on", False))
    ingestion_online = await workers_online(db, settings)
    llm_quota_max_percent = None
    try:
        quotas, _alerts = await get_department_quotas(db, days=30)
        if quotas:
            llm_quota_max_percent = max(q.utilization_percent for q in quotas)
    except Exception:  # a quota failure must not take the whole dashboard down
        logger.warning("Control room: LLM quota lookup failed", exc_info=True)
    last_activity_at = runs["last_activity_at"]
    last_activity_age = None
    if last_activity_at is not None:
        last_activity_age = (now - datetime.fromisoformat(last_activity_at)).total_seconds()
    return {
        "worker_enabled": worker_enabled,
        "last_run_activity_at": last_activity_at,
        "last_run_activity_age_seconds": last_activity_age,
        "ingestion_workers_online": ingestion_online,
        "llm_quota_max_percent": llm_quota_max_percent,
    }


def _snapshot(health: dict, runs: dict, invoice_status: dict) -> MetricsSnapshot:
    """Metrics the evaluator can honestly read right now; the rest is None."""
    return MetricsSnapshot(
        engine_on=health["worker_enabled"],
        last_tick_age_seconds=None,  # no persisted worker heartbeat yet (slice 4)
        queued_runs=runs["by_status"].get("queued", 0),
        failed_runs_last_hour=runs["failed_last_hour"],
        failed_payments_last_hour=None,  # no cheap live source yet (slice 4)
        exceptions_count=invoice_status["by_status"].get("exception", 0),
        oldest_approval_age_hours=None,  # no cheap live source yet (slice 4)
        ingestion_workers_online=health["ingestion_workers_online"],
        failed_ingestions_last_hour=None,  # no cheap live source yet (slice 4)
        llm_quota_percent=health["llm_quota_max_percent"],
    )


async def build_charts(db, user, scope_id: UUID, settings, cipher, *, now: datetime | None = None) -> dict:
    """Assemble the whole dashboard payload for one workflow scope."""
    now = now or datetime.now(UTC)
    finance = FinanceService(db, user, settings, cipher)
    spend = await finance.spend(months=12)
    invoice_status = await finance.status_counts()
    runs = await _runs_summary(db, scope_id, now=now)
    health = await _health(db, settings, runs, now=now)
    alerts = evaluate_alerts(_snapshot(health, runs, invoice_status), now=now, rules=DEFAULT_RULES)
    return {
        "scope_id": str(scope_id),
        "generated_at": now.isoformat(),
        "spend": spend,
        "invoices_by_status": invoice_status,
        "runs": runs,
        "health": health,
        "active_alerts": [_alert_out(a) for a in alerts],
        "alert_summary": alert_summary(alerts),
    }
