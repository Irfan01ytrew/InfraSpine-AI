"""Tests for the Companion OS planner (Step AN, slice 1): pure functions.

Run: .venv/bin/python -m pytest tests/test_companion_planner.py --noconftest -q
(conftest needs Postgres, unavailable on this VM; the module is stdlib-only
so --noconftest is safe here.)
"""

from datetime import date, time

import pytest

from app.companion.planner import (
    Goal,
    Plan,
    Preferences,
    ProgressRecord,
    TaskInput,
    build_plan,
    goal_progress,
    next_action,
    record_progress,
    validate_goal,
    validate_preferences,
    validate_task,
    where_did_i_leave_off,
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
    base = {
        "id": tid,
        "user_id": USER,
        "goal_id": "g1",
        "title": "Do lesson 1",
    }
    base.update(kw)
    return TaskInput(**base)


# --- validation ---


def test_validate_goal_rejects_empty_title():
    with pytest.raises(ValueError):
        validate_goal(goal(title="  "))


def test_validate_goal_rejects_bad_status():
    with pytest.raises(ValueError):
        validate_goal(goal(status="dreaming"))  # type: ignore[arg-type]


def test_validate_task_rejects_negative_hours():
    with pytest.raises(ValueError):
        validate_task(task(estimated_hours=-1))


def test_validate_task_rejects_unknown_action_kind():
    with pytest.raises(ValueError):
        validate_task(task(action_kind="teleport"))  # type: ignore[arg-type]


def test_validate_preferences_rejects_bad_language():
    with pytest.raises(ValueError):
        validate_preferences(prefs(language="klingon"))  # type: ignore[arg-type]


def test_validate_preferences_rejects_zero_capacity():
    with pytest.raises(ValueError):
        validate_preferences(prefs(weekly_capacity_hours=0))


def test_validate_preferences_accepts_roman_urdu_and_quiet_hours():
    p = prefs(language="roman_urdu", quiet_start=time(23, 0), quiet_end=time(6, 0))
    validate_preferences(p)
    assert p.language == "roman_urdu"


# --- build_plan ordering ---


def test_plan_orders_overdue_before_future_due():
    plan = build_plan(
        USER,
        [goal()],
        [
            task("t1", due=date(2026, 12, 5)),
            task("t2", due=date(2026, 11, 20)),  # 8 days overdue
            task("t3", due=date(2026, 11, 25)),  # 3 days overdue
        ],
        prefs(),
        TODAY,
    )
    assert [p.task.id for p in plan.tasks] == ["t2", "t3", "t1"]
    assert plan.tasks[0].rank == 1
    assert plan.tasks[0].overdue_days == 8
    assert plan.overdue_count == 2


def test_plan_excludes_done_cancelled_and_archived():
    plan = build_plan(
        USER,
        [goal(), goal("g2", status="archived")],
        [
            task("t1", status="done"),
            task("t2", status="cancelled"),
            task("t3", goal_id="g2"),
            task("t4", status="in_progress"),
        ],
        prefs(),
        TODAY,
    )
    assert [p.task.id for p in plan.tasks] == ["t4"]


def test_plan_high_priority_goal_first_when_due_dates_equal():
    plan = build_plan(
        USER,
        [goal("g1", priority="low"), goal("g2", priority="high")],
        [task("t1"), task("t2", goal_id="g2")],
        prefs(),
        TODAY,
    )
    assert [p.task.id for p in plan.tasks] == ["t2", "t1"]


def test_plan_flags_external_actions_needing_approval():
    plan = build_plan(
        USER,
        [goal()],
        [
            task("t1", action_kind="internal"),
            task("t2", action_kind="send"),
            task("t3", action_kind="pay"),
            task("t4", action_kind="deploy"),
        ],
        prefs(),
        TODAY,
    )
    flagged = {p.task.id: p.needs_approval for p in plan.tasks}
    assert flagged == {"t1": False, "t2": True, "t3": True, "t4": True}


def test_plan_overload_warning_when_hours_exceed_capacity():
    plan = build_plan(
        USER,
        [goal()],
        [task("t1", estimated_hours=25), task("t2", estimated_hours=20)],
        prefs(weekly_capacity_hours=40),
        TODAY,
    )
    assert plan.total_estimated_hours == 45.0
    assert plan.overloaded is True
    assert any("exceeds weekly capacity" in w for w in plan.warnings)


def test_plan_no_overload_within_capacity():
    plan = build_plan(
        USER, [goal()], [task("t1", estimated_hours=5)], prefs(), TODAY
    )
    assert plan.overloaded is False
    assert plan.warnings == ()


def test_plan_warns_and_skips_task_with_missing_goal():
    plan = build_plan(
        USER, [goal()], [task("t1", goal_id="ghost"), task("t2")], prefs(), TODAY
    )
    assert [p.task.id for p in plan.tasks] == ["t2"]
    assert any("ghost" in w for w in plan.warnings)


def test_plan_empty_when_nothing_open():
    plan = build_plan(USER, [goal()], [], prefs(), TODAY)
    assert plan.tasks == ()
    assert isinstance(plan, Plan)


# --- per-user isolation ---


def test_plan_rejects_goal_from_another_user():
    with pytest.raises(ValueError, match="different user"):
        build_plan(USER, [goal(user_id=OTHER)], [], prefs(), TODAY)


def test_plan_rejects_task_from_another_user():
    with pytest.raises(ValueError, match="different user"):
        build_plan(USER, [goal()], [task(user_id=OTHER)], prefs(), TODAY)


def test_plan_rejects_preferences_from_another_user():
    with pytest.raises(ValueError, match="different user"):
        build_plan(USER, [goal()], [], prefs(user_id=OTHER), TODAY)


# --- record_progress ---


def test_record_progress_appends_and_returns_new_tuple():
    records = record_progress(
        [], record_id="r1", user_id=USER, goal_id="g1", task_id="t1",
        percent=30, note="lesson 1 half done", on=TODAY,
    )
    assert len(records) == 1
    assert records[0].percent == 30
    assert isinstance(records, tuple)


def test_record_progress_rejects_percent_over_100():
    with pytest.raises(ValueError):
        record_progress(
            [], record_id="r1", user_id=USER, goal_id="g1", task_id=None,
            percent=101, note="x", on=TODAY,
        )


def test_record_progress_completion_requires_evidence_note():
    with pytest.raises(ValueError, match="evidence note"):
        record_progress(
            [], record_id="r1", user_id=USER, goal_id="g1", task_id="t1",
            percent=100, note="   ", on=TODAY,
        )


def test_record_progress_completion_with_note_is_allowed():
    records = record_progress(
        [], record_id="r1", user_id=USER, goal_id="g1", task_id="t1",
        percent=100, note="certificate downloaded", on=TODAY,
    )
    assert records[0].percent == 100


def test_record_progress_never_goes_backwards_for_same_task():
    existing = (
        ProgressRecord(
            id="r1", user_id=USER, goal_id="g1", task_id="t1",
            percent=60, note="more than half", on=TODAY,
        ),
    )
    with pytest.raises(ValueError, match="cannot go backwards"):
        record_progress(
            list(existing), record_id="r2", user_id=USER, goal_id="g1",
            task_id="t1", percent=40, note="oops", on=TODAY,
        )


def test_record_progress_rejects_record_from_another_user_in_list():
    existing = [
        ProgressRecord(
            id="r1", user_id=OTHER, goal_id="g1", task_id="t1",
            percent=10, note="x", on=TODAY,
        )
    ]
    with pytest.raises(ValueError, match="different user"):
        record_progress(
            existing, record_id="r2", user_id=USER, goal_id="g1",
            task_id="t1", percent=20, note="y", on=TODAY,
        )


# --- goal_progress ---


def test_goal_progress_counts_tasks_and_uses_highest_percent():
    records = [
        ProgressRecord(id="r1", user_id=USER, goal_id="g1", task_id="t1",
                       percent=30, note="a", on=date(2026, 11, 20)),
        ProgressRecord(id="r2", user_id=USER, goal_id="g1", task_id="t2",
                       percent=50, note="b", on=date(2026, 11, 27)),
    ]
    (summary,) = goal_progress(
        USER, [goal()], [task("t1"), task("t2", status="done")], records, TODAY
    )
    assert summary.tasks_total == 2
    assert summary.tasks_done == 1
    assert summary.percent == 50
    assert summary.last_progress_on == date(2026, 11, 27)
    assert summary.stalled is False


def test_goal_progress_zero_when_no_records_and_not_guessed():
    (summary,) = goal_progress(USER, [goal()], [task("t1")], [], TODAY)
    assert summary.percent == 0
    assert summary.last_progress_on is None
    assert summary.stalled is True  # active, open task, no record in 14 days


def test_goal_progress_all_tasks_done_is_100():
    (summary,) = goal_progress(
        USER, [goal()], [task("t1", status="done")], [], TODAY
    )
    assert summary.percent == 100
    assert summary.stalled is False


def test_goal_progress_not_stalled_after_recent_record():
    records = [
        ProgressRecord(id="r1", user_id=USER, goal_id="g1", task_id=None,
                       percent=10, note="started", on=date(2026, 11, 27)),
    ]
    (summary,) = goal_progress(USER, [goal()], [task("t1")], records, TODAY)
    assert summary.stalled is False


# --- next_action / where_did_i_leave_off ---


def test_next_action_returns_top_ranked_open_task():
    top = next_action(
        USER, goal(),
        [task("t1", due=date(2026, 12, 1)), task("t2", due=date(2026, 11, 20))],
        prefs(), TODAY,
    )
    assert top is not None
    assert top.task.id == "t2"
    assert top.rank == 1


def test_next_action_none_when_nothing_open():
    assert (
        next_action(USER, goal(), [task("t1", status="done")], prefs(), TODAY) is None
    )


def test_where_did_i_leave_off_reports_last_step_and_next_task():
    records = [
        ProgressRecord(id="r1", user_id=USER, goal_id="g1", task_id="t1",
                       percent=40, note="lesson 2 done", on=date(2026, 11, 26)),
    ]
    out = where_did_i_leave_off(
        USER, [goal()], [task("t1"), task("t2")], records, TODAY
    )
    assert out["g1"]["percent"] == 40
    assert out["g1"]["last_progress"]["note"] == "lesson 2 done"
    assert out["g1"]["next_task"]["id"] == "t1"
    assert out["g1"]["stalled"] is False


def test_where_did_i_leave_off_says_none_when_no_record():
    out = where_did_i_leave_off(USER, [goal()], [task("t1")], [], TODAY)
    assert out["g1"]["last_progress"] is None
    assert out["g1"]["percent"] == 0
    assert out["g1"]["stalled"] is True


def test_where_did_i_leave_off_skips_archived_goals():
    out = where_did_i_leave_off(
        USER, [goal(), goal("g2", status="archived")], [task("t1")], [], TODAY
    )
    assert list(out) == ["g1"]
