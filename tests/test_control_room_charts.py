"""Step AF slice 2: chart + health API. Real PostgreSQL *_test only.

Covers: the charts endpoint is read-only and permission-filtered (spend
series + invoice status counts come only from groups the caller may read
with ``finance.read``); run counts + recent failed runs for the scope;
health strip values; a non-member gets 403 and an unknown scope 404.
"""

from datetime import date, timedelta
from uuid import uuid4

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL, login, run_db


def seed_scope(client):
    scope = uuid4()

    async def seed(conn):
        await conn.execute(
            text("INSERT INTO access_groups(id,name,kind,source) VALUES (:id,'Control room','department','local')"),
            {"id": scope},
        )
        users = (await conn.execute(text("SELECT id,email FROM users"))).all()
        for uid, email in users:
            if email in (ADMIN_EMAIL, USER_EMAIL):
                await conn.execute(
                    text("INSERT INTO access_group_members(group_id,user_id) VALUES (:g,:u)"),
                    {"g": scope, "u": uid},
                )
        from app.platform.sync import backfill_all

        await conn.run_sync(backfill_all)
        return {email: str(uid) for uid, email in users}

    people = run_db(seed)
    login(client, ADMIN_EMAIL)
    for email, role in ((ADMIN_EMAIL, "finance.admin"), (USER_EMAIL, "finance.verifier")):
        response = client.post(
            f"/api/finance/scopes/{scope}/roles", json={"user_id": people[email], "role": role}
        )
        assert response.status_code == 201, response.text
    return str(scope), people


def seed_invoices(scope, people):
    """2 verified (this month + 40 days ago), 1 pending, 1 exception, 1 old paid."""
    batch = uuid4()
    today = date.today()
    rows = [
        ("verified", "1000.00", today, "Ops"),
        ("verified", "500.00", today - timedelta(days=40), "Ops"),
        ("pending", "250.00", today, "Ops"),
        ("exception", "75.00", today, "Finance"),
        ("paid", "2000.00", today - timedelta(days=400), "Ops"),
    ]

    async def seed(conn):
        await conn.execute(
            text(
                "INSERT INTO fin_invoice_batches(id,group_id,name,created_by,status,currency,tolerance_pct,tolerance_abs)"
                " VALUES (:id,:g,'November',:by,'open','PKR',2.00,0)"
            ),
            {"id": batch, "g": scope, "by": people[ADMIN_EMAIL]},
        )
        for seq, (status, amount, day, dept) in enumerate(rows):
            await conn.execute(
                text(
                    "INSERT INTO fin_invoices(id,group_id,batch_id,seq,invoice_number,source_kind,"
                    "source_digest,status,amount,currency,invoice_date,department)"
                    " VALUES (:id,:g,:b,:seq,:num,'json','digest',:st,:amt,'PKR',:d,:dept)"
                ),
                {
                    "id": uuid4(),
                    "g": scope,
                    "b": batch,
                    "seq": seq,
                    "num": f"INV-{seq}",
                    "st": status,
                    "amt": amount,
                    "d": day,
                    "dept": dept,
                },
            )

    run_db(seed)
    return today, rows


def seed_runs(scope, people):
    workflow = uuid4()
    version = uuid4()
    failed = uuid4()
    completed = uuid4()

    async def seed(conn):
        await conn.execute(
            text(
                "INSERT INTO wf_definitions(id,scope_id,owner_id,name,description,latest_version,enabled)"
                " VALUES (:id,:s,:o,'Control room workflow','',1,false)"
            ),
            {"id": workflow, "s": scope, "o": people[USER_EMAIL]},
        )
        await conn.execute(
            text(
                "INSERT INTO wf_versions(id,scope_id,workflow_id,number,spec,digest,status,author_id)"
                " VALUES (:id,:s,:w,1,'{}'::jsonb,'x','published',:o)"
            ),
            {"id": version, "s": scope, "w": workflow, "o": people[USER_EMAIL]},
        )
        for run_id, status, finished in (
            (failed, "failed", True),
            (completed, "completed", True),
            (uuid4(), "queued", False),
        ):
            await conn.execute(
                text(
                    "INSERT INTO wf_runs(id,scope_id,workflow_id,version_id,requester_id,"
                    "idempotency_key,input_digest,payload,status,phase,event_sequence,event_hash,"
                    "failure_code,finished_at,simulation,priority)"
                    " VALUES (:id,:s,:w,:v,:o,:key,'digest','{}'::jsonb,:st,'done',0,"
                    "'0000000000000000000000000000000000000000000000000000000000000000',"
                    ":code, CASE WHEN :fin THEN now() ELSE NULL END, false, 0)"
                ),
                {
                    "id": run_id,
                    "s": scope,
                    "w": workflow,
                    "v": version,
                    "o": people[USER_EMAIL],
                    "key": f"key-{run_id}",
                    "st": status,
                    "code": "test_failure" if status == "failed" else None,
                    "fin": finished,
                },
            )

    run_db(seed)
    return workflow


def charts(client, scope):
    response = client.get(f"/api/control-room/scopes/{scope}/charts")
    assert response.status_code == 200, response.text
    return response.json()


def test_unknown_scope_is_404(client):
    login(client, ADMIN_EMAIL)
    response = client.get(f"/api/control-room/scopes/{uuid4()}/charts")
    assert response.status_code == 404


def test_non_member_is_403(client):
    scope, _ = seed_scope(client)
    login(client, USER2_EMAIL)  # seeded user, but not a member of this scope
    response = client.get(f"/api/control-room/scopes/{scope}/charts")
    assert response.status_code == 403


def test_finance_filter_member_without_role(client):
    """A scope member with workflow.access but no finance role sees no money."""
    scope, people = seed_scope(client)
    # Drop the verifier role: delete the platform membership for USER_EMAIL.
    login(client, ADMIN_EMAIL)

    async def revoke(conn):
        await conn.execute(
            text(
                "DELETE FROM platform_memberships WHERE user_id=:u AND scope_id IN "
                "(SELECT id FROM platform_scopes WHERE legacy_id=:g)"
            ),
            {"u": people[USER_EMAIL], "g": scope},
        )

    run_db(revoke)
    today, _ = seed_invoices(scope, people)
    seed_runs(scope, people)
    login(client, USER_EMAIL)
    data = charts(client, scope)
    assert data["spend"]["rows"] == []
    assert data["invoices_by_status"] == {"total": 0, "by_status": {}}
    # Runs + health are workflow-scope data, still visible.
    assert data["runs"]["total"] == 3
    assert data["runs"]["by_status"]["failed"] == 1


def test_charts_full_payload_for_finance_reader(client):
    scope, people = seed_scope(client)
    today, _ = seed_invoices(scope, people)
    seed_runs(scope, people)
    login(client, ADMIN_EMAIL)
    data = charts(client, scope)

    assert data["scope_id"] == scope
    assert data["generated_at"]

    spend = data["spend"]
    assert spend["months"][-1] == today.strftime("%Y-%m")
    this_month = [r for r in spend["rows"] if r["month"] == today.strftime("%Y-%m")]
    assert len(this_month) == 1
    assert this_month[0]["amount"] == "1000.00"
    assert this_month[0]["currency"] == "PKR"
    assert this_month[0]["count"] == 1
    last_month = [r for r in spend["rows"] if r["month"] == (today - timedelta(days=40)).strftime("%Y-%m")]
    assert len(last_month) == 1
    assert last_month[0]["amount"] == "500.00"
    # The 400-day-old paid invoice is outside the 12-month window.
    assert all(r["amount"] != "2000.00" for r in spend["rows"])

    status = data["invoices_by_status"]
    assert status["total"] == 5
    assert status["by_status"] == {"verified": 2, "pending": 1, "exception": 1, "paid": 1}

    runs = data["runs"]
    assert runs["total"] == 3
    assert runs["by_status"] == {"failed": 1, "completed": 1, "queued": 1}
    assert runs["failed_last_hour"] == 1
    assert len(runs["recent_failed"]) == 1
    failed = runs["recent_failed"][0]
    assert failed["workflow_name"] == "Control room workflow"
    assert failed["failure_code"] == "test_failure"
    assert failed["finished_at"] is not None
    assert runs["last_activity_at"] is not None

    health = data["health"]
    assert health["worker_enabled"] is False  # APP_ENV=testing: engine off unless enabled
    assert isinstance(health["ingestion_workers_online"], int)
    assert health["last_run_activity_at"] == runs["last_activity_at"]
    assert health["last_run_activity_age_seconds"] is not None
    assert health["last_run_activity_age_seconds"] >= 0

    # Alert preview: shape only — which rules fire depends on the time of
    # day (quiet hours 22:00-07:00 PKT suppress non-breaking rules).
    assert isinstance(data["active_alerts"], list)
    for alert in data["active_alerts"]:
        assert set(alert) == {"rule_key", "severity", "title", "detail", "evaluated_at"}
        assert alert["severity"] in ("critical", "warning")
    summary = data["alert_summary"]
    assert summary["total"] == len(data["active_alerts"])
    assert summary["critical"] + summary["warning"] == summary["total"]


def test_charts_empty_scope(client):
    scope, _ = seed_scope(client)
    login(client, ADMIN_EMAIL)
    data = charts(client, scope)
    assert data["spend"]["rows"] == []
    assert data["invoices_by_status"] == {"total": 0, "by_status": {}}
    assert data["runs"] == {
        "total": 0,
        "by_status": {},
        "failed_last_hour": 0,
        "recent_failed": [],
        "last_activity_at": None,
    }
    assert data["health"]["last_run_activity_at"] is None
    assert data["health"]["last_run_activity_age_seconds"] is None
