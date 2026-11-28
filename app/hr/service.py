"""HR service: leave requests on the unified engine, balances, roles (Step AG, slices 2-3).

Every lookup is filtered by the department (access group) and authorised on that
department's platform scope; a record from another department is "not found".
A leave request is a workflow run on the unified engine (app/hr/workflow.py):
employee requests (`hr.leave.request`), department manager approves
(`hr.leave.approve`) with an approval bound to the exact action, then the
balance is updated through `execute_once`. Nobody approves their own leave.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import AccessGroup, AccessGroupMember, DepartmentManager, User
from app.hr.leave import LeaveBalance, LeavePolicy, check_request, count_business_days
from app.hr.models import ATTENDANCE_STATUSES, HrAttendance, HrLeaveBalance, HrLeavePolicy, HrLeaveRequest, HrPayrollSchedule, HrSalaryReminder, LEAVE_TYPES
from app.hr.workflow import (
    RUN_ORIGIN, _spec, attach_executor, execute_leave, guard, mirror_leave_request,
)
from app.platform.access import (
    effective_permissions, ensure_role, grant_membership, principal_for, require, visible_scopes,
)
from app.platform.approvals import decide_approval, request_approval
from app.platform.events import audit
from app.platform.models import PlatformApprovalRequest, PlatformMembership, PlatformRole, PlatformScope
from app.platform.sync import group_scope_sync
from app.platform.timezone import org_today
from app.workflows.unified import origin_run, wake_origin_run

HR_ROLES = ('hr.employee', 'hr.manager', 'hr.admin')
ROLE_PERMISSIONS = {  # recreated on demand if missing (same pattern as finance)
    'hr.employee': {'hr.leave.request', 'hr.attendance.record'},
    'hr.manager': {'hr.leave.request', 'hr.leave.approve', 'hr.attendance.record'},
    'hr.admin': {'hr.leave.request', 'hr.leave.approve', 'hr.attendance.record', 'hr.salary.remind', 'hr.manage'},
}
HR_PERMISSIONS = ('hr.leave.request', 'hr.leave.approve', 'hr.attendance.record', 'hr.salary.remind', 'hr.manage')

# Owner-proposed defaults (plan section 7) used when a department has no policy yet.
DEFAULT_POLICIES = {
    'annual': {'entitlement_days': Decimal('20'), 'carry_forward_cap': Decimal('5'),
               'max_consecutive_days': Decimal('10'), 'accrual': 'upfront'},
    'sick': {'entitlement_days': Decimal('10'), 'carry_forward_cap': Decimal('0'),
             'max_consecutive_days': Decimal('10'), 'accrual': 'upfront'},
    'casual': {'entitlement_days': Decimal('8'), 'carry_forward_cap': Decimal('0'),
              'max_consecutive_days': Decimal('5'), 'accrual': 'upfront'},
}
DEFAULT_WEEKEND_DAYS = '5,6'  # Saturday + Sunday (Python weekday numbers)


def _parse_weekend_days(raw: str) -> frozenset:
    try:
        days = frozenset(int(part) for part in raw.split(',') if part.strip() != '')
    except ValueError:
        return frozenset({5, 6})
    return days if days and all(0 <= d <= 6 for d in days) else frozenset({5, 6})


def _to_policy(row: HrLeavePolicy) -> LeavePolicy:
    return LeavePolicy(
        leave_type=row.leave_type, entitlement_days=float(row.entitlement_days),
        carry_forward_cap=float(row.carry_forward_cap),
        max_consecutive_days=None if row.max_consecutive_days is None else float(row.max_consecutive_days),
        accrual=row.accrual, weekend_days=_parse_weekend_days(row.weekend_days))


class HrService:
    def __init__(self, db, user: User):
        self.db, self.user = db, user
        self.p = principal_for(user)

    # ------------------------------------------------------------------ scope and permission

    async def scopes(self) -> list[dict]:
        """Departments where the caller holds any HR permission (HR page picker).

        Mirrors the finance scopes endpoint: departments the caller can read,
        each with the caller's HR permissions on it.
        """
        flt = await visible_scopes(self.db, self.p, 'hr.leave.request')
        group_ids = [g for g in (await self.db.scalars(select(PlatformScope.legacy_id).where(
            PlatformScope.legacy_table == 'access_groups', flt.sql(PlatformScope.id)))).all()
            if g is not None]
        out = []
        for group in (await self.db.scalars(select(AccessGroup).where(
                AccessGroup.id.in_(group_ids or [uuid.uuid4()]), AccessGroup.kind == 'department')
                .order_by(AccessGroup.name))).all():
            scope = await self.platform_scope(group.id)
            perms = await effective_permissions(self.db, self.user.id, scope)
            out.append({'id': str(group.id), 'name': group.name,
                        'permissions': sorted(p for p in HR_PERMISSIONS if p in perms or '*' in perms)})
        return out

    async def platform_scope(self, group_id) -> uuid.UUID:
        group = await self.db.get(AccessGroup, group_id)
        if group is None or group.kind != 'department':
            raise NotFoundError('Department not found.')
        scope = await self.db.run_sync(lambda s: group_scope_sync(s.connection(), group_id))
        if scope is None:
            raise NotFoundError('Department not found.')
        return scope

    async def require(self, group_id, permission: str) -> uuid.UUID:
        scope = await self.platform_scope(group_id)
        await require(self.db, self.p, permission, scope)
        return scope

    async def _is_manager(self, group_id) -> bool:
        row = await self.db.scalar(select(DepartmentManager).where(
            DepartmentManager.group_id == group_id, DepartmentManager.manager_user_id == self.user.id))
        if row is not None:
            return True
        perms = await effective_permissions(self.db, self.user.id, await self.platform_scope(group_id))
        return 'hr.manage' in perms

    async def _require_manager(self, group_id) -> uuid.UUID:
        scope = await self.require(group_id, 'hr.leave.approve')
        if not await self._is_manager(group_id):
            raise PermissionDeniedError('Only a manager of this department can do this.')
        return scope

    # ------------------------------------------------------------------ roles (platform memberships)

    async def roles(self, group_id) -> list[dict]:
        scope = await self.require(group_id, 'hr.leave.request')
        rows = (await self.db.execute(select(PlatformMembership, PlatformRole.key, User.email)
                                      .join(PlatformRole, PlatformRole.id == PlatformMembership.role_id)
                                      .join(User, User.id == PlatformMembership.user_id)
                                      .where(PlatformMembership.scope_id == scope, PlatformMembership.revoked_at.is_(None),
                                             PlatformRole.key.in_(HR_ROLES))
                                      .order_by(User.email))).all()
        return [{'id': str(m.id), 'user_id': str(m.user_id), 'email': email, 'role': key,
                 'valid_to': m.valid_to.isoformat() if m.valid_to else None} for m, key, email in rows]

    async def grant_role(self, group_id, *, user_id, role: str, valid_to=None):
        if role not in HR_ROLES:
            raise InvalidInputError('Unknown HR role.')
        scope = await self.platform_scope(group_id)
        person = await self.db.get(User, user_id)
        if person is None or not person.is_active:
            raise InvalidInputError('Choose an active person.')
        await require(self.db, self.p, 'platform.manage', scope)
        await ensure_role(self.db, role, ROLE_PERMISSIONS[role], name=role.replace('.', ' ').title(), system=True)
        row = await grant_membership(self.db, self.p, user_id=user_id, scope_id=scope, role_key=role, valid_to=valid_to)
        audit(self.db, self.p, 'hr.role_granted', target=str(row.id),
              details={'group_id': str(group_id), 'user_id': str(user_id), 'role': role,
                       'valid_to': valid_to.isoformat() if valid_to else None})
        return row

    # ------------------------------------------------------------------ policies and balances

    async def ensure_policy(self, group_id, leave_type: str) -> HrLeavePolicy:
        if leave_type not in LEAVE_TYPES:
            raise InvalidInputError(f'Unknown leave type: {leave_type}.')
        row = await self.db.scalar(select(HrLeavePolicy).where(
            HrLeavePolicy.group_id == group_id, HrLeavePolicy.leave_type == leave_type))
        if row is None:
            defaults = DEFAULT_POLICIES[leave_type]
            row = HrLeavePolicy(group_id=group_id, leave_type=leave_type, weekend_days=DEFAULT_WEEKEND_DAYS, **defaults)
            self.db.add(row)
            await self.db.flush()
        return row

    async def compute_balance(self, group_id, employee_id, leave_type: str, year: int) -> LeaveBalance:
        """Balance from leave records: entitled + carried - used - pending (never from input)."""
        start, end = date(year, 1, 1), date(year, 12, 31)
        rows = (await self.db.scalars(select(HrLeaveRequest).where(
            HrLeaveRequest.group_id == group_id, HrLeaveRequest.employee_id == employee_id,
            HrLeaveRequest.leave_type == leave_type, HrLeaveRequest.start_date >= start,
            HrLeaveRequest.start_date <= end, HrLeaveRequest.status.in_(('approved', 'pending'))))).all()
        used = sum((r.days for r in rows if r.status == 'approved'), Decimal('0'))
        pending = sum((r.days for r in rows if r.status == 'pending'), Decimal('0'))
        cache = await self.db.scalar(select(HrLeaveBalance).where(
            HrLeaveBalance.group_id == group_id, HrLeaveBalance.employee_id == employee_id,
            HrLeaveBalance.leave_type == leave_type, HrLeaveBalance.year == year))
        policy = await self.ensure_policy(group_id, leave_type)
        entitled = Decimal(str(policy.entitlement_days))
        carried = cache.carried if cache is not None else Decimal('0')
        return LeaveBalance(entitled=float(entitled), carried=float(carried), used=float(used), pending=float(pending))

    async def balance_for(self, group_id, employee_id, leave_type: str, year: int) -> dict:
        await self.require(group_id, 'hr.leave.request')
        balance = await self.compute_balance(group_id, employee_id, leave_type, year)
        return {'employee_id': str(employee_id), 'leave_type': leave_type, 'year': year,
                'entitled': balance.entitled, 'carried': balance.carried, 'used': balance.used,
                'pending': balance.pending, 'remaining': balance.remaining}

    # ------------------------------------------------------------------ leave requests

    def _out(self, row: HrLeaveRequest) -> dict:
        return {'id': str(row.id), 'group_id': str(row.group_id), 'employee_id': str(row.employee_id),
                'leave_type': row.leave_type, 'start_date': row.start_date.isoformat(),
                'end_date': row.end_date.isoformat(), 'half_day': row.half_day,
                'days': format(row.days.normalize(), 'f'), 'status': row.status,
                'workflow_run_id': str(row.workflow_run_id) if row.workflow_run_id else None,
                'approval_id': str(row.approval_id) if row.approval_id else None,
                'decided_by': str(row.decided_by) if row.decided_by else None,
                'decided_at': row.decided_at.isoformat() if row.decided_at else None,
                'applied_at': row.applied_at.isoformat() if row.applied_at else None,
                'created_at': row.created_at.isoformat() if row.created_at else None}

    async def _get_visible(self, group_id, request_id) -> HrLeaveRequest:
        row = await self.db.get(HrLeaveRequest, request_id)
        if row is None or row.group_id != group_id:
            raise NotFoundError('Leave request not found.')
        return row

    async def request_leave(self, group_id, *, leave_type: str, start: date, end: date,
                            half_day: bool = False) -> HrLeaveRequest:
        scope = await self.require(group_id, 'hr.leave.request')
        policy_row = await self.ensure_policy(group_id, leave_type)
        year = start.year
        balance = await self.compute_balance(group_id, self.user.id, leave_type, year)
        existing = (await self.db.scalars(select(HrLeaveRequest).where(
            HrLeaveRequest.group_id == group_id, HrLeaveRequest.employee_id == self.user.id,
            HrLeaveRequest.leave_type == leave_type, HrLeaveRequest.status.in_(('approved', 'pending'))))).all()
        ranges = tuple((r.start_date, r.end_date) for r in existing)
        decision = check_request(_to_policy(policy_row), balance, start, end, today=await org_today(self.db),
                                 holidays=frozenset(), existing=ranges, half_day=half_day)
        if not decision.allowed:
            raise InvalidInputError('Leave request not allowed.', details={'reasons': list(decision.reasons)})
        row = HrLeaveRequest(group_id=group_id, employee_id=self.user.id, leave_type=leave_type,
                             start_date=start, end_date=end, half_day=half_day, days=Decimal(str(decision.days)))
        self.db.add(row)
        await self.db.flush()
        run = await mirror_leave_request(self.db, row)
        if run is not None:
            row.workflow_run_id = run.id
        approval = await request_approval(
            self.db, self.p, _spec(row, scope), required_permission='hr.leave.approve', ttl=timedelta(days=2))
        row.approval_id = approval.id
        audit(self.db, self.p, 'hr.leave_requested', target=str(row.id),
              details={'leave_type': leave_type, 'start_date': start.isoformat(), 'end_date': end.isoformat(),
                       'days': format(row.days.normalize(), 'f'), 'approval_id': str(approval.id),
                       'workflow_run_id': str(row.workflow_run_id) if row.workflow_run_id else None})
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def decide_leave(self, group_id, request_id, *, decision: str, presented_hash: str,
                           note: str = '') -> HrLeaveRequest:
        row = await self._get_visible(group_id, request_id)
        await self._require_manager(group_id)
        if row.status != 'pending':
            raise ConflictError('This leave request is no longer pending.')
        if row.approval_id is None:
            raise ConflictError('This leave request has no approval waiting.')
        approval = await decide_approval(self.db, self.p, row.approval_id, decision,
                                         presented_hash=presented_hash, note=note)
        row = await self.db.get(HrLeaveRequest, request_id, populate_existing=True)
        row.status = 'approved' if approval.status == 'approved' else 'rejected'
        row.decided_by, row.decided_at = approval.decided_by, approval.decided_at
        audit(self.db, self.p, f'hr.leave_{row.status}', target=str(row.id),
              details={'approval_id': str(approval.id), 'decided_by': str(approval.decided_by)})
        await wake_origin_run(self.db, RUN_ORIGIN, row.id)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def execute_leave_request(self, group_id, request_id) -> HrLeaveRequest:
        row = await self._get_visible(group_id, request_id)
        await self._require_manager(group_id)
        run = await guard(self.db, row)
        row = await execute_leave(self.db, self.user, row, run)
        if run is not None:
            await attach_executor(self.db, run, self.user)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def cancel_leave(self, group_id, request_id) -> HrLeaveRequest:
        row = await self._get_visible(group_id, request_id)
        is_requester = row.employee_id == self.user.id
        if is_requester:
            await self.require(group_id, 'hr.leave.request')
        else:
            await self._require_manager(group_id)
        if row.status != 'pending':
            raise ConflictError('Only a pending leave request can be cancelled.')
        row.status = 'cancelled'
        if row.approval_id is not None:
            approval = await self.db.get(PlatformApprovalRequest, row.approval_id, populate_existing=True)
            if approval is not None and approval.status == 'pending':
                approval.status, approval.decided_at = 'cancelled', datetime.now(UTC)
        audit(self.db, self.p, 'hr.leave_cancelled', target=str(row.id), details={})
        await wake_origin_run(self.db, RUN_ORIGIN, row.id)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def get_leave(self, group_id, request_id) -> HrLeaveRequest:
        row = await self._get_visible(group_id, request_id)
        if row.employee_id == self.user.id:
            await self.require(group_id, 'hr.leave.request')
        else:
            await self._require_manager(group_id)
        return row

    async def list_my_leave(self, group_id) -> list[HrLeaveRequest]:
        await self.require(group_id, 'hr.leave.request')
        return (await self.db.scalars(select(HrLeaveRequest).where(
            HrLeaveRequest.group_id == group_id, HrLeaveRequest.employee_id == self.user.id)
            .order_by(HrLeaveRequest.created_at.desc()).limit(200))).all()

    async def list_team_leave(self, group_id) -> list[HrLeaveRequest]:
        await self._require_manager(group_id)
        return (await self.db.scalars(select(HrLeaveRequest).where(HrLeaveRequest.group_id == group_id)
                                      .order_by(HrLeaveRequest.created_at.desc()).limit(200))).all()

    async def pending_leave_approvals(self, group_id) -> list[dict]:
        """Pending leave approvals for the department, with the hash the manager must present.

        Manager only. Returns one row per pending platform approval whose subject is a
        leave request in this department, joined to the request itself — the page's
        Approve/Reject buttons present this row's action_hash to the decide endpoint.
        """
        await self._require_manager(group_id)
        scope = await self.platform_scope(group_id)
        rows = (await self.db.execute(select(PlatformApprovalRequest)
                                      .where(PlatformApprovalRequest.scope_id == scope,
                                             PlatformApprovalRequest.status == 'pending',
                                             PlatformApprovalRequest.subject.like('leave_request:%'))
                                      .order_by(PlatformApprovalRequest.created_at))).scalars().all()
        out = []
        for approval in rows:
            try:
                request_id = uuid.UUID(approval.subject.split(':', 1)[1])
            except (ValueError, IndexError):
                continue
            leave = await self.db.get(HrLeaveRequest, request_id)
            if leave is None or leave.group_id != group_id:
                continue
            email = await self.db.scalar(select(User.email).where(User.id == leave.employee_id))
            out.append({'approval_id': str(approval.id), 'request_id': str(leave.id),
                        'employee_id': str(leave.employee_id), 'email': email,
                        'leave_type': leave.leave_type,
                        'start_date': leave.start_date.isoformat(), 'end_date': leave.end_date.isoformat(),
                        'days': format(leave.days.normalize(), 'f') if leave.days is not None else None,
                        'action_hash': approval.action_hash,
                        'expires_at': approval.expires_at.isoformat() if approval.expires_at else None})
        return out

    # ------------------------------------------------------------------ attendance (slice 4)

    @staticmethod
    def _attendance_out(row: HrAttendance) -> dict:
        return {'id': str(row.id), 'group_id': str(row.group_id), 'employee_id': str(row.employee_id),
                'date': row.date.isoformat(), 'status': row.status,
                'check_in': row.check_in.isoformat() if row.check_in else None,
                'check_out': row.check_out.isoformat() if row.check_out else None,
                'created_at': row.created_at.isoformat() if row.created_at else None}

    @staticmethod
    def _month_bounds(year: int, month: int) -> tuple[date, date]:
        if not (1 <= month <= 12) or not (2000 <= year <= 2100):
            raise InvalidInputError('Choose a valid month.')
        first = date(year, month, 1)
        return first, date(year, month, calendar.monthrange(year, month)[1])

    async def _weekend_days(self, group_id) -> frozenset:
        """The department's weekend days (annual policy row, else the Sat+Sun default).

        Read-only: unlike ensure_policy, this never creates a policy row.
        """
        row = await self.db.scalar(select(HrLeavePolicy).where(
            HrLeavePolicy.group_id == group_id, HrLeavePolicy.leave_type == 'annual'))
        if row is None:
            return frozenset({5, 6})
        return _parse_weekend_days(row.weekend_days)

    async def record_attendance(self, group_id, *, employee_id=None, day: date, status: str,
                                check_in: time | None = None, check_out: time | None = None) -> HrAttendance:
        """Record (or correct) one day's attendance. One row per employee per day.

        An employee records their own day (`hr.attendance.record`); a department
        manager records for a member of their own department. Attendance is a
        record, not surveillance: dates and check-in/out times only.
        """
        if status not in ATTENDANCE_STATUSES:
            raise InvalidInputError(f'Unknown attendance status: {status}.')
        if day > await org_today(self.db):
            raise InvalidInputError('Attendance cannot be recorded for a future date.')
        if status in ('leave', 'holiday') and (check_in is not None or check_out is not None):
            raise InvalidInputError('Check-in/out times do not apply to leave or holiday days.')
        if check_in is not None and check_out is not None and check_out <= check_in:
            raise InvalidInputError('Check-out must be after check-in.')
        target = employee_id or self.user.id
        if target == self.user.id:
            await self.require(group_id, 'hr.attendance.record')
        else:
            await self._require_manager(group_id)
            person = await self.db.get(User, target)
            if person is None or not person.is_active:
                raise InvalidInputError('Choose an active person.')
            member = await self.db.scalar(select(AccessGroupMember).where(
                AccessGroupMember.group_id == group_id, AccessGroupMember.user_id == target))
            if member is None:
                raise InvalidInputError('That person is not a member of this department.')
        row = await self.db.scalar(select(HrAttendance).where(
            HrAttendance.group_id == group_id, HrAttendance.employee_id == target, HrAttendance.date == day))
        if row is None:
            row = HrAttendance(group_id=group_id, employee_id=target, date=day, status=status,
                               check_in=check_in, check_out=check_out)
            self.db.add(row)
        else:
            row.status, row.check_in, row.check_out = status, check_in, check_out
        audit(self.db, self.p, 'hr.attendance_recorded', target=str(row.id),
              details={'group_id': str(group_id), 'employee_id': str(target),
                       'date': day.isoformat(), 'status': status})
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def list_attendance(self, group_id, *, employee_id=None, year: int, month: int) -> list[HrAttendance]:
        """One employee's attendance rows for a month (own, or a manager's team member)."""
        first, last = self._month_bounds(year, month)
        if employee_id is not None and employee_id != self.user.id:
            await self._require_manager(group_id)
            target = employee_id
        else:
            await self.require(group_id, 'hr.attendance.record')
            target = self.user.id
        return (await self.db.scalars(select(HrAttendance).where(
            HrAttendance.group_id == group_id, HrAttendance.employee_id == target,
            HrAttendance.date >= first, HrAttendance.date <= last)
            .order_by(HrAttendance.date.desc()))).all()

    async def attendance_summary(self, group_id, *, employee_id=None, year: int, month: int) -> dict:
        """Monthly attendance summary.

        Own summary for an employee; a manager gets the whole team (members
        with attendance rows or approved leave in the month). Counts are
        business days only (weekend days come from the department's annual
        policy, default Sat+Sun). A business day that has already passed with
        neither a record nor approved leave is reported as ``unrecorded`` —
        never as "absent"; future days are not counted as missing.
        Approved leave days covered by an attendance row are counted once.
        "Approved" here means status ``approved`` (manager said yes) or
        ``applied`` (the balance was already updated) — both are real leave.
        No holiday table exists yet, so holidays come only from attendance
        rows marked ``holiday``.
        """
        first, last = self._month_bounds(year, month)
        if employee_id is not None and employee_id != self.user.id:
            await self._require_manager(group_id)
            targets = [employee_id]
        elif employee_id is not None:
            await self.require(group_id, 'hr.attendance.record')
            targets = [self.user.id]
        elif await self._is_manager(group_id):
            await self.require(group_id, 'hr.attendance.record')
            targets = None  # team
        else:
            await self.require(group_id, 'hr.attendance.record')
            targets = [self.user.id]
        weekend_days = await self._weekend_days(group_id)
        business_days = int(count_business_days(first, last, weekend_days=weekend_days))
        # "Unrecorded" only makes sense for days that have already passed:
        # future business days (and future approved leave) are not missing data.
        today = await org_today(self.db)
        window_end = min(last, today)
        if targets is None:
            rows = (await self.db.scalars(select(HrAttendance).where(
                HrAttendance.group_id == group_id, HrAttendance.date >= first, HrAttendance.date <= last))).all()
            leave_rows = (await self.db.scalars(select(HrLeaveRequest).where(
                HrLeaveRequest.group_id == group_id, HrLeaveRequest.status.in_(['approved', 'applied']),
                HrLeaveRequest.start_date <= last, HrLeaveRequest.end_date >= first))).all()
            target_ids = sorted({r.employee_id for r in rows} | {r.employee_id for r in leave_rows})
            if self.user.id not in target_ids:
                target_ids.append(self.user.id)
        else:
            target_ids = targets
            rows = (await self.db.scalars(select(HrAttendance).where(
                HrAttendance.group_id == group_id, HrAttendance.employee_id.in_(target_ids),
                HrAttendance.date >= first, HrAttendance.date <= last))).all()
            leave_rows = (await self.db.scalars(select(HrLeaveRequest).where(
                HrLeaveRequest.group_id == group_id, HrLeaveRequest.employee_id.in_(target_ids),
                HrLeaveRequest.status.in_(['approved', 'applied']), HrLeaveRequest.start_date <= last,
                HrLeaveRequest.end_date >= first))).all()
        emails = {u.id: u.email for u in (await self.db.scalars(
            select(User).where(User.id.in_(target_ids)))).all()}
        summaries = []
        for target in target_ids:
            person_rows = [r for r in rows if r.employee_id == target]
            recorded = {r.date for r in person_rows if r.date.weekday() not in weekend_days}
            counts = {'present': 0, 'work_from_home': 0, 'leave': 0, 'holiday': 0}
            for r in person_rows:
                if r.date.weekday() in weekend_days:
                    continue  # rows on weekend days are listed, not tallied
                counts[r.status] += 1
            leave_days = set()
            for req in (r for r in leave_rows if r.employee_id == target):
                day = max(req.start_date, first)
                end = min(req.end_date, last)
                while day <= end:
                    if day.weekday() not in weekend_days and day not in recorded:
                        leave_days.add(day)
                    day += timedelta(days=1)
            counts['leave'] += len(leave_days)
            unrecorded = 0
            day = first
            while day <= window_end:
                if day.weekday() not in weekend_days and day not in recorded and day not in leave_days:
                    unrecorded += 1
                day += timedelta(days=1)
            counts['unrecorded'] = unrecorded
            summaries.append({'employee_id': str(target), 'email': emails.get(target),
                              'business_days': business_days, **counts,
                              'rows': [self._attendance_out(r) for r in
                                       sorted(person_rows, key=lambda r: r.date, reverse=True)]})
        return {'group_id': str(group_id), 'month': f'{year:04d}-{month:02d}', 'employees': summaries}

    # ------------------------------------------------------------------ payroll schedules + salary reminders (slice 5)

    @staticmethod
    def _schedule_out(row: HrPayrollSchedule, email: str | None = None) -> dict:
        return {'id': str(row.id), 'group_id': str(row.group_id), 'employee_id': str(row.employee_id),
                'email': email, 'pay_day': row.pay_day, 'amount': format(row.amount.normalize(), 'f'),
                'currency': row.currency, 'active': row.active,
                'created_at': row.created_at.isoformat() if row.created_at else None}

    @staticmethod
    def _reminder_out(row: HrSalaryReminder, email: str | None = None) -> dict:
        return {'id': str(row.id), 'group_id': str(row.group_id), 'employee_id': str(row.employee_id),
                'email': email, 'payroll_schedule_id': str(row.payroll_schedule_id),
                'pay_date': row.pay_date.isoformat(), 'kind': row.kind,
                'amount': format(row.amount.normalize(), 'f'), 'currency': row.currency,
                'message': row.message, 'sent_at': row.sent_at.isoformat() if row.sent_at else None,
                'channel': row.channel, 'pending_delivery': row.sent_at is None,
                'created_at': row.created_at.isoformat() if row.created_at else None}

    async def create_payroll_schedule(self, group_id, *, employee_id, pay_day: int,
                                      amount: Decimal, currency: str = 'PKR') -> HrPayrollSchedule:
        """Create (or update + reactivate) one employee's monthly pay schedule.

        Amounts are display-only: a schedule never pays anyone. ``hr.manage``
        only. The employee must be an active member of the department.
        """
        await self.require(group_id, 'hr.manage')
        if not (1 <= pay_day <= 31):
            raise InvalidInputError('Pay-day must be between 1 and 31.')
        amount = Decimal(str(amount))
        if amount < 0:
            raise InvalidInputError('Amount cannot be negative.')
        currency = (currency or '').strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise InvalidInputError('Currency must be a 3-letter code.')
        person = await self.db.get(User, employee_id)
        if person is None or not person.is_active:
            raise InvalidInputError('Choose an active person.')
        member = await self.db.scalar(select(AccessGroupMember).where(
            AccessGroupMember.group_id == group_id, AccessGroupMember.user_id == employee_id))
        if member is None:
            raise InvalidInputError('That person is not a member of this department.')
        row = await self.db.scalar(select(HrPayrollSchedule).where(
            HrPayrollSchedule.group_id == group_id, HrPayrollSchedule.employee_id == employee_id))
        if row is None:
            row = HrPayrollSchedule(group_id=group_id, employee_id=employee_id, pay_day=pay_day,
                                    amount=amount, currency=currency, active=True)
            self.db.add(row)
        else:
            row.pay_day, row.amount, row.currency, row.active = pay_day, amount, currency, True
        audit(self.db, self.p, 'hr.payroll_schedule_saved', target=str(row.id),
              details={'group_id': str(group_id), 'employee_id': str(employee_id), 'pay_day': pay_day,
                       'amount': format(amount.normalize(), 'f'), 'currency': currency})
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def list_payroll_schedules(self, group_id) -> list[dict]:
        await self.require(group_id, 'hr.salary.remind')
        rows = (await self.db.scalars(select(HrPayrollSchedule).where(
            HrPayrollSchedule.group_id == group_id).order_by(HrPayrollSchedule.created_at))).all()
        emails = {u.id: u.email for u in (await self.db.scalars(
            select(User).where(User.id.in_([r.employee_id for r in rows])))).all()} if rows else {}
        return [self._schedule_out(row, emails.get(row.employee_id)) for row in rows]

    async def my_payroll_schedule(self, group_id) -> dict | None:
        await self.require(group_id, 'hr.leave.request')
        row = await self.db.scalar(select(HrPayrollSchedule).where(
            HrPayrollSchedule.group_id == group_id, HrPayrollSchedule.employee_id == self.user.id))
        return self._schedule_out(row, self.user.email) if row is not None else None

    async def deactivate_payroll_schedule(self, group_id, schedule_id) -> HrPayrollSchedule:
        """Stop reminders for a schedule (the row stays for history). Reactivate by saving again."""
        await self.require(group_id, 'hr.manage')
        row = await self.db.get(HrPayrollSchedule, schedule_id)
        if row is None or row.group_id != group_id:
            raise NotFoundError('Payroll schedule not found.')
        row.active = False
        audit(self.db, self.p, 'hr.payroll_schedule_deactivated', target=str(row.id),
              details={'group_id': str(group_id), 'employee_id': str(row.employee_id)})
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def list_salary_reminders(self, group_id, *, employee_id=None,
                                    year: int | None = None, month: int | None = None) -> list[dict]:
        """Salary reminders for a month (own, or a manager's team member / whole team)."""
        if year is not None or month is not None:
            first, last = self._month_bounds(year or 2000, month or 1)
        else:
            first = last = None
        if employee_id is not None and employee_id != self.user.id:
            await self._require_manager(group_id)
            targets = [employee_id]
        elif employee_id is not None:
            await self.require(group_id, 'hr.leave.request')
            targets = [self.user.id]
        elif await self._is_manager(group_id):
            await self.require(group_id, 'hr.salary.remind')
            targets = None  # team
        else:
            await self.require(group_id, 'hr.leave.request')
            targets = [self.user.id]
        query = select(HrSalaryReminder).where(HrSalaryReminder.group_id == group_id)
        if targets is not None:
            query = query.where(HrSalaryReminder.employee_id.in_(targets))
        if first is not None:
            query = query.where(HrSalaryReminder.pay_date >= first, HrSalaryReminder.pay_date <= last)
        rows = (await self.db.scalars(query.order_by(HrSalaryReminder.pay_date.desc()).limit(200))).all()
        emails = {u.id: u.email for u in (await self.db.scalars(
            select(User).where(User.id.in_([r.employee_id for r in rows])))).all()} if rows else {}
        return [self._reminder_out(row, emails.get(row.employee_id)) for row in rows]

    async def run_reminder_cycle(self, group_id) -> dict:
        """Run this department's salary-reminder cycle now (manual trigger; idempotent)."""
        await self.require(group_id, 'hr.manage')
        from app.hr.scheduler import run_salary_reminder_cycle

        result = await run_salary_reminder_cycle(self.db, await org_today(self.db), group_id=group_id)
        audit(self.db, self.p, 'hr.salary_reminder_cycle', target=str(group_id), details=result)
        await self.db.commit()
        return result
