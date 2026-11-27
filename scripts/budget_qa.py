"""Live budget journey on the local review stack with the synthetic role fixture.

Needs an active blocking purchase budget for QA Isolation A (created by an administrator on the Budgets & spend page).
Employee A submits a small purchase; Manager A sees the budget line on the request and the department's budget,
is refused the approval if it would exceed the budget, and then rejects the request so nothing stays committed.
Employee A is refused the budget list. Credentials stay in the protected fixture.

    python scripts/budget_qa.py
"""

from __future__ import annotations

import json
import sys
from contextlib import closing
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1] / ".verification-live-review-20261029"
API = "http://127.0.0.1:63011"
HEADERS = {"X-Requested-With": "InfraSpineAdmin"}


def session(account: dict) -> httpx.Client:
    c = httpx.Client(base_url=API, headers=HEADERS, timeout=60)
    c.post("/api/auth/login", json={"email": account["email"], "password": account["password"]}).raise_for_status()
    return c


def main() -> int:
    fixture = json.loads((ROOT / "role-qa-fixture.json").read_text(encoding="utf-8"))
    department = fixture["_departments"]["A"]
    out: dict = {}
    with closing(session(fixture["employee_a"])) as employee:
        created = employee.post("/api/service-requests", json={
            "category": "purchase", "department_group_id": department, "subject": "QA synthetic purchase (budget)",
            "description": "Synthetic budget test. Not a real purchase.", "amount": "20000", "currency": "PKR",
        })
        created.raise_for_status()
        request_id = created.json()["id"]
        out["employee_budget_list"] = employee.get("/api/org/budgets").status_code
    with closing(session(fixture["manager_a"])) as manager:
        out["manager_budgets"] = [
            {k: b[k] for k in ("name", "department_name", "amount", "approved", "committed", "remaining", "enforcement")}
            for b in manager.get("/api/org/budgets").json()["budgets"]
        ]
        item = next(i for i in manager.get("/api/service-requests").json()["items"] if i["id"] == request_id)
        out["request_budget_lines"] = item.get("budgets")
        # QA Isolation A uses an approval chain; below the CEO threshold the manager's step is the final one.
        approve = manager.post(f"/api/service-requests/{request_id}/approval-decision", json={"decision": "approve", "comment": "Trying to approve over budget (QA)."})
        out["manager_approve"] = {"status": approve.status_code, "message": approve.json().get("error", {}).get("message")}
        reject = manager.post(f"/api/service-requests/{request_id}/approval-decision", json={"decision": "reject", "comment": "Over budget (QA)."})
        out["manager_reject"] = reject.status_code
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
