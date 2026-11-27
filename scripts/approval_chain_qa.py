"""Live multi-step approval journey on the local review stack with the synthetic role fixture.

Employee A submits a small and a large purchase request; Manager A (Employee A's manager) decides the
"Manager review" step of both. With a chain of "Manager review" then "CEO sign-off" from 100,000, the small
request is approved at once and the large one waits for the CEO. Credentials stay in the protected fixture.
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


def steps(item: dict) -> list[str]:
    return [f"{s['order']}. {s['label']}: {s['status']}" + (f" by {s['decided_by_email']}" if s["decided_by_email"] else "") for s in item["approval_steps"]]


def main() -> int:
    fixture = json.loads((ROOT / "role-qa-fixture.json").read_text(encoding="utf-8"))
    department = fixture["_departments"]["A"]
    out: dict = {}
    with closing(session(fixture["employee_a"])) as employee:
        created = {}
        for label, amount in (("small", "30000"), ("large", "150000")):
            response = employee.post("/api/service-requests", json={
                "category": "purchase", "department_group_id": department, "subject": f"QA chain purchase ({label})",
                "description": "Synthetic multi-step approval test. Not a real purchase.", "amount": amount, "currency": "PKR",
            })
            response.raise_for_status()
            created[label] = response.json()
        out["submitted"] = {label: steps(item) for label, item in created.items()}
        # The requester cannot decide their own request.
        own = employee.post(f"/api/service-requests/{created['small']['id']}/approval-decision", json={"decision": "approve", "comment": "self"})
        out["requester_self_approval"] = own.status_code
    with closing(session(fixture["manager_a"])) as manager:
        out["manager"] = {}
        for label, item in created.items():
            decided = manager.post(f"/api/service-requests/{item['id']}/approval-decision", json={"decision": "approve", "comment": "Manager review done (QA)."})
            body = decided.json()
            out["manager"][label] = {"http": decided.status_code, "status": body.get("status"), "steps": steps(body) if "approval_steps" in body else body}
    out["large_id"] = created["large"]["id"]
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
