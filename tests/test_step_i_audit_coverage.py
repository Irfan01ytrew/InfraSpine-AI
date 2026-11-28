"""Step I slice 4: audit coverage of the privileged admin surface + platform
gate for the remaining legacy-only admin routers.

* The inventory in app/security/audit_coverage.py must list every
  state-changing route under /api/admin/* (70 on HEAD 20dc227) and every
  listed action must be audited or read-only by effect — coverage 1.0.
* Every migrated router still gives non-admins the same 403 and consults
  org_gate("platform.manage") for admins (same pattern as slice 3).
* The four actions that had no audit event before this slice (the three
  engine health checks and the department quota update) now write one:
  the tests below call the real endpoints and read the AuditLog rows back.
"""

import uuid

import pytest
from sqlalchemy import select

import app.platform.modules as platform_modules
from app.db.models import AuditLog
from app.main import app
from app.security.audit_coverage import PRIVILEGED_ADMIN_ACTIONS, measure_audit_coverage
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login, run_db


def _audit_rows(action: str) -> list[dict]:
    async def _q(conn):
        rows = (
            await conn.execute(
                select(AuditLog.action, AuditLog.actor_email, AuditLog.outcome).where(
                    AuditLog.action == action
                )
            )
        ).all()
        return [{"action": a, "actor_email": e, "outcome": o} for a, e, o in rows]

    return run_db(_q)


# --- Inventory: completeness and coverage -----------------------------------

def test_inventory_covers_every_state_changing_admin_route():
    report = measure_audit_coverage(app)
    assert not report.uninventoried, (
        "State-changing /api/admin/* routes missing from PRIVILEGED_ADMIN_ACTIONS:\n"
        + "\n".join(report.uninventoried)
    )
    assert not report.stale, (
        "Inventory entries for routes that no longer exist:\n" + "\n".join(report.stale)
    )
    # Guard against silent shrinkage of the inventory or the route walk.
    assert report.total == len(PRIVILEGED_ADMIN_ACTIONS) == 70, (
        f"Expected 70 state-changing admin routes, got {report.total} "
        f"({len(PRIVILEGED_ADMIN_ACTIONS)} inventoried)"
    )


def test_every_privileged_admin_action_is_audited_or_read_only():
    report = measure_audit_coverage(app)
    assert not report.uncovered, (
        "Privileged admin actions with no audit event:\n" + "\n".join(report.uncovered)
    )
    assert report.coverage == 1.0


# --- Platform gate: same 403s, gate consulted -------------------------------

# One probe endpoint per router migrated in slice 4 (read-only where the
# router has one; the quota PUT and retention preview otherwise).
MIGRATED_ROUTER_PROBES = [
    ("GET", "/api/admin/embedding-models", None),
    ("GET", "/api/admin/media-transcription", None),
    ("GET", "/api/admin/ocr-engine", None),
    ("GET", "/api/admin/document-parsers", None),
    ("GET", "/api/admin/traces", None),
    ("GET", "/api/admin/access/groups", None),
    ("GET", "/api/admin/deployment", None),
    ("GET", "/api/admin/platform/org-settings", None),
    ("GET", "/api/admin/providers", None),
    ("PUT", "/api/admin/reports/quotas/00000000-0000-0000-0000-000000000000",
     {"monthly_token_quota": 20000, "alert_threshold_percent": 90.0}),
    ("POST", "/api/admin/agent-runs/retention/preview", {"retention_days": 30}),
]


@pytest.mark.parametrize("method,path,body", MIGRATED_ROUTER_PROBES)
def test_non_admin_still_gets_403_on_migrated_routers(client, method, path, body):
    login(client, USER_EMAIL)
    response = client.request(method, path, json=body) if body is not None else client.request(method, path)
    assert response.status_code == 403, f"{method} {path}: {response.status_code} {response.text[:200]}"


@pytest.mark.parametrize(
    "path",
    ["/api/admin/embedding-models", "/api/admin/access/groups", "/api/admin/deployment"],
)
def test_platform_gate_is_consulted_for_migrated_routers(client, monkeypatch, path):
    calls: list[str] = []
    original = platform_modules.org_gate

    async def spy(*args, **kwargs):
        calls.append(args[2] if len(args) > 2 else kwargs.get("permission"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(platform_modules, "org_gate", spy)
    platform_modules.clear_diffs()
    login(client, ADMIN_EMAIL)
    response = client.get(path)
    assert response.status_code == 200, f"{path}: {response.text[:200]}"
    assert "platform.manage" in calls
    # Legacy and platform agree for an active administrator: no shadow diff.
    assert platform_modules.recent_diffs() == []


# --- Behavioural audit proof: the four gap-filled actions -------------------

def test_embedding_model_test_writes_audit_event(client):
    login(client, ADMIN_EMAIL)
    response = client.post("/api/admin/embedding-models/test")
    assert response.status_code == 200, response.text
    rows = _audit_rows("admin.embedding_model_tested")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == ADMIN_EMAIL
    assert rows[0]["outcome"] == ("success" if response.json()["ok"] else "failure")


def test_ocr_engine_test_writes_audit_event(client):
    login(client, ADMIN_EMAIL)
    response = client.post("/api/admin/ocr-engine/test")
    assert response.status_code == 200, response.text
    rows = _audit_rows("admin.ocr_engine_tested")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == ADMIN_EMAIL


def test_document_parser_test_writes_audit_event(client):
    login(client, ADMIN_EMAIL)
    response = client.post("/api/admin/document-parsers/txt/test")
    assert response.status_code == 200, response.text
    rows = _audit_rows("admin.document_parser_tested")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == ADMIN_EMAIL


def test_department_quota_update_writes_audit_event(client):
    login(client, ADMIN_EMAIL)
    department_id = str(uuid.uuid4())
    response = client.put(
        f"/api/admin/reports/quotas/{department_id}",
        json={"monthly_token_quota": 25000, "alert_threshold_percent": 85.0},
    )
    assert response.status_code == 200, response.text
    rows = _audit_rows("admin.department_quota_updated")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == ADMIN_EMAIL


def test_access_group_create_writes_audit_event(client):
    login(client, ADMIN_EMAIL)
    response = client.post(
        "/api/admin/access/groups", json={"name": "Audit Coverage Group", "kind": "department"}
    )
    assert response.status_code == 201, response.text
    rows = _audit_rows("access.group_created")
    assert len(rows) == 1
    assert rows[0]["actor_email"] == ADMIN_EMAIL
