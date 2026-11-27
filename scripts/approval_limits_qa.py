"""Live approval-limit journey on the local review stack with the synthetic role fixture.

Employee A (reports to Manager A, a Department Manager) submits two purchase requests in QA Isolation A;
Manager A, whose position may approve up to the configured limit, approves the small one and is refused
the large one, with the reason and any suggested approver. Credentials stay in the protected fixture.

    python scripts/approval_limits_qa.py submit      # create the two requests and print the manager's view
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
        ids = {}
        for label, amount in (("small", "30000"), ("large", "120000")):
            created = employee.post("/api/service-requests", json={
                "category": "purchase", "department_group_id": department, "subject": f"QA synthetic purchase ({label})",
                "description": "Synthetic approval-limit test. Not a real purchase.", "amount": amount, "currency": "PKR",
            })
            created.raise_for_status()
            ids[label] = created.json()["id"]
        out["requests"] = ids
    with closing(session(fixture["manager_a"])) as manager:
        items = {i["id"]: i for i in manager.get("/api/service-requests").json()["items"]}
        out["manager_view"] = {label: items[i]["approval"] for label, i in ids.items()}
        small = manager.post(f"/api/service-requests/{ids['small']}/triage", json={"status": "resolved", "comment": "Approved within my limit (QA)."})
        large = manager.post(f"/api/service-requests/{ids['large']}/triage", json={"status": "resolved", "comment": "Trying to approve above my limit (QA)."})
        out["manager_small"] = small.status_code
        out["manager_large"] = {"status": large.status_code, "message": large.json().get("error", {}).get("message")}
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
