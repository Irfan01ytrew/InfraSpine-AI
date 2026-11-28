"""Step G slice 7: disruption drills + drill/simulation comparison — real migrated PostgreSQL."""

import uuid
from datetime import UTC, datetime, timedelta

from tests.conftest import USER_EMAIL, USER2_EMAIL, login
from tests.test_continuity import activation, approve, company, ev, now, plan_input, post, scenario  # noqa: F401


def test_drills_seed_creates_four_and_skips_supplier(client, company):
    c = company
    login(client, USER_EMAIL)
    first = post(client, c["path"] + "/drills/seed", status=200)
    assert {row["kind"] for row in first["created"]} == {
        "resignation", "absence", "server_outage", "database_failure"}
    assert first["skipped"] == [{"kind": "supplier_disruption",
                                 "reason": "No suitable node in the continuity graph."}]
    assert all(row["source_key"] == f"drill:{row['kind']}" for row in first["created"])
    second = post(client, c["path"] + "/drills/seed", status=200)
    assert second["created"] == [] and len(second["existing"]) == 4


def test_drill_comparison_measures_drill_against_simulation(client, company):
    c = company
    login(client, USER_EMAIL)
    scene = scenario(client, c)
    plan = post(client, c["path"] + "/plans", plan_input(c))
    approve(client, c["path"] + f"/plans/{plan['id']}/decision", plan)
    run = activation(client, c, plan, scene)
    approve(client, c["path"] + f"/runs/{run['id']}/decision", run)
    report = client.get(c["path"] + f"/runs/{run['id']}").json()
    task = report["tasks"][0]
    login(client, USER_EMAIL)
    dispatched = post(client, c["path"] + f"/tasks/{task['id']}/dispatch", status=200)
    login(client, USER2_EMAIL)
    post(client, "/api/automation/runs/" + dispatched["workflow_run_id"] + "/decision",
         {"approved": True, "rationale": "Independent workflow execution authorization."}, status=200)
    from app.main import app
    from app.services.automation_worker import process_next_workflow_run

    assert client.portal.call(process_next_workflow_run, app.state.sessionmaker)
    login(client, USER_EMAIL)
    external = next(t for t in client.get("/api/automation/tasks").json()["tasks"]
                    if t["run_id"] == dispatched["workflow_run_id"])
    post(client, "/api/automation/tasks/" + external["id"] + "/complete", status=200)
    proof = {"evidence": ev(), "outcome": "Operating knowledge reviewed and restored database validated.",
             "recovered_at": now(),
             "restored_point_at": (datetime.now(UTC) - timedelta(minutes=35)).isoformat(),
             "service_percent": 95}
    post(client, c["path"] + f"/tasks/{task['id']}/evidence", proof, status=200)
    login(client, USER2_EMAIL)
    post(client, c["path"] + f"/tasks/{task['id']}/verify",
         {"reason": "Verified procedure and observed recovery evidence."}, status=200)
    post(client, c["path"] + f"/runs/{run['id']}/close",
         {"summary": "The controlled handover completed with independently reviewed evidence.",
          "corrective_actions": ["Schedule the next quarterly documentation review."], "evidence": ev()},
         status=200)
    comparison = client.get(c["path"] + f"/runs/{run['id']}/drill-comparison").json()
    assert comparison["matches"] is False
    codes = {d["code"] for d in comparison["differences"]}
    # The drill recovered the server in ~30 minutes against a 60-minute RTO,
    # but the 120-minute scenario forecast said the RTO would be exceeded.
    assert "drill_met_rto_simulation_missed" in codes
    # The handover plan covers only the server; the other affected resources were not recovered.
    assert "affected_not_recovered" in codes
    assert comparison["drill"]["recovered_ids"] == [c["server"]["id"]]
    assert comparison["simulation"]["affected_ids"]


def test_drill_comparison_rejects_non_drill_runs(client, company):
    c = company
    login(client, USER_EMAIL)
    # An incident-mode run needs an incident escalation, not a simulation:
    # only a scenario with source_module "07" carries a source_key, which
    # activate() requires for mode "incident".
    scene = post(client, c["path"] + "/scenarios", {
        "name": "Module 07 server incident", "kind": "server_outage",
        "disrupted_ids": [c["server"]["id"]], "duration_minutes": 240,
        "source_module": "07", "source_event_id": str(uuid.uuid4()),
        "evidence": ev("INC-QA-001")})
    plan = post(client, c["path"] + "/plans", plan_input(c))
    approve(client, c["path"] + f"/plans/{plan['id']}/decision", plan)
    # approve() leaves the session logged in as the independent approver
    # (USER2); run creation needs the owner, so log back in first — the
    # same login activation() does before creating a run.
    login(client, USER_EMAIL)
    run = post(client, c["path"] + "/runs", {
        "plan_id": plan["id"], "scenario_id": scene["id"], "mode": "incident",
        "idempotency_key": str(uuid.uuid4()), "disruption_at": now()}, status=202)
    response = client.get(c["path"] + f"/runs/{run['id']}/drill-comparison")
    assert response.status_code == 409, response.text
