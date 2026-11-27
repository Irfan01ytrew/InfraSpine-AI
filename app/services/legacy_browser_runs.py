"""Persistent approval lifecycle. Browser result text is deliberately transient."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, LegacyBrowserUnavailableError, NotFoundError, PermissionDeniedError
from app.db.models import LegacyBrowserRun, ROLE_ADMIN, User
from app.schemas.legacy_browser import (
    CapturedBrowserPage,
    LegacyBrowserApprovalOut,
    LegacyBrowserRequest,
    LegacyBrowserRunOut,
    LegacyBrowserStatusOut,
)
from app.services import audit, legacy_browser
from app.services.audit import AuditContext


def status(settings: Settings) -> LegacyBrowserStatusOut:
    try:
        legacy_browser.ensure_available(settings)
    except legacy_browser.LegacyBrowserPolicyError:
        enabled = False
    else:
        enabled = True
    return LegacyBrowserStatusOut(
        enabled=enabled,
        maximum_requests=settings.rag_legacy_browser_max_requests,
        maximum_response_bytes=settings.rag_legacy_browser_max_response_bytes,
        maximum_result_characters=settings.rag_legacy_browser_max_result_chars,
    )


async def list_runs(db: AsyncSession, user: User) -> list[LegacyBrowserRunOut]:
    stmt = select(LegacyBrowserRun)
    if user.role != ROLE_ADMIN:
        stmt = stmt.where(LegacyBrowserRun.requester_user_id == user.id)
    rows = (await db.scalars(stmt.order_by(LegacyBrowserRun.requested_at.desc()).limit(100))).all()
    return [LegacyBrowserRunOut.model_validate(row) for row in rows]


async def create_run(
    db: AsyncSession,
    user: User,
    ctx: AuditContext,
    payload: LegacyBrowserRequest,
    settings: Settings,
) -> LegacyBrowserRunOut:
    try:
        legacy_browser.ensure_available(settings)
        start_url = legacy_browser.validate_target_url(payload.start_url, settings)
    except legacy_browser.LegacyBrowserPolicyError as exc:
        if exc.code == "origin_not_allowed":
            from app.core.errors import InvalidInputError

            raise InvalidInputError("The requested site is not on the operator allowlist.") from None
        raise LegacyBrowserUnavailableError() from None
    row = LegacyBrowserRun(
        requester_user_id=user.id,
        label=payload.label,
        reason=payload.reason,
        start_url=start_url,
        link_labels=payload.link_labels,
    )
    db.add(row)
    audit.record(
        db,
        ctx,
        "legacy_browser.requested",
        target=str(row.id),
        details={"origin": legacy_browser.origin_for_target(start_url), "link_count": len(payload.link_labels)},
    )
    await db.commit()
    await db.refresh(row)
    return LegacyBrowserRunOut.model_validate(row)


async def _get_run(db: AsyncSession, run_id: uuid.UUID) -> LegacyBrowserRun:
    row = await db.get(LegacyBrowserRun, run_id)
    if row is None:
        raise NotFoundError("Browser review request not found.")
    return row


async def approve_run(
    db: AsyncSession,
    admin: User,
    ctx: AuditContext,
    run_id: uuid.UUID,
    settings: Settings,
) -> LegacyBrowserApprovalOut:
    admin_id = admin.id
    try:
        legacy_browser.ensure_available(settings)
    except legacy_browser.LegacyBrowserPolicyError:
        raise LegacyBrowserUnavailableError() from None
    row = await _get_run(db, run_id)
    if row.requester_user_id == admin_id:
        raise PermissionDeniedError("A different administrator must approve this browser request.")
    if row.status not in {"pending_approval", "failed"} or row.attempts >= legacy_browser.MAX_ATTEMPTS:
        raise ConflictError("This request is no longer eligible for approval or its retry limit was reached.")
    try:
        legacy_browser.validate_target_url(row.start_url, settings)
    except legacy_browser.LegacyBrowserPolicyError:
        raise LegacyBrowserUnavailableError("The requested site is no longer on the operator allowlist.") from None

    previous_status = row.status
    now = datetime.now(timezone.utc)
    await db.rollback()
    result = await db.execute(
        update(LegacyBrowserRun)
        .where(
            LegacyBrowserRun.id == run_id,
            LegacyBrowserRun.status == previous_status,
            LegacyBrowserRun.attempts < legacy_browser.MAX_ATTEMPTS,
            LegacyBrowserRun.requester_user_id != admin_id,
        )
        .values(
            status="running",
            approver_user_id=admin_id,
            attempts=LegacyBrowserRun.attempts + 1,
            approved_at=now,
            completed_at=None,
            failure_code=None,
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        raise ConflictError("Another reviewer changed this request. Reload and try again.")
    await db.commit()
    row = await _get_run(db, run_id)

    try:
        capture = await legacy_browser.capture_readonly(settings, row.start_url, list(row.link_labels))
    except legacy_browser.LegacyBrowserPolicyError as exc:
        failure_code = exc.code
        row = await _finish(db, run_id, "failed", failure_code, admin_id, ctx, None)
        return LegacyBrowserApprovalOut(run=LegacyBrowserRunOut.model_validate(row), pages=[], request_count=0, response_bytes=0)
    except Exception:
        row = await _finish(db, run_id, "failed", "browser_capture_failed", admin_id, ctx, None)
        return LegacyBrowserApprovalOut(run=LegacyBrowserRunOut.model_validate(row), pages=[], request_count=0, response_bytes=0)

    row = await _finish(db, run_id, "completed", None, admin_id, ctx, capture)
    return LegacyBrowserApprovalOut(
        run=LegacyBrowserRunOut.model_validate(row),
        pages=[CapturedBrowserPage(label=page.label, url=page.url, title=page.title, text=page.text) for page in capture.pages],
        request_count=capture.request_count,
        response_bytes=capture.response_bytes,
    )


async def _finish(
    db: AsyncSession,
    run_id: uuid.UUID,
    state: str,
    failure_code: str | None,
    admin_id: uuid.UUID,
    ctx: AuditContext,
    capture: legacy_browser.CaptureResult | None,
) -> LegacyBrowserRun:
    await db.rollback()
    row = await _get_run(db, run_id)
    if row.status != "running" or row.approver_user_id != admin_id:
        raise ConflictError("The browser request changed while it was running.")
    row.status = state
    row.failure_code = failure_code
    row.completed_at = datetime.now(timezone.utc)
    audit.record(
        db,
        ctx,
        "legacy_browser.completed" if state == "completed" else "legacy_browser.failed",
        outcome="success" if state == "completed" else "failure",
        target=str(run_id),
        details={
            "failure_code": failure_code,
            "page_count": len(capture.pages) if capture else 0,
            "request_count": capture.request_count if capture else 0,
            "response_bytes": capture.response_bytes if capture else 0,
        },
    )
    await db.commit()
    await db.refresh(row)
    return row


async def reject_run(db: AsyncSession, admin: User, ctx: AuditContext, run_id: uuid.UUID) -> LegacyBrowserRunOut:
    admin_id = admin.id
    row = await _get_run(db, run_id)
    if row.requester_user_id == admin_id:
        raise PermissionDeniedError("A different administrator must review this request.")
    await db.rollback()
    result = await db.execute(
        update(LegacyBrowserRun)
        .where(LegacyBrowserRun.id == run_id, LegacyBrowserRun.status == "pending_approval")
        .values(status="rejected", approver_user_id=admin_id, completed_at=datetime.now(timezone.utc))
    )
    if result.rowcount != 1:
        await db.rollback()
        raise ConflictError("This request is no longer pending review.")
    audit.record(db, ctx, "legacy_browser.rejected", target=str(run_id), details={"decision": "rejected"})
    await db.commit()
    row = await _get_run(db, run_id)
    return LegacyBrowserRunOut.model_validate(row)


async def cancel_run(db: AsyncSession, user: User, ctx: AuditContext, run_id: uuid.UUID) -> LegacyBrowserRunOut:
    user_id = user.id
    row = await _get_run(db, run_id)
    if row.requester_user_id != user_id:
        raise PermissionDeniedError()
    await db.rollback()
    result = await db.execute(
        update(LegacyBrowserRun)
        .where(
            LegacyBrowserRun.id == run_id,
            LegacyBrowserRun.requester_user_id == user_id,
            LegacyBrowserRun.status == "pending_approval",
        )
        .values(status="cancelled", completed_at=datetime.now(timezone.utc))
    )
    if result.rowcount != 1:
        await db.rollback()
        raise ConflictError("Only a pending request can be cancelled.")
    audit.record(db, ctx, "legacy_browser.cancelled", target=str(run_id), details={"decision": "cancelled"})
    await db.commit()
    row = await _get_run(db, run_id)
    return LegacyBrowserRunOut.model_validate(row)
