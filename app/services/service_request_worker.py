"""Transactional, idempotent response-SLA escalation for employee service requests."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    ROLE_ADMIN,
    DepartmentManager,
    ServiceRequest,
    ServiceRequestEvent,
    ServiceRequestNotification,
    User,
)
from app.services.audit import AuditContext, record
from app.services.automation import enqueue_service_request_workflows

logger = logging.getLogger(__name__)
SERVICE_REQUEST_SLA_POLL_SECONDS = 30
SERVICE_REQUEST_SLA_BATCH = 100


async def _escalation_recipients(db: AsyncSession, request: ServiceRequest) -> set[uuid.UUID]:
    """Select current escalation recipients without leaking HR requests to managers."""
    recipients = {request.requester_user_id}
    if request.category == "hr" or request.department_group_id is None:
        admin_ids = await db.scalars(
            select(User.id).where(User.role == ROLE_ADMIN, User.is_active.is_(True))
        )
        recipients.update(admin_ids)
        return recipients

    manager_ids = set(
        await db.scalars(
            select(User.id)
            .join(DepartmentManager, DepartmentManager.manager_user_id == User.id)
            .where(
                DepartmentManager.group_id == request.department_group_id,
                User.is_active.is_(True),
            )
        )
    )
    recipients.update(manager_ids)
    if not manager_ids:
        admin_ids = await db.scalars(
            select(User.id).where(User.role == ROLE_ADMIN, User.is_active.is_(True))
        )
        recipients.update(admin_ids)
    if request.assigned_to_user_id is not None:
        assigned = await db.get(User, request.assigned_to_user_id)
        if assigned is not None and assigned.is_active:
            recipients.add(assigned.id)
    return recipients


async def process_service_request_slas(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    batch_size: int = SERVICE_REQUEST_SLA_BATCH,
) -> int:
    """Escalate due unanswered requests and enqueue in-app notifications atomically.

    The row lock serializes competing worker processes. A nullable escalation timestamp plus the
    unique notification key makes retries idempotent. The inbox record is committed with the
    immutable timeline event; no email or external provider is called from this worker.
    """
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("The service request SLA worker requires a timezone-aware timestamp.")
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")

    async with sessionmaker() as db:
        due = list(
            (
                await db.execute(
                    select(ServiceRequest)
                    .where(
                        ServiceRequest.status == "submitted",
                        ServiceRequest.response_due_at <= current,
                        ServiceRequest.response_sla_met_at.is_(None),
                        ServiceRequest.sla_escalated_at.is_(None),
                    )
                    .order_by(ServiceRequest.response_due_at, ServiceRequest.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        if not due:
            await db.rollback()
            return 0

        for request in due:
            request.sla_escalated_at = current
            db.add(
                ServiceRequestEvent(
                    request_id=request.id,
                    actor_user_id=None,
                    actor_email="service-request-sla-worker",
                    action="sla_escalated",
                    from_status=request.status,
                    to_status=request.status,
                    comment="Initial response target passed; in-app escalation notifications were created.",
                    created_at=current,
                )
            )
            recipients = await _escalation_recipients(db, request)
            if recipients:
                await db.execute(
                    insert(ServiceRequestNotification)
                    .values(
                        [
                            {
                                "id": uuid.uuid4(),
                                "request_id": request.id,
                                "recipient_user_id": recipient_id,
                                "notification_type": "response_sla_escalated",
                                "created_at": current,
                            }
                            for recipient_id in sorted(recipients, key=str)
                        ]
                    )
                    .on_conflict_do_nothing(constraint="uq_service_request_notifications_request_recipient_type")
                )
            if request.category != "hr" and request.department_group_id is not None:
                await enqueue_service_request_workflows(db, request_id=request.id,
                    department_group_id=request.department_group_id, requester_user_id=request.requester_user_id,
                    event_type="sla_escalated", now=current)
            record(
                db,
                AuditContext(None, "service-request-sla-worker", None, None),
                "service_request.sla_escalated",
                target=str(request.id),
                details={"priority": request.priority, "recipient_count": len(recipients)},
            )
        await db.commit()
        return len(due)


async def run_service_request_sla_worker(
    sessionmaker: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
    *,
    interval_seconds: float = SERVICE_REQUEST_SLA_POLL_SECONDS,
) -> None:
    """Poll until stopped; one failure never kills the long-running document worker process."""
    while not stop.is_set():
        try:
            worked = await process_service_request_slas(sessionmaker)
        except Exception as exc:
            logger.warning("Service request SLA iteration failed (%s)", type(exc).__name__)
            worked = 0
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
