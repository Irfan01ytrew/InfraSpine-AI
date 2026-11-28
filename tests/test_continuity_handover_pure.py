"""Step G slice 5: effective-permission handover checks — pure logic, no database."""

import uuid

from app.continuity.handover import check_candidate, handover_check, required_permissions


def _node(node_id, kind, name="Node", data=None, user_id=None):
    return {"id": str(node_id), "kind": kind, "name": name, "data": data or {},
            "user_id": str(user_id) if user_id else None}


def _link(source_id, target_id, kind="backup_for", basis="verified_fact"):
    return {"source_id": str(source_id), "target_id": str(target_id), "kind": kind, "basis": basis}


def test_required_permissions_defaults_and_recorded_override():
    assert required_permissions({}, "process") == {"workflow.access", "documents.read"}
    assert required_permissions({}, "department") == {"group.access"}
    assert required_permissions({}, "knowledge") == {"documents.read"}
    assert required_permissions({}, "employee") == set()
    assert required_permissions({"required_permissions": ["documents.read"]}, "process") == {"documents.read"}
    # An override with only invalid entries falls back to the per-kind default.
    assert required_permissions({"required_permissions": ["not a permission", "UPPER"]}, "process") == {
        "workflow.access", "documents.read"}


def test_check_candidate_eligible_when_everything_holds():
    result = check_candidate(required={"workflow.access", "documents.read"},
                             held={"workflow.access", "documents.read", "group.access"},
                             skills=["database"], required_skills=["Database"],
                             backup_ok=True, available=True, has_user=True)
    assert result == {"eligible": True, "gaps": [], "missing_permissions": [],
                      "missing_skills": [], "backup_ok": True}


def test_check_candidate_reports_each_gap_code():
    result = check_candidate(required={"workflow.access", "documents.read"},
                             held={"documents.read"}, skills=[], required_skills=["database"],
                             backup_ok=False, available=False, has_user=False)
    assert result["eligible"] is False
    codes = {gap["code"] for gap in result["gaps"]}
    assert codes == {"missing_permission", "missing_skill", "missing_backup",
                     "candidate_unavailable", "no_user_binding"}
    assert result["missing_permissions"] == ["workflow.access"]
    assert result["missing_skills"] == ["database"]


def test_handover_check_marks_eligible_and_unbound_resources():
    server_id, process_id, backup_id, manager_id = (uuid.uuid4() for _ in range(4))
    user_id = uuid.uuid4()
    draft = {"steps": [
        {"resource_id": str(server_id), "owner_id": str(backup_id), "kind": "knowledge_transfer"},
        {"resource_id": str(server_id), "owner_id": str(backup_id), "kind": "responsibility_review"},
        {"resource_id": str(process_id), "owner_id": str(backup_id), "kind": "knowledge_transfer"},
    ], "unresolved_resource_ids": []}
    nodes = [
        _node(server_id, "system", "Server", {"required_skills": ["database"]}),
        _node(process_id, "process", "Payroll", {"required_skills": ["database"]}),
        _node(backup_id, "employee", "Backup", {"skills": ["database"], "available": True}, user_id),
        _node(manager_id, "employee", "Manager", {"skills": ["database"], "available": True}, uuid.uuid4()),
    ]
    links = [_link(backup_id, server_id), _link(backup_id, process_id)]
    scope_id = uuid.uuid4()
    result = handover_check(draft, nodes, links, {str(user_id): {"workflow.access", "documents.read"}},
                            {str(server_id): scope_id, str(process_id): None})
    assert result["eligible"] is True
    by_resource = {r["resource_id"]: r for r in result["resources"]}
    server = by_resource[str(server_id)]
    assert server["scope_bound"] is True and server["permission_check"] == "checked"
    assert server["required_permissions"] == ["documents.read", "workflow.access"]
    assert server["eligible_candidates"] == [str(backup_id)]
    assert server["candidates"][0]["gaps"] == []
    process = by_resource[str(process_id)]
    assert process["scope_bound"] is False and process["permission_check"] == "not_applicable"
    assert process["permission_note"] is not None
    assert process["eligible_candidates"] == [str(backup_id)]


def test_handover_check_ineligible_when_permission_missing():
    server_id, backup_id = uuid.uuid4(), uuid.uuid4()
    user_id = uuid.uuid4()
    draft = {"steps": [{"resource_id": str(server_id), "owner_id": str(backup_id), "kind": "knowledge_transfer"}],
             "unresolved_resource_ids": [str(uuid.uuid4())]}
    nodes = [
        _node(server_id, "system", "Server", {"required_skills": ["database"]}),
        _node(backup_id, "employee", "Backup", {"skills": ["database"], "available": True}, user_id),
    ]
    result = handover_check(draft, nodes, [_link(backup_id, server_id)],
                            {str(user_id): {"documents.read"}}, {str(server_id): uuid.uuid4()})
    assert result["eligible"] is False
    assert result["resources"][0]["candidates"][0]["missing_permissions"] == ["workflow.access"]
    assert result["unresolved_resource_ids"] == draft["unresolved_resource_ids"]
