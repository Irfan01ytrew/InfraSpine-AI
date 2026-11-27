"""Create the synthetic Manager/Employee QA fixture on the local review stack (idempotent).

Two departments ("QA Isolation A/B"), a manager of A, an employee in A and an employee in B, each
created through the real invitation flow, plus one department-restricted synthetic document per
department. Generated passwords are written only to the protected review fixture file and are never
printed. Runs against the loopback review API only.
"""

from __future__ import annotations

import json
import secrets
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / ".verification-live-review-20261029"
ADMIN_FIXTURE = ROOT / "credentials-fixture.json"
ROLE_FIXTURE = ROOT / "role-qa-fixture.json"
API = "http://127.0.0.1:63011"
HEADERS = {"X-Requested-With": "InfraSpineAdmin"}

DEPARTMENTS = {"A": "QA Isolation A", "B": "QA Isolation B"}
ACCOUNTS = {
    "manager_a": {"email": "qa.manager.a@livereview.test", "role": "manager", "dept": "A"},
    "employee_a": {"email": "qa.employee.a@livereview.test", "role": "employee", "dept": "A"},
    "employee_b": {"email": "qa.employee.b@livereview.test", "role": "employee", "dept": "B"},
}
DOCUMENTS = {
    "A": ("qa-isolation-a-synthetic.txt", "SYNTHETIC QA ONLY. Department A access test. Alpha vault code: TULIP-7701. Alpha review window: 9 days."),
    "B": ("qa-isolation-b-synthetic.txt", "SYNTHETIC QA ONLY. Department B access test. Bravo vault code: CEDAR-9902. Bravo review window: 4 days."),
}


def client() -> httpx.Client:
    return httpx.Client(base_url=API, headers=HEADERS, timeout=60)


def login(c: httpx.Client, email: str, password: str) -> None:
    response = c.post("/api/auth/login", json={"email": email, "password": password})
    response.raise_for_status()


def main() -> int:
    if not API.startswith("http://127.0.0.1"):
        raise SystemExit("The role QA fixture only runs against the loopback review API.")
    admin = json.loads(ADMIN_FIXTURE.read_text(encoding="utf-8-sig"))["admin@livereview.test"]
    saved = json.loads(ROLE_FIXTURE.read_text(encoding="utf-8")) if ROLE_FIXTURE.exists() else {}
    with client() as c:
        login(c, "admin@livereview.test", admin["password"])
        groups = {g["name"]: g for g in c.get("/api/admin/access/groups").json()["groups"]}
        dept_ids = {}
        for key, name in DEPARTMENTS.items():
            if name not in groups:
                created = c.post("/api/admin/access/groups", json={"name": name, "kind": "department"})
                created.raise_for_status()
                groups[name] = created.json()
            dept_ids[key] = groups[name]["id"]

        users = {u["email"]: u for u in c.get("/api/admin/users").json()["users"]}
        for key, spec in ACCOUNTS.items():
            if spec["email"] in users and key in saved:
                continue
            if spec["email"] in users:
                raise SystemExit(f"{spec['email']} exists but its password is not in the fixture; reset it manually.")
            invite = c.post("/api/admin/users/invitations", json={"email": spec["email"], "role": spec["role"], "department_ids": [dept_ids[spec["dept"]]]})
            invite.raise_for_status()
            password = "Qa-" + secrets.token_urlsafe(18)
            with client() as anonymous:
                accepted = anonymous.post("/api/onboarding/accept", json={"token": invite.json()["token"], "password": password})
                accepted.raise_for_status()
            saved[key] = {"email": spec["email"], "role": spec["role"], "department": DEPARTMENTS[spec["dept"]], "password": password}
            ROLE_FIXTURE.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")

        docs = {d["filename"]: d for d in c.get("/api/rag/documents").json()["documents"]}
        doc_ids = {}
        for key, (filename, text) in DOCUMENTS.items():
            if filename not in docs:
                uploaded = c.post(
                    "/api/rag/documents",
                    files={"file": (filename, text.encode("utf-8"), "text/plain")},
                    data={"visibility": "restricted", "department_id": dept_ids[key]},
                )
                uploaded.raise_for_status()
                docs[filename] = uploaded.json()
            doc_ids[key] = docs[filename]["id"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            states = {d["filename"]: d["status"] for d in c.get("/api/rag/documents").json()["documents"]}
            if all(states.get(name) == "ready" for name, _ in DOCUMENTS.values()):
                break
            time.sleep(3)
        saved["_departments"] = dept_ids
        saved["_documents"] = doc_ids
        ROLE_FIXTURE.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"departments": dept_ids, "documents": doc_ids, "accounts": [v["email"] for k, v in saved.items() if not k.startswith("_")], "doc_states": states}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
