"""Universal Personal AI Companion OS (Step AN, slice 1): pure-function planner.

Goals + user preferences + recorded progress -> an ordered task plan, plus
progress memory that only records what was actually reported.

Pure functions only: no database access, no network, no sending, no
execution. The API/persistence layer (slice 2) will fetch rows the signed-in
user owns (``companion.*`` permissions through ``app/platform``) and hand
them to these functions; the coordinator (slice 4) will act on a plan only
through an exact-action approval consumed via ``execute_once``.

Safety rules baked in here, not just in the docs:
- Per-user isolation: every input row carries ``user_id`` and must match the
  requesting ``user_id``; a row from another user is rejected, never merged.
- Progress is append-only evidence: a record is only what was reported.
  Completion (100%) requires a non-empty evidence note; nothing here ever
  marks a task done on its own.
- The planner only *flags* ``needs_approval`` for external actions
  (send/book/pay/share/publish/deploy). It never executes anything.
- Goal/task/preference text is data, never instructions: text is never
  parsed for permissions, payees or approval scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import Literal

GoalStatus = Literal["active", "archived", "completed"]
TaskStatus = Literal["todo", "in_progress", "done", "cancelled"]
Priority = Literal["low", "medium", "high"]
Language = Literal["en", "ur", "roman_urdu"]
# Action kinds that leave the Companion: always need an exact-action approval.
ActionKind = Literal["internal", "send", "book", "pay", "share", "publish", "deploy"]

EXTERNAL_ACTIONS: frozenset[ActionKind] = frozenset(
    {"send", "book", "pay", "share", "publish", "deploy"}
)
GOAL_STATUSES: tuple[GoalStatus, ...] = ("active", "archived", "completed")
TASK_STATUSES: tuple[TaskStatus, ...] = ("todo", "in_progress", "done", "cancelled")
PRIORITIES: tuple[Priority, ...] = ("low", "medium", "high")
LANGUAGES: tuple[Language, ...] = ("en", "ur", "roman_urdu")
PRIORITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}

DEFAULT_WEEKLY_CAPACITY_HOURS = 40.0
DEFAULT_STALL_DAYS = 14
DEFAULT_RETENTION_DAYS = 90


@dataclass(frozen=True)
class Goal:
    """One personal goal. Persisted in slice 2; pure data here."""

    id: str
    user_id: str
    title: str
    status: GoalStatus = "active"
    priority: Priority = "medium"
    deadline: date | None = None
    project: str | None = None


@dataclass(frozen=True)
class TaskInput:
    """One task belonging to a goal. ``action_kind`` decides approval need."""

    id: str
    user_id: str
    goal_id: str
    title: str
    status: TaskStatus = "todo"
    priority: Priority = "medium"
    due: date | None = None
    estimated_hours: float = 0.0
    action_kind: ActionKind = "internal"


@dataclass(frozen=True)
class Preferences:
    """Per-user Companion preferences."""

    user_id: str
    language: Language = "en"
    timezone: str = "Asia/Karachi"
    weekly_capacity_hours: float = DEFAULT_WEEKLY_CAPACITY_HOURS
    quiet_start: time = time(22, 0)
    quiet_end: time = time(7, 0)
    retention_days: int = DEFAULT_RETENTION_DAYS


@dataclass(frozen=True)
class ProgressRecord:
    """One append-only progress report. Evidence, not inference."""

    id: str
    user_id: str
    goal_id: str
    task_id: str | None
    percent: int  # 0..100, as reported
    note: str
    on: date


@dataclass(frozen=True)
class PlannedTask:
    """A task placed in the plan, with its ordering reason."""

    task: TaskInput
    rank: int  # 1 = do first
    overdue_days: int
    needs_approval: bool
    reason: str


@dataclass(frozen=True)
class GoalProgress:
    """Progress summary for one goal."""

    goal_id: str
    tasks_total: int
    tasks_done: int
    percent: int  # highest reported percent across the goal's records/tasks
    last_progress_on: date | None
    stalled: bool


@dataclass(frozen=True)
class Plan:
    """The ordered plan for one user on one day."""

    user_id: str
    on: date
    tasks: tuple[PlannedTask, ...] = ()
    overdue_count: int = 0
    total_estimated_hours: float = 0.0
    overloaded: bool = False
    warnings: tuple[str, ...] = ()


def _require_user(owner_id: str, row_user_id: str, what: str) -> None:
    if not row_user_id or row_user_id != owner_id:
        raise ValueError(f"{what} belongs to a different user")


def validate_goal(goal: Goal) -> None:
    if not goal.id.strip():
        raise ValueError("goal id must not be empty")
    if not goal.user_id.strip():
        raise ValueError("goal user_id must not be empty")
    if not goal.title.strip():
        raise ValueError("goal title must not be empty")
    if goal.status not in GOAL_STATUSES:
        raise ValueError(f"unknown goal status: {goal.status}")
    if goal.priority not in PRIORITIES:
        raise ValueError(f"unknown goal priority: {goal.priority}")


def validate_task(task: TaskInput) -> None:
    if not task.id.strip():
        raise ValueError("task id must not be empty")
    if not task.user_id.strip():
        raise ValueError("task user_id must not be empty")
    if not task.goal_id.strip():
        raise ValueError("task goal_id must not be empty")
    if not task.title.strip():
        raise ValueError("task title must not be empty")
    if task.status not in TASK_STATUSES:
        raise ValueError(f"unknown task status: {task.status}")
    if task.priority not in PRIORITIES:
        raise ValueError(f"unknown task priority: {task.priority}")
    if task.action_kind not in ("internal",) + tuple(EXTERNAL_ACTIONS):
        raise ValueError(f"unknown action kind: {task.action_kind}")
    if task.estimated_hours < 0:
        raise ValueError("estimated_hours must not be negative")


def validate_preferences(prefs: Preferences) -> None:
    if not prefs.user_id.strip():
        raise ValueError("preferences user_id must not be empty")
    if prefs.language not in LANGUAGES:
        raise ValueError(f"unknown language: {prefs.language}")
    if not prefs.timezone.strip():
        raise ValueError("timezone must not be empty")
    if not 0 < prefs.weekly_capacity_hours <= 168:
        raise ValueError("weekly_capacity_hours must be between 0 and 168")
    if prefs.retention_days < 1:
        raise ValueError("retention_days must be at least 1")


def _overdue_days(task: TaskInput, today: date) -> int:
    if task.due is None or task.due >= today:
        return 0
    return (today - task.due).days


def _sort_key(goal_priority: Priority, task: TaskInput, today: date) -> tuple:
    # Overdue first (most days overdue first), then earliest due date,
    # then goal priority, then task priority, then smallest effort.
    overdue = _overdue_days(task, today)
    due = task.due or date.max
    return (
        -overdue,
        due,
        -PRIORITY_WEIGHT[goal_priority],
        -PRIORITY_WEIGHT[task.priority],
        task.estimated_hours,
        task.title.lower(),
    )


def build_plan(
    user_id: str,
    goals: list[Goal],
    tasks: list[TaskInput],
    prefs: Preferences,
    today: date,
) -> Plan:
    """Order one user's open tasks into a plan for ``today``.

    Only ``active`` goals and ``todo``/``in_progress`` tasks are planned.
    Completed, cancelled and archived items never appear. Rows belonging to
    another user raise ValueError.
    """
    if not user_id.strip():
        raise ValueError("user_id must not be empty")
    validate_preferences(prefs)
    _require_user(user_id, prefs.user_id, "preferences")
    for goal in goals:
        validate_goal(goal)
        _require_user(user_id, goal.user_id, f"goal {goal.id}")
    active = {g.id: g for g in goals if g.status == "active"}
    planned: list[PlannedTask] = []
    overdue_count = 0
    warnings: list[str] = []
    for task in tasks:
        validate_task(task)
        _require_user(user_id, task.user_id, f"task {task.id}")
        if task.goal_id not in active:
            if task.status in ("todo", "in_progress"):
                warnings.append(
                    f"task {task.id} references missing/archived goal {task.goal_id}; skipped"
                )
            continue
        if task.status not in ("todo", "in_progress"):
            continue
        overdue = _overdue_days(task, today)
        if overdue:
            overdue_count += 1
        planned.append(
            PlannedTask(
                task=task,
                rank=0,  # filled after sorting
                overdue_days=overdue,
                needs_approval=task.action_kind in EXTERNAL_ACTIONS,
                reason=(
                    f"overdue by {overdue} day(s)"
                    if overdue
                    else (f"due {task.due.isoformat()}" if task.due else "no due date")
                ),
            )
        )
    planned.sort(key=lambda p: _sort_key(active[p.task.goal_id].priority, p.task, today))
    ranked = tuple(
        PlannedTask(
            task=p.task,
            rank=i + 1,
            overdue_days=p.overdue_days,
            needs_approval=p.needs_approval,
            reason=p.reason,
        )
        for i, p in enumerate(planned)
    )
    total_hours = round(sum(p.task.estimated_hours for p in ranked), 2)
    overloaded = total_hours > prefs.weekly_capacity_hours
    if overloaded:
        warnings.append(
            f"planned {total_hours}h exceeds weekly capacity "
            f"{prefs.weekly_capacity_hours}h"
        )
    return Plan(
        user_id=user_id,
        on=today,
        tasks=ranked,
        overdue_count=overdue_count,
        total_estimated_hours=total_hours,
        overloaded=overloaded,
        warnings=tuple(warnings),
    )


def record_progress(
    records: list[ProgressRecord],
    *,
    record_id: str,
    user_id: str,
    goal_id: str,
    task_id: str | None,
    percent: int,
    note: str,
    on: date,
) -> tuple[ProgressRecord, ...]:
    """Append one progress record. Returns the new record list.

    Rules: percent must be 0..100; completion (100) requires a non-empty
    evidence note; a record for another user, or a percent lower than the
    highest already recorded for the same task, is rejected (progress is
    evidence — it never goes backwards silently and is never invented).
    """
    if not record_id.strip():
        raise ValueError("record id must not be empty")
    if not user_id.strip():
        raise ValueError("user_id must not be empty")
    if not goal_id.strip():
        raise ValueError("goal_id must not be empty")
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    if percent == 100 and not note.strip():
        raise ValueError("completion (100%) requires an evidence note")
    for existing in records:
        _require_user(user_id, existing.user_id, f"progress record {existing.id}")
        if (
            task_id is not None
            and existing.task_id == task_id
            and percent < existing.percent
        ):
            raise ValueError(
                f"progress for task {task_id} cannot go backwards "
                f"({existing.percent}% -> {percent}%)"
            )
    record = ProgressRecord(
        id=record_id,
        user_id=user_id,
        goal_id=goal_id,
        task_id=task_id,
        percent=percent,
        note=note,
        on=on,
    )
    return tuple(records) + (record,)


def goal_progress(
    user_id: str,
    goals: list[Goal],
    tasks: list[TaskInput],
    records: list[ProgressRecord],
    today: date,
    stall_days: int = DEFAULT_STALL_DAYS,
) -> tuple[GoalProgress, ...]:
    """Per-goal progress: task counts, highest reported percent, stalled flag.

    A goal is stalled when it is active, has open tasks, and has no progress
    record in the last ``stall_days`` days. No record means 0% — never guessed.
    """
    if not user_id.strip():
        raise ValueError("user_id must not be empty")
    for goal in goals:
        validate_goal(goal)
        _require_user(user_id, goal.user_id, f"goal {goal.id}")
    for task in tasks:
        validate_task(task)
        _require_user(user_id, task.user_id, f"task {task.id}")
    for record in records:
        _require_user(user_id, record.user_id, f"progress record {record.id}")
    out: list[GoalProgress] = []
    for goal in goals:
        goal_tasks = [t for t in tasks if t.goal_id == goal.id]
        goal_records = [r for r in records if r.goal_id == goal.id]
        done = sum(1 for t in goal_tasks if t.status == "done")
        last = max((r.on for r in goal_records), default=None)
        percent = max((r.percent for r in goal_records), default=0)
        if done and len(goal_tasks) == done:
            percent = 100
        open_tasks = any(t.status in ("todo", "in_progress") for t in goal_tasks)
        stalled = (
            goal.status == "active"
            and open_tasks
            and (last is None or (today - last).days > stall_days)
        )
        out.append(
            GoalProgress(
                goal_id=goal.id,
                tasks_total=len(goal_tasks),
                tasks_done=done,
                percent=percent,
                last_progress_on=last,
                stalled=stalled,
            )
        )
    return tuple(out)


def next_action(
    user_id: str,
    goal: Goal,
    tasks: list[TaskInput],
    prefs: Preferences,
    today: date,
) -> PlannedTask | None:
    """The single top-ranked open task for one goal, or None if none is open."""
    validate_goal(goal)
    _require_user(user_id, goal.user_id, f"goal {goal.id}")
    plan = build_plan(user_id, [goal], tasks, prefs, today)
    return plan.tasks[0] if plan.tasks else None


def where_did_i_leave_off(
    user_id: str,
    goals: list[Goal],
    tasks: list[TaskInput],
    records: list[ProgressRecord],
    today: date,
) -> dict[str, dict]:
    """For each active goal: last progress (what/when) and the next action.

    Goals with no record say so explicitly — the Companion never invents
    a "last step".
    """
    summaries = {g.goal_id: g for g in goal_progress(user_id, goals, tasks, records, today)}
    out: dict[str, dict] = {}
    for goal in goals:
        if goal.status != "active":
            continue
        _require_user(user_id, goal.user_id, f"goal {goal.id}")
        goal_records = [r for r in records if r.goal_id == goal.id]
        last = max(goal_records, key=lambda r: (r.on, r.percent), default=None)
        out[goal.id] = {
            "goal_title": goal.title,
            "percent": summaries[goal.id].percent,
            "last_progress": (
                {"percent": last.percent, "note": last.note, "on": last.on.isoformat()}
                if last is not None
                else None
            ),
            "stalled": summaries[goal.id].stalled,
            "next_task": (
                (lambda p: {"id": p.task.id, "title": p.task.title, "due": p.task.due.isoformat() if p.task.due else None, "needs_approval": p.needs_approval})(p)
                if (p := _first_open_task(user_id, goal, tasks, today)) is not None
                else None
            ),
        }
    return out


def _first_open_task(
    user_id: str, goal: Goal, tasks: list[TaskInput], today: date
) -> PlannedTask | None:
    open_tasks = [
        t
        for t in tasks
        if t.goal_id == goal.id and t.status in ("todo", "in_progress")
    ]
    if not open_tasks:
        return None
    open_tasks.sort(key=lambda t: _sort_key(goal.priority, t, today))
    task = open_tasks[0]
    return PlannedTask(
        task=task,
        rank=1,
        overdue_days=_overdue_days(task, today),
        needs_approval=task.action_kind in EXTERNAL_ACTIONS,
        reason="next open task",
    )
