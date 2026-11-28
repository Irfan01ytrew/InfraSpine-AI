"""Step G slice 5: effective-permission handover check + time-limited grants — real migrated PostgreSQL."""

from sqlalchemy import select, text

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL, login, run_db
from tests.test_continuity import company, plan_input, post, scenario  # noqa: F401  (fixtures/helpers)


def _reconcile_group(c):
    """Mirror the fixture's raw access_group_members row into platform memberships."""
    def _run(conn):
        from app.platform.sync import reconcile_group_member
        return reconcile_group_member(conn, c["group"]["id"], c["ids"][USER_EMAIL])
    return run_db(lambda conn: conn.run_sync(_run))


def _revoke_group_member(c):
    def _run(conn):
        from app.platform.models import PlatformScope
        from app.platform.sync import sync_membership
        scope_id = conn.execute(select(PlatformScope.id).where(
            PlatformScope.legacy_table == "access_groups",
            PlatformScope.legacy_id == c["group"]["id"])).scalar_one()
        return sync_membership(conn, scope_id=scope_id, user_id=c["ids"][USER_EMAIL],
                               source="legacy:access_group_members", role=None)
    return run_db(lambda conn: conn.run_sync(_run))


def _grant_memberships(c):
    rows = run_db(lambda conn: conn.execute(text(
        "SELECT m.revoked_at, m.valid_to FROM platform_memberships m "
        "JOIN platform_roles r ON r.id = m.role_id "
        "JOIN platform_scopes s ON s.id = m.scope_id "
        "WHERE r.key = 'continuity.handover' AND s.legacy_table = 'access_groups' "
        "AND s.legacy_id = :g AND m.user_id = :u"),
        {"g": c["group"]["id"], "u": c["ids"][USER_EMAIL]})).all()
    return rows


def _check(client, c, scene):
    login(client, USER_EMAIL)
    response = client.get(c["path"] + f"/scenarios/{scene['id']}/handover-check")
    assert response.status_code == 200, response.text
    return response.json()


def _resource(check, resource_id):
    return next(r for r in check["resources"] if r["resource_id"] == resource_id)


def test_handover_check_eligible_replacement(client, company):
    c = company
    _reconcile_group(c)
    scene = scenario(client, c)
    check = _check(client, c, scene)
    assert check["eligible"] is True
    server = _resource(check, c["server"]["id"])
    assert server["scope_bound"] is True and server["permission_check"] == "checked"
    assert server["required_permissions"] == ["documents.read", "workflow.access"]
    candidate = next(x for x in server["candidates"] if x["node_id"] == c["backup"]["id"])
    assert candidate["eligible"] is True and candidate["gaps"] == []
    assert server["eligible_candidates"] == [c["backup"]["id"]]


def test_handover_check_missing_permission_without_group_membership(client, company):
    c = company
    scene = scenario(client, c)
    check = _check(client, c, scene)
    assert check["eligible"] is False
    server = _resource(check, c["server"]["id"])
    candidate = next(x for x in server["candidates"] if x["node_id"] == c["backup"]["id"])
    assert candidate["eligible"] is False
    assert candidate["missing_permissions"] == ["workflow.access"]
    assert any(gap["code"] == "missing_permission" for gap in candidate["gaps"])
    # Reconciling the membership, then revoking it, flips the same candidate back to ineligible.
    _reconcile_group(c)
    assert _resource(_check(client, c, scene), c["server"]["id"])["eligible_candidates"] == [c["backup"]["id"]]
    _revoke_group_member(c)
    candidate = next(x for x in _resource(_check(client, c, scene), c["server"]["id"])["candidates"]
                     if x["node_id"] == c["backup"]["id"])
    assert candidate["missing_permissions"] == ["workflow.access"]


def test_handover_check_scope_unbound_resource(client, company):
    c = company
    _reconcile_group(c)
    login(client, ADMIN_EMAIL)
    finance_process = post(client, c["path"] + "/nodes", {
        "external_key": "dept-finance-payroll", "name": "Finance Payroll", "kind": "process",
        "department_id": c["dep2"]["id"], "basis": "verified_fact",
        "evidence": [{"reference": "SOP-FIN-001", "note": "Synthetic acceptance evidence.", "observed_at": "2026-11-28T00:00:00Z"}],
        "required_skills": ["database"]})
    for source, target, kind in ((finance_process, c["server"], "depends_on"),
                                 (c["backup"], finance_process, "backup_for")):
        post(client, c["path"] + "/links", {"source_id": source["id"], "target_id": target["id"],
                                            "kind": kind, "basis": "verified_fact",
                                            "evidence": [{"reference": "SOP-FIN-001", "note": "Synthetic acceptance evidence.", "observed_at": "2026-11-28T00:00:00Z"}]})
    scene = scenario(client, c)
    check = _check(client, c, scene)
    finance = _resource(check, finance_process["id"])
    assert finance["scope_bound"] is False and finance["permission_check"] == "not_applicable"
    assert finance["permission_note"] is not None
    # Eligibility then rests on the backup relationship and skills alone.
    assert finance["eligible_candidates"] == [c["backup"]["id"]]


def _propose(client, c, scene=None, plan=None, status=201):
    login(client, USER_EMAIL)
    payload = {"resource_id": c["server"]["id"], "candidate_node_id": c["backup"]["id"],
               "valid_days": 7, "reason": "Handover for the database server during manager resignation."}
    if scene is not None:
        payload["scenario_id"] = scene["id"]
    if plan is not None:
        payload["plan_id"] = plan["id"]
    response = client.post(c["path"] + "/handover-grants", json=payload)
    assert response.status_code == status, response.text
    return response.json()


def _decide(client, c, grant, approved=True, digest=None, email=USER2_EMAIL, status=200):
    login(client, email)
    response = client.post(c["path"] + f"/handover-grants/{grant['id']}/decision", json={
        "approved": approved, "digest": digest if digest is not None else grant["digest"],
        "reason": "Independently reviewed the exact action and evidence."})
    assert response.status_code == status, response.text
    return response.json()


def test_handover_grant_flow_approve_then_revoke(client, company):
    c = company
    _reconcile_group(c)
    scene = scenario(client, c)
    grant = _propose(client, c, scene=scene)
    assert grant["status"] == "pending" and grant["digest"]
    assert grant["permissions"] == ["documents.read", "workflow.access"]
    decided = _decide(client, c, grant)
    assert decided["status"] == "active" and decided["valid_from"] and decided["valid_to"]
    memberships = _grant_memberships(c)
    assert len(memberships) == 1 and memberships[0][0] is None and memberships[0][1] is not None
    login(client, USER2_EMAIL)
    revoked = client.post(c["path"] + f"/handover-grants/{grant['id']}/revoke", json={
        "reason": "Handover complete; access no longer needed."})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    memberships = _grant_memberships(c)
    assert len(memberships) == 1 and memberships[0][0] is not None


def test_handover_grant_decision_guards_and_supersede(client, company):
    c = company
    _reconcile_group(c)
    scene = scenario(client, c)
    grant = _propose(client, c, scene=scene)
    # Requester (and beneficiary) cannot decide their own grant.
    assert _decide(client, c, grant, email=USER_EMAIL, status=403)["error"]["message"]
    # A wrong digest does not consume the decision.
    assert _decide(client, c, grant, digest="0" * 64, status=409)["error"]["message"]
    assert _decide(client, c, grant)["status"] == "active"
    # A second grant for the same pair supersedes the first membership.
    grant2 = _propose(client, c, scene=scene)
    assert _decide(client, c, grant2)["status"] == "active"
    memberships = _grant_memberships(c)
    assert len(memberships) == 2
    assert sum(1 for row in memberships if row[0] is None) == 1


def test_handover_grant_rejects_ineligible_candidate(client, company):
    c = company
    scene = scenario(client, c)
    assert _propose(client, c, scene=scene, status=409)["error"]["message"]
    # Exactly one of scenario_id / plan_id is required.
    login(client, USER_EMAIL)
    base = {"resource_id": c["server"]["id"], "candidate_node_id": c["backup"]["id"],
            "valid_days": 7, "reason": "Handover for the database server during manager resignation."}
    assert client.post(c["path"] + "/handover-grants", json=base).status_code == 422
    assert client.post(c["path"] + "/handover-grants", json={
        **base, "scenario_id": scene["id"], "plan_id": "00000000-0000-0000-0000-000000000000"}).status_code == 422


def test_handover_grant_from_plan(client, company):
    c = company
    _reconcile_group(c)
    login(client, USER_EMAIL)
    plan = post(client, c["path"] + "/plans", plan_input(c))
    grant = _propose(client, c, plan=plan)
    assert grant["status"] == "pending"
    assert _decide(client, c, grant)["status"] == "active"
    assert len(_grant_memberships(c)) == 1
