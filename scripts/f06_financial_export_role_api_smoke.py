"""Non-destructive current-source API role smoke against the isolated local review DB."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


CSRF = {"X-Requested-With": "f06-role-smoke"}


@asynccontextmanager
async def _no_startup_side_effects(_app):
    """Keep TestClient from running app startup credential rebinding or background workers."""
    yield


def main() -> None:
    fixture_path = Path(os.environ["INFRASPINE_F06_ROLE_FIXTURE"])
    evidence_path = Path(os.environ["INFRASPINE_F06_ROLE_EVIDENCE"])
    fixture = json.loads(fixture_path.read_text(encoding="utf-8-sig"))
    admin = fixture.get("admin") or {}
    managers = fixture.get("managers") or []
    employees = fixture.get("employees") or []
    if not admin.get("email") or not admin.get("password") or not managers or not employees:
        raise RuntimeError("Protected synthetic role fixture is incomplete.")

    # Environment is pinned by the PowerShell runner to the retained synthetic review database.
    # No migration, test fixture, filesystem RAG component, worker or provider is initialized here.
    from app.core.config import get_settings
    from app.db.session import create_sessionmaker
    from app.main import app

    settings = get_settings()
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        poolclass=NullPool,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)
    app.router.lifespan_context = _no_startup_side_effects

    results: dict[str, int] = {}
    accounts = (("employee", employees[0], 403), ("manager", managers[0], 200), ("admin", admin, 200))
    with TestClient(app) as client:
        for role, account, expected_export_status in accounts:
            login = client.post(
                "/api/auth/login",
                json={"email": account["email"], "password": account["password"]},
                headers=CSRF,
            )
            if login.status_code != 200:
                raise RuntimeError(f"Synthetic {role} login returned HTTP {login.status_code}; no password or response body logged.")
            identity = login.json()
            if identity.get("role") != role:
                raise RuntimeError(f"Synthetic {role} fixture resolved to a different account role.")

            export = client.get("/api/rag/financial-extractions/bookkeeping.csv")
            results[role] = export.status_code
            if export.status_code != expected_export_status:
                raise RuntimeError(f"Financial CSV export for {role} returned HTTP {export.status_code}; expected {expected_export_status}.")
            if role in {"manager", "admin"} and not export.headers.get("content-type", "").startswith("text/csv"):
                raise RuntimeError(f"Authorized {role} export did not return CSV.")

            logout = client.post("/api/auth/logout", headers=CSRF)
            if logout.status_code != 204:
                raise RuntimeError(f"Synthetic {role} logout returned HTTP {logout.status_code}.")

    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps({"employee_export_http": results["employee"], "manager_export_http": results["manager"],
                    "admin_export_http": results["admin"], "database": "infraspine_ai_native_review",
                    "migrations_or_fixture_resets": False}, indent=2) + "\n",
        encoding="utf-8",
    )
    print("LOCAL_F06_CURRENT_SOURCE_EMPLOYEE_EXPORT_DENIED=PASS http=403")
    print("LOCAL_F06_CURRENT_SOURCE_MANAGER_EXPORT_ALLOWED=PASS http=200 csv=true")
    print("LOCAL_F06_CURRENT_SOURCE_ADMIN_EXPORT_ALLOWED=PASS http=200 csv=true")
    print("LOCAL_F06_CURRENT_SOURCE_API_ROLE_BOUNDARY=PASS migrations=none fixture_resets=none")


if __name__ == "__main__":
    main()
