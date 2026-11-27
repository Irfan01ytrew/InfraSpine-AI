"""Live escalation journey on the local review stack with the synthetic role fixture.

Employee A submits a small purchase in QA Isolation A. Its step is aged as if the last of three reminders had
gone unanswered 25 hours ago, and one reminder pass is run: the step is escalated to Manager A's own manager, or
the administrators when Manager A has none. The request is then rejected (by Employee B, who holds Manager A's
delegation, when it is active; otherwise by Manager A) so nothing stays committed. Credentials stay in the fixture.

    python scripts/escalation_qa.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import start_live_review_stack as stack  # noqa: E402
from app.services.approval_reminders import process_approval_reminders  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / ".verification-live-review-20261029"
API = "http://127.0.0.1:63011"
HEADERS = {"X-Requested-With": "InfraSpineAdmin"}


def session(account: dict) -> httpx.Client:
    c = httpx.Client(base_url=API, headers=HEADERS, timeout=60)
    c.post("/api/auth/login", json={"email": account["email"], "password": account["password"]}).raise_for_status()
    return c


async def age_and_escalate(request_id: str) -> tuple[int, list[str]]:
    engine = create_async_engine(stack.database_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "UPDATE request_approval_steps SET pending_since = now() - interval '4 days', reminder_count = 3, "
                "last_reminded_at = now() - interval '25 hours' WHERE request_id = :id AND status = 'pending'"), {"id": uuid.UUID(request_id)})
        count = await process_approval_reminders(async_sessionmaker(engine, expire_on_commit=False), now=datetime.now(UTC))
        async with engine.connect() as conn:
            recipients = (await conn.execute(text(
                "SELECT u.email FROM service_request_notifications n JOIN users u ON u.id = n.recipient_user_id "
                "WHERE n.request_id = :id AND n.notification_type = 'approval_step_escalated' ORDER BY u.email"), {"id": uuid.UUID(request_id)})).scalars().all()
        return count, list(recipients)
    finally:
        await engine.dispose()


def main() -> int:
    fixture = json.loads((ROOT / "role-qa-fixture.json").read_text(encoding="utf-8"))
    out: dict = {}
    with closing(session(fixture["employee_a"])) as employee:
        created = employee.post("/api/service-requests", json={
            "category": "purchase", "department_group_id": fixture["_departments"]["A"], "subject": "QA synthetic purchase (escalation)",
            "description": "Synthetic escalation test. Not a real purchase.", "amount": "12000", "currency": "PKR",
        })
        created.raise_for_status()
        request_id = created.json()["id"]
    out["steps_escalated"], out["escalated_to"] = asyncio.run(age_and_escalate(request_id))
    for who in ("employee_b", "manager_a"):
        with closing(session(fixture[who])) as approver:
            item = approver.get(f"/api/service-requests/{request_id}").json()
            if not item.get("can_decide_step"):
                continue
            step = item["approval_steps"][0]
            out["step_after_escalation"] = {"reminder_count": step["reminder_count"], "escalated": bool(step["escalated_at"])}
            out["timeline_note"] = next(e["comment"] for e in item["events"] if (e["comment"] or "").startswith("Escalated"))
            out["cleanup"] = {"by": who, "status": approver.post(f"/api/service-requests/{request_id}/approval-decision",
                                                                 json={"decision": "reject", "comment": "QA cleanup (escalation)."}).status_code}
            break
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
