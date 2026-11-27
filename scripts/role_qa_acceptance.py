"""Manager/Employee allow-and-deny acceptance against the local review stack.

Uses the synthetic accounts and department-restricted documents made by `role_qa_setup.py`. Each check
records what was expected and what happened; the result table goes to stdout and the full evidence to
`evidence/role-qa-<timestamp>.json`. Credentials are read from the protected fixture and never printed.

    python scripts/role_qa_acceptance.py            # API checks
    python scripts/role_qa_acceptance.py --chat     # also ask cited questions (slow on a local model)
"""

from __future__ import annotations

import json
from contextlib import closing
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / ".verification-live-review-20261029"
API = "http://127.0.0.1:63011"
HEADERS = {"X-Requested-With": "InfraSpineAdmin"}
DENIED = {401, 403, 404}

results: list[dict] = []


def check(role: str, name: str, expected: str, ok: bool, actual: str) -> None:
    results.append({"role": role, "check": name, "expected": expected, "actual": actual, "result": "PASS" if ok else "FAIL"})


def session(email: str, password: str) -> httpx.Client:
    c = httpx.Client(base_url=API, headers=HEADERS, timeout=300)
    response = c.post("/api/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    return c


def ask(c: httpx.Client, question: str) -> tuple[str, list[str]]:
    conversation = c.post("/api/conversations", json={}).json()["id"]
    response = c.post(f"/api/conversations/{conversation}/messages", json={"question": question})
    if response.status_code != 200:
        return f"HTTP {response.status_code}", []
    message = response.json()["assistant_message"]
    files = [cite.get("filename", "") for cite in (message.get("response") or {}).get("citations") or []]
    return message.get("content", ""), files


def main() -> int:
    chat = "--chat" in sys.argv
    fixture = json.loads((ROOT / "role-qa-fixture.json").read_text(encoding="utf-8"))
    admin_password = json.loads((ROOT / "credentials-fixture.json").read_text(encoding="utf-8-sig"))["admin@livereview.test"]["password"]
    departments, documents = fixture["_departments"], fixture["_documents"]
    names = {"A": "qa-isolation-a-synthetic.txt", "B": "qa-isolation-b-synthetic.txt"}
    plan = {"manager_a": "A", "employee_a": "A", "employee_b": "B"}

    for key, own in plan.items():
        account = fixture[key]
        other = "B" if own == "A" else "A"
        role = f"{account['role']} ({account['department']})"
        with closing(session(account["email"], account["password"])) as c:
            me = c.get("/api/auth/me").json()
            check(role, "signs in with the shared login and gets its own role", account["role"], me.get("role") == account["role"], str(me.get("role")))

            listed = {d["filename"] for d in c.get("/api/rag/documents").json()["documents"]}
            check(role, f"library lists own department document {names[own]}", "listed", names[own] in listed, "listed" if names[own] in listed else "missing")
            check(role, f"library hides other department document {names[other]}", "hidden", names[other] not in listed, "hidden" if names[other] not in listed else "VISIBLE")

            own_doc = c.get(f"/api/rag/documents/{documents[own]}")
            check(role, "opens own department document by id", "200", own_doc.status_code == 200, str(own_doc.status_code))
            for path in (f"/api/rag/documents/{documents[other]}", f"/api/rag/documents/{documents[other]}/file"):
                response = c.get(path)
                check(role, f"other department document denied: {path.split(documents[other])[-1] or '/'}", "401/403/404", response.status_code in DENIED, str(response.status_code))

            for path in ("/api/admin/users", "/api/admin/providers", "/api/admin/audit-logs", "/api/admin/access/groups", "/api/admin/access/grants", "/api/admin/deployment", "/api/admin/reports/usage"):
                response = c.get(path)
                check(role, f"admin endpoint denied {path}", "401/403", response.status_code in {401, 403}, str(response.status_code))

            managed = c.get("/api/departments/managed")
            managed_names = sorted(g["name"] for g in managed.json().get("groups", [])) if managed.status_code == 200 else []
            if account["role"] == "manager":
                check(role, "manages only its own department", f"['{account['department']}']", managed_names == [account["department"]], str(managed_names))
                members = c.get(f"/api/departments/{departments[own]}/members")
                check(role, "reads own department members", "200", members.status_code == 200, str(members.status_code))
                foreign = c.get(f"/api/departments/{departments[other]}/members")
                check(role, "other department members denied", "401/403/404", foreign.status_code in DENIED, str(foreign.status_code))
                add = c.post(f"/api/departments/{departments[other]}/members", json={"email": fixture['employee_a']['email']})
                check(role, "cannot add people to another department", "401/403/404", add.status_code in DENIED, str(add.status_code))
            else:
                check(role, "manages no department", "[] or denied", managed_names == [], f"{managed.status_code} {managed_names}")
                members = c.get(f"/api/departments/{departments[own]}/members")
                check(role, "cannot manage own department membership", "401/403/404", members.status_code in DENIED, str(members.status_code))

            if chat:
                # A plain business fact: "vault code" values are (rightly) redacted as credentials by the answer guard.
                window = {"A": ("Alpha", "9"), "B": ("Bravo", "4")}
                label, days = window[own]
                answer, cited = ask(c, f"How many days is the {label} review window?")
                check(role, "cited answer from own department document", f"{days} days, cited from {names[own]}", days in answer and names[own] in cited, f"{answer[:120]!r} cites {cited}")
                other_label, other_days = window[other]
                answer, cited = ask(c, f"How many days is the {other_label} review window?")
                other_code = "CEDAR-9902" if own == "A" else "TULIP-7701"
                leaked = names[other] in cited or other_code in answer or f"{other_days} days" in answer or f"{other_days} calendar" in answer
                check(role, "other department fact never appears in an answer", "no fact, no citation", not leaked, f"{answer[:120]!r} cites {cited}")

    # Revocation: removing a person from the department takes effect on their next request.
    employee = fixture["employee_a"]
    with httpx.Client(base_url=API, headers=HEADERS, timeout=60) as admin:
        admin.post("/api/auth/login", json={"email": "admin@livereview.test", "password": admin_password}).raise_for_status()
        user_id = next(u["id"] for u in admin.get("/api/admin/users").json()["users"] if u["email"] == employee["email"])
        with closing(session(employee["email"], employee["password"])) as c:
            removed = admin.delete(f"/api/admin/access/groups/{departments['A']}/members/{user_id}")
            listed = {d["filename"] for d in c.get("/api/rag/documents").json()["documents"]}
            direct = c.get(f"/api/rag/documents/{documents['A']}")
            check("employee (QA Isolation A)", "after removal from A: document hidden on the next request (same session)", "hidden + 401/403/404", removed.status_code in {200, 204} and names["A"] not in listed and direct.status_code in DENIED, f"remove {removed.status_code}, listed={names['A'] in listed}, direct {direct.status_code}")
            restored = admin.post(f"/api/admin/access/groups/{departments['A']}/members", json={"email": employee["email"]})
            listed = {d["filename"] for d in c.get("/api/rag/documents").json()["documents"]}
            check("employee (QA Isolation A)", "after re-adding to A: document visible again", "listed", restored.status_code in {200, 204} and names["A"] in listed, f"re-add {restored.status_code}, listed={names['A'] in listed}")

    evidence = ROOT / "evidence" / f"role-qa-{datetime.now():%Y%m%d-%H%M%S}.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    failed = [r for r in results if r["result"] == "FAIL"]
    for r in results:
        print(f"{r['result']:4}  {r['role']:<32} {r['check']}  [{r['actual']}]")
    print(f"\n{len(results) - len(failed)}/{len(results)} PASS  evidence: {evidence.relative_to(REPO)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
