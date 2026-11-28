"""Tests for the Companion OS daily briefing + digest rendering (Step AN, slice 2).

Pure functions: no database, no network, no sending.

Run: .venv/bin/python -m pytest tests/test_companion_briefing.py --noconftest -q
"""

from datetime import date, datetime, time, timezone

import pytest

from app.companion.briefing import (
    build_daily_briefing,
    in_quiet_hours,
    render_digest_subject,
    render_digest_text,
)
from app.companion.planner import (
    Goal,
    Preferences,
    ProgressRecord,
    TaskInput,
)

TODAY = date(2026, 11, 28)
USER = "user-1"
OTHER = "user-2"


def prefs(**kw):
    base = {"user_id": USER}
    base.update(kw)
    return Preferences(**base)


def goal(gid="g1", **kw):
    base = {"id": gid, "user_id": USER, "title": "Learn Spanish"}
    base.update(kw)
    return Goal(**base)


def task(tid="t1", **kw):
    base = {"id": tid, "user_id": USER, "goal_id": "g1", "title": "Do lesson 1"}
    base.update(kw)
    return TaskInput(**base)


def record(rid="r1", **kw):
    base = {
        "id": rid, "user_id": USER, "goal_id": "g1", "task_id": "t1",
        "percent": 50, "note": "half done", "on": date(2026, 11, 27),
    }
    base.update(kw)
    return ProgressRecord(**base)


# --- briefing ---


def test_briefing_orders_overdue_first_and_counts_them():
    goals = [goal()]
    tasks = [
        task("t1", title="Future task", due=date(2026, 11, 30)),
        task("t2", title="Overdue task", due=date(2026, 11, 20)),
    ]
    briefing = build_daily_briefing(USER, goals, tasks, prefs(), [], TODAY)
    assert briefing.plan.tasks[0].task.id == "t2"
    assert briefing.plan.overdue_count == 1
    assert briefing.on == TODAY
    assert briefing.user_id == USER


def test_briefing_marks_stalled_goals_and_reports_progress():
    goals = [goal(), goal("g2", title="Run a marathon")]
    tasks = [task(), task("t2", goal_id="g2", title="Buy shoes")]
    records = [record(on=date(2026, 11, 27))]
    briefing = build_daily_briefing(USER, goals, tasks, prefs(), records, TODAY)
    assert briefing.stalled_goal_ids == ("g2",)  # g1 has a record yesterday; g2 has none
    assert {g.goal_id: g.percent for g in briefing.goals} == {"g1": 50, "g2": 0}
    assert briefing.leave_off["g1"]["last_progress"]["percent"] == 50
    assert briefing.leave_off["g2"]["last_progress"] is None  # never invented


def test_briefing_rejects_foreign_rows():
    with pytest.raises(ValueError):
        build_daily_briefing(USER, [goal(user_id=OTHER)], [], prefs(), [], TODAY)
    with pytest.raises(ValueError):
        build_daily_briefing(USER, [goal()], [task(user_id=OTHER)], prefs(), [], TODAY)
    with pytest.raises(ValueError):
        build_daily_briefing(USER, [goal()], [], prefs(), [record(user_id=OTHER)], TODAY)


def test_briefing_flags_overload_warning():
    tasks = [task(estimated_hours=50.0)]
    briefing = build_daily_briefing(USER, [goal()], tasks, prefs(), [], TODAY)
    assert briefing.plan.overloaded is True
    assert briefing.plan.total_estimated_hours == 50.0
    assert any("capacity" in w for w in briefing.plan.warnings)


def test_briefing_skips_archived_goals_and_done_tasks():
    goals = [goal(), goal("g2", title="Old", status="archived")]
    tasks = [task(), task("t2", goal_id="g2", title="Old task"), task("t3", title="Done", status="done")]
    briefing = build_daily_briefing(USER, goals, tasks, prefs(), [], TODAY)
    assert [item.task.id for item in briefing.plan.tasks] == ["t1"]


# --- digest rendering ---


def test_digest_subject_and_body_carry_only_own_data():
    briefing = build_daily_briefing(
        USER, [goal()], [task(due=date(2026, 11, 20))], prefs(), [record()], TODAY
    )
    subject = render_digest_subject(briefing)
    body = render_digest_text(briefing, user_email="user@example.com")
    assert "2026-11-28" in subject
    assert "1 overdue" in subject
    assert "Learn Spanish" in body
    assert "Do lesson 1" in body
    assert "user@example.com" in body
    assert "50%" in body
    assert "[needs approval]" not in body  # internal task: no approval flag


def test_digest_body_flags_external_tasks_needing_approval():
    briefing = build_daily_briefing(
        USER, [goal()], [task(action_kind="send", title="Send the report")], prefs(), [], TODAY
    )
    body = render_digest_text(briefing, user_email="user@example.com")
    assert "Send the report" in body
    assert "[needs approval]" in body


def test_digest_body_for_empty_day_has_no_invented_items():
    briefing = build_daily_briefing(USER, [goal()], [], prefs(), [], TODAY)
    body = render_digest_text(briefing, user_email="user@example.com")
    assert "0 task(s)" in body
    assert "Learn Spanish: 0%" in body


# --- quiet hours ---


def test_quiet_hours_overnight_window():
    # Default 22:00-07:00 Asia/Karachi.
    assert in_quiet_hours(datetime(2026, 11, 28, 18, 0, tzinfo=timezone.utc), "Asia/Karachi", time(22, 0), time(7, 0))
    assert in_quiet_hours(datetime(2026, 11, 28, 1, 0, tzinfo=timezone.utc), "Asia/Karachi", time(22, 0), time(7, 0))
    assert not in_quiet_hours(datetime(2026, 11, 28, 8, 0, tzinfo=timezone.utc), "Asia/Karachi", time(22, 0), time(7, 0))
    assert not in_quiet_hours(datetime(2026, 11, 28, 16, 0, tzinfo=timezone.utc), "Asia/Karachi", time(22, 0), time(7, 0))  # 21:00 PKT


def test_quiet_hours_daytime_window_and_empty_window():
    assert in_quiet_hours(datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc), "UTC", time(9, 0), time(17, 0))
    assert not in_quiet_hours(datetime(2026, 11, 28, 18, 0, tzinfo=timezone.utc), "UTC", time(9, 0), time(17, 0))
    assert not in_quiet_hours(datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc), "UTC", time(9, 0), time(9, 0))


def test_quiet_hours_naive_datetime_treated_as_utc():
    assert in_quiet_hours(datetime(2026, 11, 28, 18, 0), "Asia/Karachi", time(22, 0), time(7, 0))
