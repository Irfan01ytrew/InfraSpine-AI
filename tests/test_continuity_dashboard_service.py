"""Step AJ slices 2–3: service wiring + dashboard API (real migrated PostgreSQL).

Seeds reuse the continuity acceptance fixtures in test_continuity.py
(synthetic company, critical server/payroll, BIA with RTO 60). The
platform approval and the recovery incident are seeded with raw SQL
because neither has a public creation path that fits a dashboard test:
the approval needs a chosen permission/scope, and recovery workspaces
are a separate tenant namespace from continuity tenants.
"""

import json
import uuid

from sqlalchemy import text

from app.continuity.dashboard_summary import RiskInput
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.test_continuity import (
    activation,
    approve,
    company,  # noqa: F401  (fixture reused from the continuity acceptance tests)
    plan_input,
    post,
    scenario,
)


def aj_path(tenant_id):
    return f"/api/continuity/scopes/{tenant_id}/dashboard"


def continuity_module_scope_id(client, tenant_id, user_id):
    """Platform scope id for a continuity tenant (for seeding approvals)."""
    from app.main import app
    from app.platform import sync as platform_sync

    async def _resolve(sessionmaker):
        async with sessionmaker() as db:
            scope_id, _status = await db.run_sync(
                lambda s: platform_sync.reconcile_module_member(
                    s.connection(),
                    "continuity",
                    uuid.UUID(tenant_id),
                    uuid.UUID(user_id),
                )
            )
            # run_sync only stages the sync rows; commit so the scope
            # id exists for the approval seed that follows.
            await db.commit()
            return scope_id

    return client.portal.call(_resolve, app.state.sessionmaker)


def seed_platform_approval(client, tenant_id, user_id, *, requested_by, hours=12):
    scope_id = continuity_module_scope_id(client, tenant_id, user_id)
    approval_id = str(uuid.uuid4())
    action = json.dumps(
        {"type": "test.action", "scope_id": str(scope_id), "params": {}}
    )
    run_db(
        lambda conn: conn.execute(
            text(
                "INSERT INTO platform_approval_requests"
                " (id, scope_id, action_type, action_hash, action, subject, amount, currency,"
                "  required_permission, requested_by, requested_via, status, created_at, expires_at, trace_id)"
                " VALUES (:id, :scope, 'test.action', :hash, CAST(:action AS jsonb), 'Test approval',"
                "  NULL, NULL, 'continuity.access', :by, 'web', 'pending', now(),"
                "  now() + make_interval(hours => :hours), :trace)"
            ),
            {
                "id": approval_id,
                "scope": scope_id,
                "hash": "a" * 64,
                "action": action,
                "by": requested_by,
                "hours": hours,
                "trace": str(uuid.uuid4()),
            },
        )
    )
    return approval_id


def seed_recovery_incident(tenant_id, user_id, *, severity="high", sla_hours=-1, status="detected"):
    """Seed a recovery workspace sharing the continuity tenant id.

    Recovery workspaces are a separate tenant namespace; the dashboard
    bridge resolves one only when a workspace with the same id exists
    (see app/continuity/dashboard.py). Seeding the same id here proves
    that bridge end to end.
    """
    target_id = str(uuid.uuid4())
    incident_id = str(uuid.uuid4())
    run_db(
        lambda conn: conn.execute(
            text("INSERT INTO recovery_tenants (id, name) VALUES (:t, 'AJ QA')"),
            {"t": tenant_id},
        )
    )
    run_db(
        lambda conn: conn.execute(
            text(
                "INSERT INTO recovery_members (tenant_id, user_id, role, active)"
                " VALUES (:t, :u, 'owner', true)"
            ),
            {"t": tenant_id, "u": user_id},
        )
    )
    run_db(
        lambda conn: conn.execute(
            text(
                "INSERT INTO recovery_targets"
                " (id, tenant_id, owner_id, name, adapter, resource, enabled,"
                "  threshold, window_seconds, sla_seconds, check_interval, failures)"
                " VALUES (:id, :t, :u, 'AJ target', 'test', 'res', true, 1, 300, 3600, 60, 0)"
            ),
            {"id": target_id, "t": tenant_id, "u": user_id},
        )
    )
    run_db(
        lambda conn: conn.execute(
            text(
                "INSERT INTO recovery_incidents"
                " (id, tenant_id, target_id, owner_id, fingerprint, title, source, category,"
                "  severity, status, sla_due, generation, version, occurrences, recovery_count,"
                "  diagnosis, archived, audit_head, audit_seq)"
                " VALUES (:id, :t, :target, :u, :fp, 'Database unreachable', 'test', 'outage',"
                "  :sev, :status, now() + make_interval(hours => :h),"
                "  1, 1, 1, 0, CAST('{}' AS jsonb), false, :audit_head, 0)"
            ),
            {
                "id": incident_id,
                "t": tenant_id,
                "target": target_id,
                "u": user_id,
                "fp": "fp-" + incident_id[:8],
                "sev": severity,
                "status": status,
                "h": sla_hours,
                "audit_head": "0" * 64,
            },
        )
    )
    return incident_id


def test_dashboard_matches_sources_and_counts(client, company):
    c = company
    login(client, USER_EMAIL)
    scene = scenario(client, c)
    plan = post(client, c["path"] + "/plans", plan_input(c))

    base = client.get(c["path"] + "/dashboard").json()
    mine = client.get(aj_path(c["tenant"]["id"])).json()
    # Every gap becomes one risk with the documented severity mapping;
    # the fixture's backup employee is available (NodeInput default),
    # so there is no missing-eligible-backup gap and nothing critical.
    from app.continuity.dashboard import GAP_SEVERITY

    expected_by_severity = {}
    for gap in base["gaps"]:
        sev = GAP_SEVERITY.get(gap["code"], "medium")
        expected_by_severity[sev] = expected_by_severity.get(sev, 0) + 1
    assert mine["risks"]["total"] == len(base["gaps"])
    assert mine["risks"]["by_severity"] == expected_by_severity
    assert mine["risks"]["uncovered_critical"] == 0
    assert mine["overall_status"] == "attention"  # risks open, none critical
    assert mine["health"] is None  # Step G roll-up not merged into this branch yet
    assert mine["incidents"]["total_active"] == 0  # no recovery workspace yet
    # A planner may open the dashboard but may not decide a plan/run
    # (validate_decision requires owner/approver), so the pending plan
    # decision is not listed as theirs to decide.
    assert mine["approvals"]["pending"] == 0

    login(client, USER2_EMAIL)  # approver: may decide the plan
    mine = client.get(aj_path(c["tenant"]["id"])).json()
    assert mine["approvals"]["pending"] == 1  # the plan decision
    assert mine["recovery"]["active"] == 0

    approve(client, c["path"] + f"/plans/{plan['id']}/decision", plan)
    mine = client.get(aj_path(c["tenant"]["id"])).json()
    assert mine["approvals"]["pending"] == 0

    run = activation(client, c, plan, scene)
    login(client, USER2_EMAIL)
    mine = client.get(aj_path(c["tenant"]["id"])).json()
    assert mine["approvals"]["pending"] == 1  # the run decision
    # A pending run already counts as in-flight: slice 1 defines
    # recovery.active over pending/active/suspended runs.
    assert mine["recovery"]["active"] == 1
    assert mine["recovery"]["average_progress_percent"] == 0.0

    approve(client, c["path"] + f"/runs/{run['id']}/decision", run)
    mine = client.get(aj_path(c["tenant"]["id"])).json()
    assert mine["approvals"]["pending"] == 0
    assert mine["recovery"]["active"] == 1
    assert mine["recovery"]["average_progress_percent"] == 0.0  # 1 step, 0 verified
    assert mine["recovery"]["at_risk"] == 0  # 30 min elapsed, RTO 60
    assert mine["recovery"]["items"][0]["verified_steps"] == 0
    assert mine["recovery"]["items"][0]["total_steps"] == 1


def test_dashboard_critical_uncovered_risk_highlight(client, company):
    c = company
    # Make the only eligible backup unavailable: the critical server
    # and payroll process then have no one who can take over.
    run_db(
        lambda conn: conn.execute(
            text(
                "UPDATE bc_nodes SET data = data || '{\"available\": false}'::jsonb"
                " WHERE id = :node"
            ),
            {"node": c["backup"]["id"]},
        )
    )
    login(client, USER_EMAIL)
    mine = client.get(aj_path(c["tenant"]["id"])).json()
    assert mine["risks"]["uncovered_critical"] >= 1
    assert mine["overall_status"] == "critical"
    assert "critical risk(s) have no recovery plan" in " | ".join(mine["highlights"])


def test_dashboard_platform_approval_and_recovery_incident(client, company):
    c = company
    seed_platform_approval(
        client, c["tenant"]["id"], c["ids"][USER_EMAIL], requested_by=c["ids"][USER2_EMAIL]
    )
    seed_recovery_incident(
        c["tenant"]["id"], c["ids"][USER_EMAIL], severity="high", sla_hours=-1
    )
    login(client, USER_EMAIL)
    mine = client.get(aj_path(c["tenant"]["id"])).json()
    assert mine["approvals"]["pending"] == 1
    assert mine["approvals"]["expiring_soon"] == 1  # expires in 12 h
    assert mine["approvals"]["own_requests"] == 0  # requested by someone else
    assert mine["incidents"]["total_active"] == 1
    assert mine["incidents"]["by_severity"] == {"high": 1}
    assert mine["incidents"]["overdue"] == 1  # SLA already passed
    assert mine["incidents"]["top"][0]["title"] == "Database unreachable"


def test_dashboard_404_for_non_member(client, company):
    c = company
    login(client, ADMIN_EMAIL)
    other = post(client, "/api/continuity/tenants", {"name": "Separate company"})
    login(client, USER2_EMAIL)
    assert client.get(aj_path(other["id"])).status_code == 404


def test_dashboard_403_for_viewer_role(client, company):
    c = company
    run_db(
        lambda conn: conn.execute(
            text(
                "UPDATE bc_members SET role='viewer' WHERE tenant_id=:t AND user_id=:u"
            ),
            {"t": c["tenant"]["id"], "u": c["ids"][USER_EMAIL]},
        )
    )
    login(client, USER_EMAIL)
    assert client.get(aj_path(c["tenant"]["id"])).status_code == 403


def test_dashboard_empty_scope_is_ok(client):
    login(client, ADMIN_EMAIL)
    tenant = post(client, "/api/continuity/tenants", {"name": "Empty company"})
    mine = client.get(aj_path(tenant["id"])).json()
    assert mine["overall_status"] == "ok"
    assert mine["risks"]["total"] == 0
    assert mine["incidents"]["total_active"] == 0
    assert mine["approvals"]["pending"] == 0
    assert mine["recovery"]["active"] == 0
    assert mine["recovery"]["average_progress_percent"] is None
    assert mine["highlights"] == []
    assert mine["health"] is None


def test_dashboard_department_limited_member_sees_only_own_scope(client, company):
    c = company
    run_db(
        lambda conn: conn.execute(
            text(
                "UPDATE bc_members SET all_departments=false,"
                " departments=CAST(:deps AS jsonb), role='planner'"
                " WHERE tenant_id=:t AND user_id=:u"
            ),
            {"deps": '["' + c["dep2"]["id"] + '"]', "t": c["tenant"]["id"], "u": c["ids"][USER_EMAIL]},
        )
    )
    login(client, USER_EMAIL)
    mine = client.get(aj_path(c["tenant"]["id"])).json()
    assert mine["risks"]["total"] == 0  # critical IT resources are out of scope
    assert mine["recovery"]["active"] == 0
    assert mine["overall_status"] == "ok"
    assert "Critical Database Server" not in str(mine)


def test_service_accepts_alert_and_prediction_inputs(client, company):
    c = company
    from app.main import app
    from app.continuity.access import scope_for
    from app.continuity.dashboard import continuity_dashboard_summary
    from app.db.models import User

    async def _summary(sessionmaker):
        async with sessionmaker() as db:
            user = await db.get(User, uuid.UUID(c["ids"][USER_EMAIL]))
            scope = await scope_for(db, uuid.UUID(c["tenant"]["id"]), user)
            return await continuity_dashboard_summary(
                db,
                scope,
                alerts=[
                    RiskInput(
                        id="alert:1",
                        title="Worker down",
                        severity="critical",
                        category="worker_down",
                        source="alert",
                    )
                ],
                predictions=[
                    RiskInput(
                        id="pred:1",
                        title="Supplier delay likely",
                        severity="high",
                        category="supplier",
                        source="prediction",
                        has_plan=True,
                    )
                ],
            )

    summary = client.portal.call(_summary, app.state.sessionmaker)
    base = client.get(c["path"] + "/dashboard").json()
    assert summary["risks"]["total"] == len(base["gaps"]) + 2
    assert summary["risks"]["by_severity"]["critical"] >= 1
    sources = {r["source"] for r in summary["risks"]["top"]}
    assert "alert" in sources or "prediction" in sources
