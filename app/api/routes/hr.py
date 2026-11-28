"""HR API (Step AG, slice 3): leave requests on the unified engine.

Every route authorises on the department's platform scope: hr.leave.request,
hr.leave.approve, hr.attendance.record, hr.salary.remind, hr.manage. A leave
approval is bound to the exact request (employee + type + dates + days); the
manager decides by presenting the action hash, and the balance is applied
through `execute_once` with read-back.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import CurrentUser, DbSession
from app.core.errors import InvalidInputError
from app.hr.service import HrService

router = APIRouter(prefix='/api/hr', tags=['hr'])


@router.get('/scopes')
async def hr_scopes(db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    return {'scopes': await service.scopes()}


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class LeaveRequestIn(Strict):
    group_id: UUID
    leave_type: Literal['annual', 'sick', 'casual']
    start_date: date
    end_date: date
    half_day: bool = False


class LeaveDecisionIn(Strict):
    decision: Literal['approve', 'reject']
    presented_hash: str = Field(min_length=64, max_length=64, pattern=r'^[0-9a-f]{64}$')
    note: str = Field(default='', max_length=500)


@router.get('/leave/approvals')
async def leave_approvals(group_id: UUID, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    return {'approvals': await service.pending_leave_approvals(group_id)}


@router.get('/leave/balance')
async def my_leave_balance(group_id: UUID, year: int, leave_type: str, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    return await service.balance_for(group_id, user.id, leave_type, year)


@router.post('/leave', status_code=201)
async def request_leave(body: LeaveRequestIn, db: DbSession, user: CurrentUser):
    row = await HrService(db, user).request_leave(
        body.group_id, leave_type=body.leave_type, start=body.start_date, end=body.end_date,
        half_day=body.half_day)
    return HrService(db, user)._out(row)


@router.get('/leave')
async def my_leave(group_id: UUID, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    return {'leave': [service._out(row) for row in await service.list_my_leave(group_id)]}


@router.get('/leave/team')
async def team_leave(group_id: UUID, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    return {'leave': [service._out(row) for row in await service.list_team_leave(group_id)]}


@router.get('/leave/{request_id}')
async def get_leave(request_id: UUID, group_id: UUID, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    return service._out(await service.get_leave(group_id, request_id))


@router.post('/leave/{request_id}/decide')
async def decide_leave(request_id: UUID, body: LeaveDecisionIn, group_id: UUID, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    row = await service.decide_leave(group_id, request_id, decision=body.decision,
                                     presented_hash=body.presented_hash, note=body.note)
    return service._out(row)


@router.post('/leave/{request_id}/execute')
async def execute_leave(request_id: UUID, group_id: UUID, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    row = await service.execute_leave_request(group_id, request_id)
    return service._out(row)


@router.post('/leave/{request_id}/cancel')
async def cancel_leave(request_id: UUID, group_id: UUID, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    row = await service.cancel_leave(group_id, request_id)
    return service._out(row)


@router.get('/leave/{request_id}/balance')
async def leave_balance(request_id: UUID, group_id: UUID, year: int, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    row = await service.get_leave(group_id, request_id)
    return await service.balance_for(group_id, row.employee_id, row.leave_type, year)


# ------------------------------------------------------------------ attendance (slice 4)

class AttendanceIn(Strict):
    group_id: UUID
    employee_id: UUID | None = None  # default: yourself; a manager may record for a team member
    date: date
    status: Literal['present', 'leave', 'holiday', 'work_from_home']
    check_in: time | None = None
    check_out: time | None = None


def _parse_month(month: str) -> tuple[int, int]:
    try:
        year_s, month_s = month.split('-')
        year, mon = int(year_s), int(month_s)
    except (ValueError, AttributeError):
        raise InvalidInputError('Month must look like YYYY-MM.')
    if not (1 <= mon <= 12) or not (2000 <= year <= 2100):
        raise InvalidInputError('Choose a valid month.')
    return year, mon


@router.post('/attendance', status_code=201)
async def record_attendance(body: AttendanceIn, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    row = await service.record_attendance(
        body.group_id, employee_id=body.employee_id, day=body.date, status=body.status,
        check_in=body.check_in, check_out=body.check_out)
    return service._attendance_out(row)


@router.get('/attendance')
async def list_attendance(group_id: UUID, month: str, db: DbSession, user: CurrentUser,
                          employee_id: UUID | None = None):
    year, mon = _parse_month(month)
    service = HrService(db, user)
    rows = await service.list_attendance(group_id, employee_id=employee_id, year=year, month=mon)
    return {'attendance': [service._attendance_out(row) for row in rows]}


@router.get('/attendance/summary')
async def attendance_summary(group_id: UUID, month: str, db: DbSession, user: CurrentUser,
                             employee_id: UUID | None = None):
    year, mon = _parse_month(month)
    service = HrService(db, user)
    return await service.attendance_summary(group_id, employee_id=employee_id, year=year, month=mon)


# ------------------------------------------------------------------ payroll schedules + salary reminders (slice 5)

class PayrollScheduleIn(Strict):
    group_id: UUID
    employee_id: UUID
    pay_day: int = Field(ge=1, le=31)
    amount: Decimal = Field(ge=0)
    currency: str = Field(default='PKR', min_length=3, max_length=3, pattern=r'^[A-Za-z]{3}$')


class RunCycleIn(Strict):
    group_id: UUID


@router.post('/payroll-schedules', status_code=201)
async def create_payroll_schedule(body: PayrollScheduleIn, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    row = await service.create_payroll_schedule(
        body.group_id, employee_id=body.employee_id, pay_day=body.pay_day,
        amount=body.amount, currency=body.currency)
    return service._schedule_out(row)


@router.get('/payroll-schedules')
async def list_payroll_schedules(group_id: UUID, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    return {'schedules': await service.list_payroll_schedules(group_id)}


@router.get('/payroll-schedules/mine')
async def my_payroll_schedule(group_id: UUID, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    return {'schedule': await service.my_payroll_schedule(group_id)}


@router.post('/payroll-schedules/{schedule_id}/deactivate')
async def deactivate_payroll_schedule(schedule_id: UUID, group_id: UUID, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    row = await service.deactivate_payroll_schedule(group_id, schedule_id)
    return service._schedule_out(row)


@router.get('/salary-reminders')
async def list_salary_reminders(group_id: UUID, db: DbSession, user: CurrentUser,
                                month: str | None = None, employee_id: UUID | None = None):
    year = mon = None
    if month is not None:
        year, mon = _parse_month(month)
    service = HrService(db, user)
    return {'reminders': await service.list_salary_reminders(
        group_id, employee_id=employee_id, year=year, month=mon)}


@router.post('/salary-reminders/run')
async def run_salary_reminders(body: RunCycleIn, db: DbSession, user: CurrentUser):
    service = HrService(db, user)
    return await service.run_reminder_cycle(body.group_id)
