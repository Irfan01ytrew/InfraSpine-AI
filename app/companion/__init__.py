"""Universal Personal AI Companion OS (Step AN).

Slice 1: pure-function goal/task planner + progress memory (``planner.py``).
Slice 2: persistence + per-user API + daily briefing (``service.py``,
``briefing.py``, ``app/api/routes/companion.py``, migration 0104).
Slice 3: per-integration consent + approval-gated email-digest outbox
(``service.py`` consent/digest sections, same migration).
"""

from app.companion.briefing import build_daily_briefing, in_quiet_hours
from app.companion.planner import build_plan, goal_progress, next_action, record_progress

__all__ = [
    "build_daily_briefing",
    "build_plan",
    "goal_progress",
    "in_quiet_hours",
    "next_action",
    "record_progress",
]
