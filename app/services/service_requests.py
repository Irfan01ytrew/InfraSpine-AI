"""Employee self-service requests with explicit HR confidentiality and department triage ACLs."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.approval_models import FINANCIAL_CATEGORIES
from app.db.models import (
    ROLE_ADMIN,
    ROLE_MANAGER,
    AccessGroup,
    AccessGroupMember,
    DepartmentManager,
    ServiceRequest,
    ServiceRequestEvent,
    ServiceRequestNotification,
    User,
)
from app.services import audit
from app.services.audit import AuditContext
from app.services.approval_authority import check_approval
from app.services.budgets import check_budgets_for_approval
from app.services.approval_chains import attach_steps, cancel_steps, is_step_reader, request_steps, step_reader_condition
from app.services.automation import enqueue_service_request_workflows
from app.services.request_forms import answers_as_text, can_use_form, validate_answers
from app.services import request_attachments, webhooks
from app.db.form_models import RequestForm

_OPEN_STATUSES = {"submitted", "acknowledged", "in_progress", "waiting_requester"}
_TERMINAL_STATUSES = {"resolved", "rejected", "cancelled"}
RESPONSE_SLA = {
    "urgent": timedelta(hours=1),
    "high": timedelta(hours=4),
    "normal": timedelta(hours=24),
    "low": timedelta(hours=72),
}
_TRANSITIONS = {
    "submitted": {"acknowledged", "in_progress", "waiting_requester", "resolved", "rejected"},
    "acknowledged": {"in_progress", "waiting_requester", "resolved", "rejected"},
    "in_progress": {"waiting_requester", "resolved", "rejected"},
    "waiting_requester": {"in_progress", "resolved", "rejected"},
}


class ServiceRequestService:
    def __init__(self, db: AsyncSession, user: User, ctx: AuditContext) -> None:
        self.db = db
        self.user = user
        self.ctx = ctx
        self._managed_groups: set[uuid.UUID] | None = None

    async def managed_department_ids(self) -> set[uuid.UUID]:
        if self._managed_groups is None:
            if self.user.role != ROLE_MANAGER:
                self._managed_groups = set()
            else:
                rows = await self.db.scalars(
                    select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == self.user.id)
                )
                self._managed_groups = set(rows)
        return self._managed_groups

    async def can_manage(self, request: ServiceRequest) -> bool:
        if self.user.role == ROLE_ADMIN:
            return True
        if self.user.role != ROLE_MANAGER or request.category == "hr" or request.department_group_id is None:
            return False
        return request.department_group_id in await self.managed_department_ids()

    async def assigned_department_ids(self) -> set[uuid.UUID]:
        """Departments where the current active user still has membership for assigned work."""
        if not self.user.is_active:
            return set()
        member_rows = await self.db.scalars(
            select(AccessGroupMember.group_id).where(AccessGroupMember.user_id == self.user.id)
        )
        manager_rows = await self.db.scalars(
            select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == self.user.id)
        )
        return set(member_rows).union(manager_rows)

    async def can_read_assigned(self, request: ServiceRequest) -> bool:
        if (
            not self.user.is_active
            or request.assigned_to_user_id != self.user.id
            or request.category == "hr"
            or request.department_group_id is None
        ):
            return False
        member = await self.db.get(AccessGroupMember, (request.department_group_id, self.user.id))
        manager = await self.db.get(DepartmentManager, (request.department_group_id, self.user.id))
        return member is not None or manager is not None

    async def list_readable(self) -> list[ServiceRequest]:
        query = select(ServiceRequest)
        if self.user.role != ROLE_ADMIN:
            requester = ServiceRequest.requester_user_id == self.user.id
            conditions = [requester]
            managed = await self.managed_department_ids()
            if managed:
                conditions.append(
                    and_(
                        ServiceRequest.category != "hr",
                        ServiceRequest.department_group_id.in_(managed),
                    )
                )
            assigned_departments = await self.assigned_department_ids()
            if assigned_departments:
                conditions.append(
                    and_(
                        ServiceRequest.assigned_to_user_id == self.user.id,
                        ServiceRequest.category != "hr",
                        ServiceRequest.department_group_id.in_(assigned_departments),
                    )
                )
            conditions.append(step_reader_condition(self.user))
            query = query.where(or_(*conditions))
        result = await self.db.scalars(query.order_by(ServiceRequest.updated_at.desc(), ServiceRequest.id).limit(100))
        return list(result)

    async def get_readable(self, request_id: uuid.UUID, *, for_update: bool = False) -> ServiceRequest:
        query = select(ServiceRequest).where(ServiceRequest.id == request_id)
        if for_update:
            query = query.with_for_update()
        request = await self.db.scalar(query)
        if request is None or (
            request.requester_user_id != self.user.id
            and not await self.can_manage(request)
            and not await self.can_read_assigned(request)
            and not await is_step_reader(self.db, self.user, request.id)
        ):
            # Conceal the existence of out-of-scope requests, especially HR records.
            raise NotFoundError("Service request not found.")
        return request

    async def create(
        self,
        *,
        category: str,
        priority: str,
        department_group_id: uuid.UUID | None,
        subject: str,
        description: str,
        amount: Decimal | None = None,
        currency: str | None = None,
        form_id: uuid.UUID | None = None,
        answers: dict | None = None,
    ) -> ServiceRequest:
        form, form_answers, claimed_files = None, None, []
        if form_id is not None:
            form = await self.db.get(RequestForm, form_id)
            if form is None or not form.is_active or not await can_use_form(self.db, self.user, form):
                raise InvalidInputError("This form is no longer available; choose another.")
            category = form.category
            form_answers = validate_answers(form.fields, answers or {})
            for entry in form_answers:
                if entry["type"] == "file":
                    rows = await request_attachments.claim(self.db, self.user, entry["value"])
                    claimed_files.append((entry["key"], rows))
                    entry["value"] = ", ".join(row.filename for row in rows)
                    entry["attachments"] = [{"id": str(row.id), "filename": row.filename, "size_bytes": row.size_bytes,
                                             "content_type": row.content_type} for row in rows]
            if not description.strip():
                description = answers_as_text(form_answers) or form.name
        elif answers:
            raise InvalidInputError("Answers need a form.")
        financial = category in FINANCIAL_CATEGORIES
        if financial:
            if amount is None or amount <= 0:
                raise InvalidInputError("Enter the amount for a purchase request or expense claim.")
            currency = currency or _organization_currency()
            if not currency:
                raise InvalidInputError("Choose a currency, or set the company currency in Company setup.")
        elif amount is not None:
            raise InvalidInputError("Only purchase requests and expense claims have an amount.")
        else:
            currency = None
        group = None
        if form is not None and form.department_group_id is not None:
            # The form routes to its department; the requester need not belong to it.
            group = await self.db.get(AccessGroup, form.department_group_id)
            department_group_id = None
        if department_group_id is not None:
            group = await self.db.scalar(
                select(AccessGroup).where(
                    AccessGroup.id == department_group_id,
                    AccessGroup.kind == "department",
                    AccessGroup.source == "local",
                )
            )
            if group is None:
                raise InvalidInputError("Select an existing local department.")
            if self.user.role != ROLE_ADMIN:
                membership = await self.db.get(AccessGroupMember, (group.id, self.user.id))
                managed = await self.db.get(DepartmentManager, (group.id, self.user.id))
                if membership is None and managed is None:
                    raise PermissionDeniedError("You can only submit for a department you belong to or manage.")

        clean_subject = " ".join(subject.split())
        clean_description = description.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not clean_subject or len(clean_subject) > 160 or not clean_description or len(clean_description) > 8000:
            raise InvalidInputError("Add a subject and request details within the field limits.")

        request = ServiceRequest(
            requester_user_id=self.user.id,
            department_group_id=group.id if group else None,
            category=category,
            priority=priority,
            subject=clean_subject,
            description=clean_description,
            amount=amount if financial else None,
            currency=currency,
            form_id=form.id if form else None,
            form_name=form.name if form else None,
            form_version=form.version if form else None,
            form_answers=form_answers,
            created_at=(now := datetime.now(UTC)),
            updated_at=now,
            response_due_at=now + RESPONSE_SLA[priority],
        )
        self.db.add(request)
        await self.db.flush()
        for field_key, rows in claimed_files:
            request_attachments.link(rows, request.id, field_key)
        await attach_steps(self.db, request, self.user)
        self.db.add(
            ServiceRequestEvent(
                request_id=request.id,
                actor_user_id=self.user.id,
                actor_email=self.user.email,
                action="submitted",
                to_status="submitted",
            )
        )
        if category != "hr" and group is not None:
            await enqueue_service_request_workflows(
                self.db,
                request_id=request.id,
                department_group_id=group.id,
                requester_user_id=self.user.id,
                now=now,
            )
        await webhooks.request_event(self.db, request, "request.submitted")
        audit.record(self.db, self.ctx, "service_request.submitted", target=str(request.id),
                     details={"form": form.name, "form_version": form.version} if form else None)
        await self.db.commit()
        await self.db.refresh(request)
        return request

    async def comment(self, request_id: uuid.UUID, comment: str) -> ServiceRequest:
        request = await self.get_readable(request_id, for_update=True)
        clean = comment.strip()
        if not clean or len(clean) > 2000:
            raise InvalidInputError("Add a comment within the 2,000 character limit.")
        if request.status == "cancelled":
            raise ConflictError("Cancelled requests cannot be updated.")
        self.db.add(
            ServiceRequestEvent(
                request_id=request.id,
                actor_user_id=self.user.id,
                actor_email=self.user.email,
                action="commented",
                from_status=request.status,
                to_status=request.status,
                comment=clean,
            )
        )
        now = datetime.now(UTC)
        request.updated_at = now
        if (
            request.status == "submitted"
            and request.response_sla_met_at is None
            and (await self.can_manage(request) or await self.can_read_assigned(request))
        ):
            request.response_sla_met_at = now
        audit.record(self.db, self.ctx, "service_request.comment_added", target=str(request.id))
        await self.db.commit()
        await self.db.refresh(request)
        return request

    async def triage(
        self,
        request_id: uuid.UUID,
        *,
        status: str,
        assigned_to_user_id: uuid.UUID | None,
        comment: str | None,
    ) -> ServiceRequest:
        request = await self.get_readable(request_id, for_update=True)
        if not await self.can_manage(request):
            raise NotFoundError("Service request not found.")

        clean_comment = comment.strip() if comment else None
        if clean_comment == "":
            clean_comment = None
        if clean_comment is not None and len(clean_comment) > 2000:
            raise InvalidInputError("Comments must be 2,000 characters or fewer.")
        if status in _TERMINAL_STATUSES and not clean_comment:
            raise InvalidInputError("Provide a short resolution or rejection note.")
        if status != request.status and status not in _TRANSITIONS.get(request.status, set()):
            raise ConflictError("This service request can no longer move to that status.")
        approval = None
        if status in _TERMINAL_STATUSES and status != request.status and request.category in FINANCIAL_CATEGORIES:
            if await request_steps(self.db, request.id):
                raise ConflictError("This request follows an approval chain; approve or reject it at its current step.")
        if status == "resolved" and status != request.status and request.category in FINANCIAL_CATEGORIES:
            # Approving money needs approval authority, not just department management (separation of duties).
            approval = await check_approval(self.db, self.user, request)
            if not approval.can_approve:
                raise PermissionDeniedError(approval.reason or "You cannot approve this request.")
            budget_details = await check_budgets_for_approval(self.db, request)
        if status == request.status and assigned_to_user_id == request.assigned_to_user_id and clean_comment is None:
            raise ConflictError("Choose a status, assignee, or comment change.")

        assigned_email = None
        if assigned_to_user_id is not None:
            assignee = await self.db.get(User, assigned_to_user_id)
            if assignee is None or not assignee.is_active:
                raise InvalidInputError("Choose an active assignee.")
            if request.category == "hr":
                if self.user.role != ROLE_ADMIN or assignee.role != ROLE_ADMIN:
                    raise InvalidInputError("HR requests can only be assigned to an administrator.")
            elif assignee.role != ROLE_ADMIN:
                if request.department_group_id is None:
                    raise InvalidInputError("Requests without a department can only be assigned to an administrator.")
                member = await self.db.get(AccessGroupMember, (request.department_group_id, assignee.id))
                manager = await self.db.get(DepartmentManager, (request.department_group_id, assignee.id))
                if member is None and manager is None:
                    raise InvalidInputError("The assignee must belong to or manage the request department.")
            assigned_email = assignee.email

        old_status = request.status
        now = datetime.now(UTC)
        request.status = status
        request.assigned_to_user_id = assigned_to_user_id
        request.updated_at = now
        if old_status == "submitted" and status != "submitted" and request.response_sla_met_at is None:
            request.response_sla_met_at = now
        request.closed_at = request.updated_at if status in _TERMINAL_STATUSES else None
        action = "status_changed" if status != old_status else "commented"
        self.db.add(
            ServiceRequestEvent(
                request_id=request.id,
                actor_user_id=self.user.id,
                actor_email=self.user.email,
                action=action,
                from_status=old_status,
                to_status=status,
                comment=clean_comment,
            )
        )
        audit.record(
            self.db,
            self.ctx,
            "service_request.triaged",
            target=str(request.id),
            details={"status": status, "assigned_to": assigned_email},
        )
        if approval is not None:
            audit.record(
                self.db,
                self.ctx,
                "service_request.approved",
                target=str(request.id),
                details={
                    "category": request.category, "amount": str(request.amount), "currency": request.currency,
                    "limit": str(approval.limit), "limit_source": approval.limit_source, **budget_details,
                },
            )
        if status != old_status:
            await webhooks.request_event(self.db, request, "request.status_changed", from_status=old_status)
            if approval is not None:
                await webhooks.request_event(self.db, request, "request.approved", decided_by=self.user.email)
            elif status == "rejected":
                await webhooks.request_event(self.db, request, "request.rejected", decided_by=self.user.email)
        await self.db.commit()
        await self.db.refresh(request)
        return request

    async def list_notifications(self) -> list[ServiceRequestNotification]:
        """Return only the recipient's notifications whose request is still readable now."""
        rows = list(
            await self.db.scalars(
                select(ServiceRequestNotification)
                .where(ServiceRequestNotification.recipient_user_id == self.user.id)
                .order_by(ServiceRequestNotification.created_at.desc(), ServiceRequestNotification.id)
                .limit(200)
            )
        )
        if not rows:
            return []
        requests = {
            item.id: item
            for item in await self.db.scalars(
                select(ServiceRequest).where(ServiceRequest.id.in_({row.request_id for row in rows}))
            )
        }
        visible = []
        for row in rows:
            request = requests.get(row.request_id)
            if request is None:
                continue
            if (
                request.requester_user_id == self.user.id
                or await self.can_manage(request)
                or await self.can_read_assigned(request)
                or await is_step_reader(self.db, self.user, request.id)
            ):
                visible.append(row)
        return visible

    async def mark_notification_read(self, notification_id: uuid.UUID) -> ServiceRequestNotification:
        row = await self.db.scalar(
            select(ServiceRequestNotification)
            .where(
                ServiceRequestNotification.id == notification_id,
                ServiceRequestNotification.recipient_user_id == self.user.id,
            )
            .with_for_update()
        )
        if row is None:
            raise NotFoundError("Notification not found.")
        # Department membership may have been revoked since the notification was created.
        await self.get_readable(row.request_id)
        if row.read_at is None:
            row.read_at = datetime.now(UTC)
            await self.db.commit()
            await self.db.refresh(row)
        return row

    async def cancel(self, request_id: uuid.UUID) -> ServiceRequest:
        request = await self.get_readable(request_id, for_update=True)
        if request.requester_user_id != self.user.id:
            raise NotFoundError("Service request not found.")
        cancellable = {"submitted", "acknowledged"} | ({"in_progress"} if request.category in FINANCIAL_CATEGORIES else set())
        if request.status not in cancellable:
            raise ConflictError("Only a new or acknowledged request can be cancelled.")
        old_status = request.status
        await cancel_steps(self.db, request.id)
        request.status = "cancelled"
        request.updated_at = datetime.now(UTC)
        request.closed_at = request.updated_at
        self.db.add(
            ServiceRequestEvent(
                request_id=request.id,
                actor_user_id=self.user.id,
                actor_email=self.user.email,
                action="cancelled",
                from_status=old_status,
                to_status="cancelled",
            )
        )
        await webhooks.request_event(self.db, request, "request.cancelled", from_status=old_status)
        audit.record(self.db, self.ctx, "service_request.cancelled", target=str(request.id))
        await self.db.commit()
        await self.db.refresh(request)
        return request


def _organization_currency() -> str | None:
    """The company currency from Company setup, used when a financial request names none."""
    from app.core.config import get_settings
    from app.services.deployment import get_company_profile

    try:
        return get_company_profile(get_settings()).currency
    except Exception:
        return None
