"""Universal Personal AI Companion OS (Step AN, slice 2): daily briefing + digest.

Pure functions only: no database access, no network, no sending. The service
layer (``service.py``) loads only rows the signed-in user owns and hands them
to these functions; the planner (slice 1) already rejects rows from another
user, so a briefing can never mix in someone else's goals, tasks or progress.

A digest is the user's own data, rendered as plain text for one email. It is
queued through an approval-gated outbox (slice 3) and never sent during the
user's quiet hours.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.companion.planner import (
    Goal,
    GoalProgress,
    Plan,
    Preferences,
    ProgressRecord,
    TaskInput,
    build_plan,
    goal_progress,
    where_did_i_leave_off,
)


@dataclass(frozen=True)
class DailyBriefing:
    """Everything the user needs for one day, from their own data only."""

    user_id: str
    on: date
    plan: Plan
    goals: tuple[GoalProgress, ...]
    leave_off: dict[str, dict]
    stalled_goal_ids: tuple[str, ...]


def build_daily_briefing(
    user_id: str,
    goals: list[Goal],
    tasks: list[TaskInput],
    prefs: Preferences,
    records: list[ProgressRecord],
    today: date,
) -> DailyBriefing:
    """Build one user's briefing for ``today``.

    Only ``active`` goals and open tasks are planned; completed, cancelled
    and archived items never appear. Rows belonging to another user raise
    ValueError (from the planner) — never merged, never leaked.
    """
    plan = build_plan(user_id, goals, tasks, prefs, today)
    progress = goal_progress(user_id, goals, tasks, records, today)
    leave_off = where_did_i_leave_off(user_id, goals, tasks, records, today)
    stalled = tuple(g.goal_id for g in progress if g.stalled)
    return DailyBriefing(
        user_id=user_id,
        on=today,
        plan=plan,
        goals=progress,
        leave_off=leave_off,
        stalled_goal_ids=stalled,
    )


def render_digest_subject(briefing: DailyBriefing) -> str:
    """Subject line for the daily digest email."""
    return f"Your day, {briefing.on.isoformat()} — {briefing.plan.overdue_count} overdue, {len(briefing.plan.tasks)} to do"


def render_digest_text(briefing: DailyBriefing, *, user_email: str) -> str:
    """Plain-text digest body, built only from the briefing owner's own data.

    No goal/task text from any other user can reach this function: the
    briefing builder rejects foreign rows before any rendering happens.
    """
    lines = [
        f"Your Companion briefing for {briefing.on.isoformat()}",
        "",
        f"Overdue: {briefing.plan.overdue_count} task(s). "
        f"Planned: {len(briefing.plan.tasks)} task(s), "
        f"{briefing.plan.total_estimated_hours}h estimated.",
    ]
    if briefing.plan.overloaded:
        lines.append("Your planned work exceeds your weekly capacity.")
    lines.append("")
    if briefing.plan.tasks:
        lines.append("Today's plan (in order):")
        for item in briefing.plan.tasks:
            due = f"due {item.task.due.isoformat()}" if item.task.due else "no due date"
            approval = " [needs approval]" if item.needs_approval else ""
            lines.append(f"  {item.rank}. {item.task.title} ({due}){approval}")
        lines.append("")
    if briefing.stalled_goal_ids:
        lines.append("Stalled goals (no progress in 14 days):")
        for goal_id in briefing.stalled_goal_ids:
            title = briefing.leave_off.get(goal_id, {}).get("goal_title", goal_id)
            lines.append(f"  - {title}")
        lines.append("")
    lines.append("Progress per goal:")
    for summary in briefing.goals:
        title = briefing.leave_off.get(summary.goal_id, {}).get("goal_title", summary.goal_id)
        lines.append(
            f"  - {title}: {summary.percent}% "
            f"({summary.tasks_done}/{summary.tasks_total} tasks done)"
        )
    if briefing.plan.warnings:
        lines.append("")
        lines.append("Notes:")
        for warning in briefing.plan.warnings:
            lines.append(f"  - {warning}")
    lines.append("")
    lines.append("This digest was built only from your own goals, tasks and progress.")
    lines.append(f"Sent to {user_email} by your Personal AI Companion.")
    return "\n".join(lines) + "\n"


def in_quiet_hours(now: datetime, timezone: str, start: time, end: time) -> bool:
    """True when ``now`` falls inside the user's quiet-hours window.

    ``start == end`` means an empty window (never quiet). A window that
    crosses midnight (start > end) is quiet from ``start`` until ``end`` the
    next day. ``now`` may be naive (treated as UTC) or aware.
    """
    if start == end:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    local = now.astimezone(ZoneInfo(timezone)).time()
    if start < end:
        return start <= local < end
    return local >= start or local < end
