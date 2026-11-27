from datetime import datetime, timedelta, timezone

import pytest

from app.services.personal_task_reminders import reminder_type_for_due_at


def test_personal_task_reminder_classifier_uses_exact_deadline_and_requires_timezone():
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    assert reminder_type_for_due_at(now + timedelta(seconds=1), now) == "due_soon"
    assert reminder_type_for_due_at(now, now) == "overdue"
    with pytest.raises(ValueError, match="timezone-aware"):
        reminder_type_for_due_at(datetime(2026, 10, 26, 12), now)
