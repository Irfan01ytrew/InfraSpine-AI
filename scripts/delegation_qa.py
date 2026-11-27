"""Live approval delegation and reminder journey on the local review stack with the synthetic role fixture.

Needs an active delegation from Manager A to Employee B (set by an administrator on Help & requests). Employee A
submits two small purchases in QA Isolation A. Employee B, outside that department, is told about the waiting
step, can read the request through the delegation, and decides it on Manager A's behalf. The second request's step
is aged past the reminder interval and the reminder pass is run once; Employee B gets the reminder. Both requests
end rejected so nothing stays committed. Credentials stay in the protected fixture.

    python scripts/delegation_qa.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
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


def kinds(client: httpx.Client, request_id: str) -> list[str]:
    return sorted(n["notification_type"] for n in client.get("/api/service-requests/notifications").json()["items"] if n["request_id"] == request_id)


async def age_and_remind(step_request_id: str) -> int:
    engine = create_async_engine(stack.database_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE request_approval_steps SET pending_since = now() - interval '25 hours' WHERE request_id = :id AND status = 'pending'"),
                               {"id": uuid.UUID(step_request_id)})
        return await process_approval_reminders(async_sessionmaker(engine, expire_on_commit=False), now=datetime.now(UTC), after=timedelta(hours=24))
    finally:
        await engine.dispose()


def main() -> int:
    fixture = json.loads((ROOT / "role-qa-fixture.json").read_text(encoding="utf-8"))
    department = fixture["_departments"]["A"]
    out: dict = {}
    with closing(session(fixture["employee_a"])) as employee:
        ids = []
        for label in ("delegated decision", "reminder"):
            created = employee.post("/api/service-requests", json={
                "category": "purchase", "department_group_id": department, "subject": f"QA synthetic purchase ({label})",
                "description": "Synthetic delegation test. Not a real purchase.", "amount": "15000", "currency": "PKR",
            })
            created.raise_for_status()
            ids.append(created.json()["id"])
    first, second = ids
    with closing(session(fixture["employee_b"])) as deputy:
        out["delegations_seen_by_deputy"] = [(d["delegator_email"], d["delegate_email"], d["current"]) for d in deputy.get("/api/org/approval-delegations").json()["delegations"]]
        out["deputy_notifications_first"] = kinds(deputy, first)
        item = deputy.get(f"/api/service-requests/{first}")
        out["deputy_reads_request"] = item.status_code
        out["deputy_can_decide"] = item.json().get("can_decide_step")
        approve = deputy.post(f"/api/service-requests/{first}/approval-decision", json={"decision": "approve", "comment": "Approving for Manager A (QA)."})
        out["deputy_approve"] = {"status": approve.status_code, "message": approve.json().get("error", {}).get("message")}
        reject = deputy.post(f"/api/service-requests/{first}/approval-decision", json={"decision": "reject", "comment": "Rejected for Manager A while away (QA)."})
        step = reject.json()["approval_steps"][0]
        out["deputy_reject"] = {"status": reject.status_code, "decided_by": step["decided_by_email"], "on_behalf_of": step["on_behalf_of_email"]}
        out["reminders_sent"] = asyncio.run(age_and_remind(second))
        out["deputy_notifications_second"] = kinds(deputy, second)
        pending = deputy.get(f"/api/service-requests/{second}").json()["approval_steps"][0]
        out["second_step"] = {"status": pending["status"], "reminder_count": pending["reminder_count"]}
        cleanup = deputy.post(f"/api/service-requests/{second}/approval-decision", json={"decision": "reject", "comment": "QA cleanup."})
        out["second_cleanup"] = cleanup.status_code
    with closing(session(fixture["manager_a"])) as manager:
        out["manager_notifications_second"] = kinds(manager, second)
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
