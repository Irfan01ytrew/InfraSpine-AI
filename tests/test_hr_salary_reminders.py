"""Step AG slice 5: salary-reminder scheduler (read-only builder, delivery via Step AA).

The cycle creates one HrSalaryReminder row per active schedule per pay cycle
(unique on schedule + pay date + kind), copies amounts from the schedule,
and never moves money. Rows are created unsent (sent_at=None) — delivery
(WhatsApp/email) is Step AA's scheduled-report delivery, not on this branch.

Runs against real PostgreSQL via the standard conftest fixtures.
"""

import uuid
from datetime import date

from sqlalchemy import text

from app.hr.models import HrPayrollSchedule
from app.hr.scheduler import run_salary_reminder_cycle
from app.main import app
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.test_hr_leave_api import _grant_role, _setup, _user_id


def _setup_hr(client):
    """Department + hr.admin granted to the admin user (schedule management)."""
    ctx = _setup(client)
    _grant_role(client, ctx["department_id"], _user_id(ADMIN_EMAIL), "hr.admin")
    return ctx


def _create_schedule_row(department_id, employee_id, pay_day, amount="50000.00",
                         currency="PKR", active=True):
    return str(run_db(lambda conn: conn.scalar(
        HrPayrollSchedule.__table__.insert().values(
            group_id=department_id, employee_id=employee_id, pay_day=pay_day,
            amount=amount, currency=currency, active=active).returning(HrPayrollSchedule.id))))


def _run_cycle(client, today):
    async def cycle(db):
        return await run_salary_reminder_cycle(db, today)

    def in_app(fn):
        async def _run():
            async with app.state.sessionmaker() as session:
                return await fn(session)

        return _run

    return client.portal.call(in_app(cycle))


def _reminder_rows(schedule_id):
    return run_db(lambda conn: conn.execute(text(
        "SELECT kind, pay_date, amount, sent_at IS NULL FROM hr_salary_reminders "
        "WHERE payroll_schedule_id = :s"), {"s": schedule_id})).all()


def _create_schedule_api(client, department_id, employee_id, pay_day, amount="50000.00",
                         currency="PKR", email=ADMIN_EMAIL):
    login(client, email)
    return client.post("/api/hr/payroll-schedules", json={
        "group_id": department_id, "employee_id": employee_id, "pay_day": pay_day,
        "amount": amount, "currency": currency})


# --------------------------------------------------------------------------- scheduler cycle

def test_cycle_creates_upcoming_reminder(client):
    ctx = _setup_hr(client)
    schedule_id = _create_schedule_row(ctx["department_id"], _user_id(USER_EMAIL), pay_day=15)
    result = _run_cycle(client, date(2026, 11, 13))  # 2 days before pay-day
    assert result == {"created": 1, "already_present": 0, "due": 1}
    rows = _reminder_rows(schedule_id)
    assert len(rows) == 1
    assert rows[0][0] == "upcoming" and str(rows[0][1]) == "2026-11-15"
    assert float(rows[0][2]) == 50000.00 and rows[0][3] is True  # unsent


def test_cycle_creates_due_today_reminder(client):
    ctx = _setup_hr(client)
    schedule_id = _create_schedule_row(ctx["department_id"], _user_id(USER_EMAIL), pay_day=15)
    assert _run_cycle(client, date(2026, 11, 15))["created"] == 1
    assert _reminder_rows(schedule_id)[0][0] == "due_today"


def test_cycle_creates_overdue_reminder(client):
    ctx = _setup_hr(client)
    schedule_id = _create_schedule_row(ctx["department_id"], _user_id(USER_EMAIL), pay_day=15)
    assert _run_cycle(client, date(2026, 11, 20))["created"] == 1
    assert _reminder_rows(schedule_id)[0][0] == "overdue"


def test_cycle_is_idempotent(client):
    ctx = _setup_hr(client)
    schedule_id = _create_schedule_row(ctx["department_id"], _user_id(USER_EMAIL), pay_day=15)
    assert _run_cycle(client, date(2026, 11, 15)) == {"created": 1, "already_present": 0, "due": 1}
    assert _run_cycle(client, date(2026, 11, 15)) == {"created": 0, "already_present": 1, "due": 1}
    assert len(_reminder_rows(schedule_id)) == 1


def test_cycle_skips_inactive_schedule(client):
    ctx = _setup_hr(client)
    schedule_id = _create_schedule_row(ctx["department_id"], _user_id(USER_EMAIL), pay_day=15, active=False)
    assert _run_cycle(client, date(2026, 11, 15)) == {"created": 0, "already_present": 0, "due": 0}
    assert _reminder_rows(schedule_id) == []


def test_cycle_clamps_pay_day_to_month_length(client):
    ctx = _setup_hr(client)
    schedule_id = _create_schedule_row(ctx["department_id"], _user_id(USER_EMAIL), pay_day=31)
    assert _run_cycle(client, date(2026, 2, 25))["created"] == 1  # February has 28 days in 2026
    assert str(_reminder_rows(schedule_id)[0][1]) == "2026-02-28"


# --------------------------------------------------------------------------- schedule API

def test_create_schedule_api_upserts(client):
    ctx = _setup_hr(client)
    first = _create_schedule_api(client, ctx["department_id"], _user_id(USER_EMAIL), pay_day=15)
    assert first.status_code == 201, first.text
    assert first.json()["pay_day"] == 15 and first.json()["active"] is True
    second = _create_schedule_api(client, ctx["department_id"], _user_id(USER_EMAIL), pay_day=25)
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"] and second.json()["pay_day"] == 25


def test_create_schedule_validation(client):
    ctx = _setup_hr(client)
    employee = _user_id(USER_EMAIL)
    assert _create_schedule_api(client, ctx["department_id"], employee, pay_day=0).status_code == 422
    assert _create_schedule_api(client, ctx["department_id"], employee, pay_day=15,
                                amount="-1").status_code == 422
    assert _create_schedule_api(client, ctx["department_id"], employee, pay_day=15,
                                currency="PK").status_code == 422


def test_create_schedule_forbidden_for_employee(client):
    ctx = _setup_hr(client)
    response = _create_schedule_api(client, ctx["department_id"], _user_id(USER_EMAIL),
                                    pay_day=15, email=USER_EMAIL)
    assert response.status_code == 403, response.text


def test_list_reminders_scoped(client):
    ctx = _setup_hr(client)
    schedule_id = _create_schedule_row(ctx["department_id"], _user_id(USER_EMAIL), pay_day=15)
    assert _run_cycle(client, date(2026, 11, 15))["created"] == 1
    login(client, USER_EMAIL)
    mine = client.get(f"/api/hr/salary-reminders?group_id={ctx['department_id']}").json()["reminders"]
    assert len(mine) == 1 and mine[0]["pending_delivery"] is True
    login(client, USER2_EMAIL)
    assert client.get(f"/api/hr/salary-reminders?group_id={ctx['department_id']}").status_code == 404


def test_run_endpoint_idempotent(client):
    ctx = _setup_hr(client)
    _create_schedule_api(client, ctx["department_id"], _user_id(USER_EMAIL),
                         pay_day=date.today().day)
    login(client, ADMIN_EMAIL)
    first = client.post("/api/hr/salary-reminders/run", json={"group_id": ctx["department_id"]})
    assert first.status_code == 200, first.text
    assert first.json()["created"] == 1
    second = client.post("/api/hr/salary-reminders/run", json={"group_id": ctx["department_id"]})
    assert second.status_code == 200, second.text
    assert second.json()["created"] == 0 and second.json()["already_present"] == 1


def test_deactivate_then_reactivate(client):
    ctx = _setup_hr(client)
    schedule_id = _create_schedule_api(
        client, ctx["department_id"], _user_id(USER_EMAIL), pay_day=date.today().day).json()["id"]
    login(client, ADMIN_EMAIL)
    response = client.post(
        f"/api/hr/payroll-schedules/{schedule_id}/deactivate?group_id={ctx['department_id']}")
    assert response.status_code == 200, response.text
    assert response.json()["active"] is False
    assert client.post("/api/hr/salary-reminders/run",
                       json={"group_id": ctx["department_id"]}).json()["created"] == 0
    # Saving the schedule again reactivates it; the next cycle creates the reminder.
    assert _create_schedule_api(client, ctx["department_id"], _user_id(USER_EMAIL),
                                pay_day=date.today().day).json()["active"] is True
    assert client.post("/api/hr/salary-reminders/run",
                       json={"group_id": ctx["department_id"]}).json()["created"] == 1
    assert uuid.UUID(schedule_id)  # same schedule row reused
