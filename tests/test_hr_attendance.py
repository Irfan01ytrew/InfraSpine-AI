"""Step AG slice 4: attendance recording API + monthly attendance summary.

An employee records their own day (`hr.attendance.record`); a department
manager records for a member of their own department. One row per employee
per day — recording the same day again corrects it. The monthly summary
counts business days only (weekend days from the department's annual
policy, default Sat+Sun); a past business day with neither a record nor
approved leave is reported as `unrecorded`, never as "absent".

Runs against real PostgreSQL via the standard conftest fixtures.
"""

import calendar
from datetime import date, timedelta

from sqlalchemy import text

from app.hr.leave import count_business_days
from tests.conftest import USER2_EMAIL, USER_EMAIL, login, run_db
from tests.test_hr_leave_api import _approve_and_execute, _decide, _next_weekday, _request, _setup, _user_id


def _first_monday_next_month() -> date:
    """First Monday of next month (always on or before the 7th).

    A Mon–Wed leave block starting here never straddles a month boundary —
    `_next_weekday(0)` does when the next Monday is within two days of
    month-end (e.g. Sat 2026-11-28 → Mon 2026-11-30), which made the
    leave-count assertion date-dependent.
    """
    first = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    while first.weekday() != 0:
        first += timedelta(days=1)
    return first


def _past_weekday(weekday: int, weeks_ago: int = 1) -> date:
    """The most recent past date with the given Python weekday, weeks_ago weeks back."""
    day = date.today() - timedelta(days=1)
    while day.weekday() != weekday:
        day -= timedelta(days=1)
    return day - timedelta(weeks=weeks_ago - 1)


def _month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _business_days(year: int, month: int) -> int:
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return int(count_business_days(first, last, weekend_days=frozenset({5, 6})))


def _business_days_upto(year: int, month: int, upto: date) -> int:
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return int(count_business_days(first, min(last, upto), weekend_days=frozenset({5, 6})))


def _record(client, department_id, *, day, status="present", check_in=None, check_out=None,
            employee_id=None, email=USER_EMAIL):
    login(client, email)
    body = {"group_id": department_id, "date": day.isoformat(), "status": status}
    if employee_id is not None:
        body["employee_id"] = employee_id
    if check_in is not None:
        body["check_in"] = check_in
    if check_out is not None:
        body["check_out"] = check_out
    return client.post("/api/hr/attendance", json=body)


def _attendance_rows(department_id, employee_id):
    return run_db(lambda conn: conn.execute(text(
        "SELECT date, status FROM hr_attendance WHERE group_id = :g AND employee_id = :u"),
        {"g": department_id, "u": employee_id})).all()


# --------------------------------------------------------------------------- recording

def test_record_own_present_day(client):
    ctx = _setup(client)
    day = _past_weekday(0)
    response = _record(client, ctx["department_id"], day=day, check_in="09:00", check_out="17:30")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "present" and body["check_in"] == "09:00:00" and body["check_out"] == "17:30:00"
    rows = _attendance_rows(ctx["department_id"], _user_id(USER_EMAIL))
    assert len(rows) == 1 and rows[0][1] == "present"


def test_record_future_date_rejected(client):
    ctx = _setup(client)
    response = _record(client, ctx["department_id"], day=date.today() + timedelta(days=30))
    assert response.status_code == 422, response.text


def test_record_check_out_before_check_in_rejected(client):
    ctx = _setup(client)
    response = _record(client, ctx["department_id"], day=_past_weekday(1),
                       check_in="17:00", check_out="09:00")
    assert response.status_code == 422, response.text


def test_record_times_with_leave_rejected(client):
    ctx = _setup(client)
    response = _record(client, ctx["department_id"], day=_past_weekday(2),
                       status="leave", check_in="09:00")
    assert response.status_code == 422, response.text


def test_record_same_day_updates_row(client):
    ctx = _setup(client)
    day = _past_weekday(3)
    assert _record(client, ctx["department_id"], day=day).status_code == 201
    response = _record(client, ctx["department_id"], day=day, status="work_from_home")
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "work_from_home"
    rows = _attendance_rows(ctx["department_id"], _user_id(USER_EMAIL))
    assert len(rows) == 1 and rows[0][1] == "work_from_home"


# --------------------------------------------------------------------------- authorization

def test_employee_cannot_record_for_other(client):
    ctx = _setup(client)
    response = _record(client, ctx["department_id"], day=_past_weekday(0),
                       employee_id=ctx["manager_id"])
    assert response.status_code == 403, response.text


def test_manager_records_for_team_member(client):
    ctx = _setup(client)
    response = _record(client, ctx["department_id"], day=_past_weekday(0),
                       employee_id=_user_id(USER_EMAIL), email=ctx["manager_email"])
    assert response.status_code == 201, response.text
    assert response.json()["employee_id"] == _user_id(USER_EMAIL)


def test_non_member_gets_404(client):
    ctx = _setup(client)
    login(client, USER2_EMAIL)  # seeded user, no membership in this department
    response = client.post("/api/hr/attendance", json={
        "group_id": ctx["department_id"], "date": _past_weekday(0).isoformat(), "status": "present"})
    assert response.status_code == 404, response.text


# --------------------------------------------------------------------------- listing

def test_list_own_attendance(client):
    ctx = _setup(client)
    day = _past_weekday(0)
    assert _record(client, ctx["department_id"], day=day).status_code == 201
    assert _record(client, ctx["department_id"], day=day + timedelta(days=1),
                   status="work_from_home").status_code == 201
    login(client, USER_EMAIL)
    body = client.get(f"/api/hr/attendance?group_id={ctx['department_id']}&month={_month_of(day)}").json()
    assert len(body["attendance"]) == 2
    assert {row["status"] for row in body["attendance"]} == {"present", "work_from_home"}


def test_list_other_employee_forbidden(client):
    ctx = _setup(client)
    login(client, USER_EMAIL)
    response = client.get(
        f"/api/hr/attendance?group_id={ctx['department_id']}&month={_month_of(_past_weekday(0))}"
        f"&employee_id={ctx['manager_id']}")
    assert response.status_code == 403, response.text


# --------------------------------------------------------------------------- monthly summary

def test_summary_counts_present_and_unrecorded(client):
    ctx = _setup(client)
    monday = _past_weekday(0)
    tuesday = monday + timedelta(days=1)
    assert _record(client, ctx["department_id"], day=monday).status_code == 201
    assert _record(client, ctx["department_id"], day=tuesday, status="work_from_home").status_code == 201
    login(client, USER_EMAIL)
    body = client.get(
        f"/api/hr/attendance/summary?group_id={ctx['department_id']}&month={_month_of(monday)}").json()
    assert len(body["employees"]) == 1
    mine = body["employees"][0]
    assert mine["business_days"] == _business_days(monday.year, monday.month)
    assert mine["present"] == 1 and mine["work_from_home"] == 1
    assert mine["leave"] == 0 and mine["holiday"] == 0
    assert mine["unrecorded"] == _business_days_upto(monday.year, monday.month, date.today()) - 2


def test_summary_includes_approved_leave(client):
    ctx = _setup(client)
    start = _first_monday_next_month()  # Monday-Wednesday: 3 business days, one month
    request_id = _request(client, ctx["department_id"], start=start, end=start + timedelta(days=2)).json()["id"]
    _approve_and_execute(client, ctx, request_id)
    login(client, USER_EMAIL)
    body = client.get(
        f"/api/hr/attendance/summary?group_id={ctx['department_id']}&month={_month_of(start)}").json()
    mine = body["employees"][0]
    assert mine["leave"] == 3 and mine["present"] == 0
    assert mine["business_days"] == _business_days(start.year, start.month)


def test_summary_includes_approved_but_not_executed_leave(client):
    # Approved (manager said yes) but not yet applied to the balance is still leave.
    ctx = _setup(client)
    start = _first_monday_next_month()  # Monday-Wednesday: 3 business days, one month
    request_id = _request(client, ctx["department_id"], start=start, end=start + timedelta(days=2)).json()["id"]
    assert _decide(client, ctx, request_id).status_code == 200
    login(client, USER_EMAIL)
    body = client.get(
        f"/api/hr/attendance/summary?group_id={ctx['department_id']}&month={_month_of(start)}").json()
    mine = body["employees"][0]
    assert mine["leave"] == 3 and mine["present"] == 0


def test_summary_team_for_manager(client):
    ctx = _setup(client)
    day = _past_weekday(0)
    assert _record(client, ctx["department_id"], day=day).status_code == 201
    login(client, ctx["manager_email"])
    body = client.get(
        f"/api/hr/attendance/summary?group_id={ctx['department_id']}&month={_month_of(day)}").json()
    by_email = {row["email"]: row for row in body["employees"]}
    assert set(by_email) == {USER_EMAIL, ctx["manager_email"]}
    assert by_email[USER_EMAIL]["present"] == 1
    assert by_email[ctx["manager_email"]]["present"] == 0


def test_summary_bad_month_rejected(client):
    ctx = _setup(client)
    login(client, USER_EMAIL)
    assert client.get(
        f"/api/hr/attendance/summary?group_id={ctx['department_id']}&month=2026-13").status_code == 422
    assert client.get(
        f"/api/hr/attendance/summary?group_id={ctx['department_id']}&month=junk").status_code == 422
