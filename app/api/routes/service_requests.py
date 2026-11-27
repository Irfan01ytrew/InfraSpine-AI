"""Employee self-service support requests and privacy-aware human escalation."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api.deps import AppSettings, CurrentUser, DbSession, audit_context
from app.core.errors import NotFoundError
from app.db.form_models import RequestAttachment
from app.services import request_attachments
from app.services.request_forms import can_use_form
from app.db.models import AccessGroup, ServiceRequest, ServiceRequestEvent, User
from app.schemas.service_requests import (
    ServiceRequestComment,
    ServiceRequestCreate,
    ServiceRequestEventOut,
    ServiceRequestListResponse,
    ServiceRequestNotificationListResponse,
    ServiceRequestNotificationOut,
    ApprovalDecision,
    ApprovalStepOut,
    AttachmentOut,
    FormAnswerOut,
    RequestBudgetLine,
    ServiceRequestApproval,
    ServiceRequestOut,
    ServiceRequestTriage,
)
from app.db.approval_models import FINANCIAL_CATEGORIES
from app.services.approval_authority import check_approval, suggest_approver
from app.services.approval_chains import decidable_step, decide, request_steps
from app.services.budgets import budgets_for_request
from app.db.org_models import Position
from app.db.form_models import RequestForm
from app.schemas.request_forms import RequestFormListResponse, form_out
from app.services.service_requests import ServiceRequestService

router = APIRouter(prefix="/api/service-requests", tags=["service-requests"])


def get_service(request: Request, db: DbSession, user: CurrentUser) -> ServiceRequestService:
    return ServiceRequestService(db, user, audit_context(request, user))


Requests = Annotated[ServiceRequestService, Depends(get_service)]


async def _render_many(
    service: ServiceRequestService, items: list[ServiceRequest]
) -> list[ServiceRequestOut]:
    if not items:
        return []
    db = service.db
    user_ids = {item.requester_user_id for item in items}
    user_ids.update(item.assigned_to_user_id for item in items if item.assigned_to_user_id is not None)
    users = {
        user.id: user
        for user in await db.scalars(select(User).where(User.id.in_(user_ids)))
    }
    department_ids = {item.department_group_id for item in items if item.department_group_id is not None}
    departments = {
        group.id: group
        for group in await db.scalars(select(AccessGroup).where(AccessGroup.id.in_(department_ids)))
    } if department_ids else {}
    request_ids = [item.id for item in items]
    event_rows = await db.scalars(
        select(ServiceRequestEvent)
        .where(ServiceRequestEvent.request_id.in_(request_ids))
        .order_by(ServiceRequestEvent.created_at, ServiceRequestEvent.id)
    )
    events_by_request: dict[uuid.UUID, list[ServiceRequestEventOut]] = {request_id: [] for request_id in request_ids}
    for event in event_rows:
        events_by_request[event.request_id].append(
            ServiceRequestEventOut(
                id=event.id,
                actor_email=event.actor_email,
                action=event.action,
                from_status=event.from_status,
                to_status=event.to_status,
                comment=event.comment,
                created_at=event.created_at,
            )
        )

    result = []
    now = datetime.now(UTC)
    for item in items:
        requester = users[item.requester_user_id]
        assignee = users.get(item.assigned_to_user_id)
        can_manage = await service.can_manage(item)
        steps = await request_steps(db, item.id) if item.category in FINANCIAL_CATEGORIES else []
        step_out, can_decide_step, step_blocker = [], False, None
        if steps:
            people = {u.id: u.email for u in await db.scalars(select(User).where(User.id.in_({s.approver_user_id for s in steps if s.approver_user_id})))}
            titles = {p.id: p.title for p in await db.scalars(select(Position).where(Position.id.in_({s.approver_position_id for s in steps if s.approver_position_id})))}
            for step in steps:
                if step.approver_kind == "position":
                    who = f"Any {titles.get(step.approver_position_id, 'position holder')}"
                elif step.approver_user_id:
                    who = people.get(step.approver_user_id, "Unavailable")
                else:
                    who = "Department managers (the requester has no manager)"
                step_out.append(ApprovalStepOut(
                    order=step.step_order, label=step.label, approver_kind=step.approver_kind, approver=who, stage=step.stage, stage_mode=step.stage_mode,
                    status=step.status, final=step.final, decided_by_email=step.decided_by_email,
                    decided_at=step.decided_at, comment=step.comment, on_behalf_of_email=step.on_behalf_of_email,
                    pending_since=step.pending_since if step.status == "pending" else None, reminder_count=step.reminder_count,
                    escalated_at=step.escalated_at,
                ))
            if any(s.status == "pending" for s in steps) and item.status not in {"resolved", "rejected", "cancelled"}:
                decidable, check = await decidable_step(db, service.user, item, steps, approving=True)
                can_decide_step, step_blocker = decidable is not None, check.reason
        budget_lines = []
        if (can_manage or can_decide_step) and item.category in FINANCIAL_CATEGORIES and item.status not in {"resolved", "rejected", "cancelled"}:
            for use in await budgets_for_request(db, item):
                after = use.approved + item.amount
                budget_lines.append(RequestBudgetLine(
                    name=use.budget.name, amount=use.budget.amount, currency=use.budget.currency, approved=use.approved,
                    committed=use.committed, after_approval=after, would_exceed=after > use.budget.amount, enforcement=use.budget.enforcement,
                ))
        approval = None
        if not steps and can_manage and item.category in FINANCIAL_CATEGORIES and item.status not in {"resolved", "rejected", "cancelled"}:
            check = await check_approval(db, service.user, item)
            suggested = None if check.can_approve else await suggest_approver(db, item)
            approval = ServiceRequestApproval(
                can_approve=check.can_approve, reason=check.reason, your_limit=check.limit,
                limit_currency=check.limit_currency, suggested_approver_email=suggested.email if suggested else None,
            )
        result.append(
            ServiceRequestOut(
                id=item.id,
                requester_user_id=item.requester_user_id,
                requester_email=requester.email,
                department_group_id=item.department_group_id,
                department_name=departments[item.department_group_id].name if item.department_group_id in departments else None,
                assigned_to_user_id=item.assigned_to_user_id,
                assigned_to_email=assignee.email if assignee else None,
                category=item.category,
                priority=item.priority,
                subject=item.subject,
                description=item.description,
                status=item.status,
                amount=item.amount,
                currency=item.currency,
                approval=approval,
                approval_steps=step_out,
                can_decide_step=can_decide_step,
                step_blocker=step_blocker,
                budgets=budget_lines,
                created_at=item.created_at,
                updated_at=item.updated_at,
                closed_at=item.closed_at,
                response_due_at=item.response_due_at,
                response_sla_met_at=item.response_sla_met_at,
                sla_escalated_at=item.sla_escalated_at,
                response_sla_overdue=(
                    item.status == "submitted"
                    and item.response_sla_met_at is None
                    and item.response_due_at <= now
                ),
                can_manage=can_manage,
                can_cancel=item.requester_user_id == service.user.id and (
                    item.status in {"submitted", "acknowledged"} or (bool(steps) and item.status == "in_progress")
                ),
                events=events_by_request[item.id],
                form_name=item.form_name,
                form_answers=[FormAnswerOut(label=a["label"], value=a["value"], attachments=a.get("attachments", [])) for a in item.form_answers or []],
            )
        )
    return result


@router.get("", response_model=ServiceRequestListResponse)
async def list_requests(service: Requests) -> ServiceRequestListResponse:
    return ServiceRequestListResponse(items=await _render_many(service, await service.list_readable()))


@router.post("", response_model=ServiceRequestOut, status_code=status.HTTP_201_CREATED)
async def create_request(payload: ServiceRequestCreate, service: Requests) -> ServiceRequestOut:
    request = await service.create(
        category=payload.category,
        priority=payload.priority,
        department_group_id=payload.department_group_id,
        subject=payload.subject,
        description=payload.description,
        amount=payload.amount,
        currency=payload.currency,
        form_id=payload.form_id,
        answers=payload.answers,
    )
    return (await _render_many(service, [request]))[0]


@router.post("/attachments", response_model=AttachmentOut, status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    service: Requests, settings: AppSettings,
    file: Annotated[UploadFile, File(description="PDF, Word, Excel, PowerPoint, text, CSV, PNG or JPEG, up to 10 MB")],
) -> AttachmentOut:
    """Upload a file for a form's file field; it is attached when the request is submitted."""
    row = await request_attachments.upload(service.db, service.user, file.filename, file.file, settings)
    return AttachmentOut(id=row.id, filename=row.filename, size_bytes=row.size_bytes, content_type=row.content_type)


@router.delete("/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_attachment(attachment_id: uuid.UUID, service: Requests) -> Response:
    """Remove a file you uploaded but have not submitted yet."""
    await request_attachments.remove_pending(service.db, service.user, attachment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/forms", response_model=RequestFormListResponse)
async def list_request_forms(service: Requests) -> RequestFormListResponse:
    """Active custom request forms the current user may use."""
    rows = (await service.db.execute(
        select(RequestForm, AccessGroup.name).outerjoin(AccessGroup, AccessGroup.id == RequestForm.department_group_id)
        .where(RequestForm.is_active.is_(True)).order_by(RequestForm.name)
    )).all()
    usable = [(form, name) for form, name in rows if await can_use_form(service.db, service.user, form)]
    return RequestFormListResponse(forms=[form_out(form, name) for form, name in usable])


@router.get("/notifications", response_model=ServiceRequestNotificationListResponse)
async def list_notifications(service: Requests) -> ServiceRequestNotificationListResponse:
    rows = await service.list_notifications()
    return ServiceRequestNotificationListResponse(
        items=[
            ServiceRequestNotificationOut(
                id=row.id,
                request_id=row.request_id,
                notification_type=row.notification_type,
                created_at=row.created_at,
                read_at=row.read_at,
            )
            for row in rows
        ]
    )


@router.post("/notifications/{notification_id}/read", response_model=ServiceRequestNotificationOut)
async def mark_notification_read(
    notification_id: uuid.UUID, service: Requests
) -> ServiceRequestNotificationOut:
    row = await service.mark_notification_read(notification_id)
    return ServiceRequestNotificationOut(
        id=row.id,
        request_id=row.request_id,
        notification_type=row.notification_type,
        created_at=row.created_at,
        read_at=row.read_at,
    )


@router.get("/{request_id}", response_model=ServiceRequestOut)
async def get_request(request_id: uuid.UUID, service: Requests) -> ServiceRequestOut:
    request = await service.get_readable(request_id)
    return (await _render_many(service, [request]))[0]


@router.post("/{request_id}/comments", response_model=ServiceRequestOut)
async def add_comment(request_id: uuid.UUID, payload: ServiceRequestComment, service: Requests) -> ServiceRequestOut:
    request = await service.comment(request_id, payload.comment)
    return (await _render_many(service, [request]))[0]


@router.post("/{request_id}/triage", response_model=ServiceRequestOut)
async def triage_request(request_id: uuid.UUID, payload: ServiceRequestTriage, service: Requests) -> ServiceRequestOut:
    request = await service.triage(
        request_id,
        status=payload.status,
        assigned_to_user_id=payload.assigned_to_user_id,
        comment=payload.comment,
    )
    return (await _render_many(service, [request]))[0]


@router.post("/{request_id}/cancel", response_model=ServiceRequestOut)
async def cancel_request(request_id: uuid.UUID, service: Requests) -> ServiceRequestOut:
    request = await service.cancel(request_id)
    return (await _render_many(service, [request]))[0]


@router.get("/{request_id}/attachments/{attachment_id}")
async def download_attachment(request_id: uuid.UUID, attachment_id: uuid.UUID, service: Requests, settings: AppSettings) -> FileResponse:
    """Download a request's file; only for people who can read the request."""
    await service.get_readable(request_id)
    row = await service.db.get(RequestAttachment, attachment_id)
    if row is None or row.request_id != request_id:
        raise NotFoundError("Attachment not found.")
    return FileResponse(
        request_attachments.storage(settings).path_for(row.storage_key), media_type=row.content_type, filename=row.filename,
        content_disposition_type="attachment", headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


@router.post("/{request_id}/approval-decision", response_model=ServiceRequestOut)
async def decide_approval_step(request_id: uuid.UUID, payload: ApprovalDecision, service: Requests) -> ServiceRequestOut:
    request = await service.get_readable(request_id, for_update=True)
    await decide(service.db, service.user, service.ctx, request, payload.decision, payload.comment)
    await service.db.refresh(request)
    return (await _render_many(service, [request]))[0]
