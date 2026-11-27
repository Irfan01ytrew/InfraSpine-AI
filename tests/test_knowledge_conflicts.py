"""PostgreSQL-backed acceptance for candidate conflict detection and source-safe human triage."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.api.routes.governance import _conflict_cursor_decode, _conflict_cursor_encode
from app.db.models import KnowledgeConflict
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.test_knowledge import quote_from, ready_doc
from tests.test_access_control import new_group, set_access


SOURCE = (
    "Customer refund requests must be filed with billing after the invoice date. "
    "Reviewers should check the published policy and cite this exact source passage."
)


def _create(client, document_id: str, statement: str, *, scope_group_id: str | None = None) -> dict:
    response = client.post(
        "/api/knowledge",
        json={
            "statement": statement,
            "category": "policy",
            "visibility": "restricted" if scope_group_id else "shared",
            "scope_group_id": scope_group_id,
            "evidence": [{"document_id": document_id, "quote": quote_from(document_id)}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _publish(client, entry: dict, *, review_email: str = USER_EMAIL, publish_email: str = USER2_EMAIL) -> str:
    entry_id = entry["id"]
    revision_id = entry["revisions"][0]["id"]
    submitted = client.post(f"/api/knowledge/{entry_id}/submit")
    assert submitted.status_code == 200, submitted.text
    login(client, review_email)
    review = client.post(
        f"/api/governance/knowledge/{revision_id}/decisions",
        json={"stage": "review", "rationale": "Compared the cited evidence and checked the policy scope."},
    )
    assert review.status_code == 200, review.text
    login(client, publish_email)
    published = client.post(
        f"/api/governance/knowledge/{revision_id}/decisions",
        json={"stage": "publish", "rationale": "Approved the independently reviewed source-backed statement."},
    )
    assert published.status_code == 200, published.text
    return revision_id


def _grant_reviewers(client, scope_group_id: str | None = None) -> None:
    login(client, ADMIN_EMAIL)
    for email, permission in ((USER_EMAIL, "knowledge.review"), (USER2_EMAIL, "knowledge.publish")):
        body = {"email": email, "permission": permission, "category": "policy"}
        if scope_group_id:
            body["scope_group_id"] = scope_group_id
        result = client.post(
            "/api/admin/access/grants",
            json=body,
        )
        assert result.status_code == 201, result.text


def test_lexical_detector_flags_conflicting_facts_but_not_similar_wording():
    from app.rag.conflict_detection import _canonical_tokens, conflict_search_query, potential_conflict
    from app.rag.conflict_detection import _numbers

    assert potential_conflict(
        "Customer refund requests are permitted within 30 days after invoice date.",
        "Customer refund requests are prohibited after 14 days from invoice date.",
    )
    assert potential_conflict(
        "Employees may access the maintenance console during the approved window.",
        "Employees may not access the maintenance console during the approved window.",
    )
    assert potential_conflict(
        "Employees can access the maintenance console during the approved window.",
        "Employees can't access the maintenance console during the approved window.",
    )
    assert potential_conflict(
        "Employees must file expense reports within thirty days of purchase.",
        "Employees must file expense reports within ninety days of purchase.",
    )
    assert potential_conflict(
        "Employees may submit a report without manager approval.",
        "Employees may submit a report with manager approval.",
    )
    assert potential_conflict(
        "Employees must use company laptops during work hours inside the office.",
        "Staff may use corporate computers during work hours away from the office.",
    )
    low_overlap_left = "Employees must keep company laptops at headquarters."
    low_overlap_right = "Staff may take corporate computers home."
    left_tokens = _canonical_tokens(low_overlap_left)
    right_tokens = _canonical_tokens(low_overlap_right)
    assert len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens)) == 0.5
    assert potential_conflict(low_overlap_left, low_overlap_right)
    backup_policy = "Employees must keep backup copies on premises."
    cloud_policy = "Staff may store backups in cloud storage."
    assert potential_conflict(backup_policy, cloud_policy)
    retention_wording = "Staff may keep customer backups for seven years."
    disposal_wording = "Employees should discard customer backup copies after twelve months."
    assert potential_conflict(retention_wording, disposal_wording)
    assert not potential_conflict(
        retention_wording,
        "Employees may retain customer backup copies for seven years.",
    )
    disposal_query = conflict_search_query(disposal_wording)
    assert "discard:*" in disposal_query and "delete:*" in disposal_query and "expunge:*" in disposal_query
    assert not potential_conflict(
        backup_policy,
        "Staff must retain backup copies on premises.",
    )
    assert not potential_conflict(
        backup_policy,
        "Staff must process employee expense reports through payroll.",
    )
    assert not potential_conflict(
        "Employees may use company laptops during work hours.",
        "Staff may use corporate computers during work hours.",
    )
    assert not potential_conflict(
        "Employees may keep company laptops at headquarters.",
        "Staff may take corporate computers home.",
    )
    assert not potential_conflict(
        "Employees must file expense reports within twenty-four hours of purchase.",
        "Employees must file expense reports within one day of purchase.",
    )
    assert not potential_conflict(
        "Customer refund requests are permitted within 30 calendar days after invoice date.",
        "Customer refund requests should be filed within 30 calendar days after invoice date.",
    )
    assert _numbers("Customer discounts are capped at 5 percent annually.") == _numbers(
        "Customer discounts are capped at 5.0% annually."
    )
    assert not potential_conflict(
        "Customer discounts are capped at 5 percent annually.",
        "Customer discounts are capped at 5.0% annually.",
        semantic_similarity=0.99,
    )
    assert potential_conflict(
        "Customer discounts are capped at 5 percent annually.",
        "Customer discounts are capped at 7% annually.",
        semantic_similarity=0.99,
    )
    assert not potential_conflict("The invoice close date is 14 days away.", "Office access is restricted to employees.")
    low_overlap_retention = "Managers must retain incident reports for seven years."
    low_overlap_deletion = "Incident documentation may be deleted after one year."
    assert not potential_conflict(low_overlap_retention, low_overlap_deletion)
    assert potential_conflict(low_overlap_retention, low_overlap_deletion, semantic_similarity=0.60)
    assert not potential_conflict(low_overlap_retention, low_overlap_deletion, semantic_similarity=0.519)
    assert not potential_conflict(
        "Managers must retain incident reports for seven years.",
        "Incident documentation must be retained for seven years.",
        semantic_similarity=0.80,
    )
    assert not potential_conflict(
        low_overlap_retention,
        "The cafeteria is open to visitors.",
        semantic_similarity=0.95,
    )
    query = conflict_search_query("Refund timeline: 30 days; approved by billing.")
    assert "refund:*" in query and "30:*" in query and ";" not in query
    alias_query = conflict_search_query("Employees must use company laptops at work.")
    assert "staff:*" in alias_query and "corporate:*" in alias_query and "computers:*" in alias_query
    backup_query = conflict_search_query(backup_policy)
    assert "staff:*" in backup_query and "store:*" in backup_query and "backups:*" in backup_query
    now = datetime.now(UTC)
    conflict_id = uuid.uuid4()
    cursor_row = KnowledgeConflict(
        id=conflict_id, revision_id=uuid.uuid4(), compared_revision_id=uuid.uuid4(), detected_at=now,
    )
    assert _conflict_cursor_decode(_conflict_cursor_encode(cursor_row)) == (now, conflict_id)
    with pytest.raises(Exception, match="invalid"):
        _conflict_cursor_decode("not-a-cursor")


def test_submission_creates_scoped_review_candidate_and_records_immutable_decision(client):
    _grant_reviewers(client)
    login(client, ADMIN_EMAIL)
    document_id = ready_doc(client, "conflict-policy.pdf", SOURCE, visibility="shared")
    _publish(
        client,
        _create(client, document_id, "Customer refund requests are permitted within 30 days after the invoice date."),
    )

    login(client, ADMIN_EMAIL)
    candidate = _create(
        client, document_id, "Customer refund requests are prohibited after 14 days from the invoice date."
    )
    revision_id = candidate["revisions"][0]["id"]
    submitted = client.post(f"/api/knowledge/{candidate['id']}/submit")
    assert submitted.status_code == 200, submitted.text

    # The contributor is not a reviewer for their own pair. The publisher-only user has no review scope.
    assert client.get("/api/governance/knowledge/conflicts").json()["items"] == []
    conflict_id = run_db(lambda conn: conn.scalar(text("SELECT id FROM knowledge_conflicts")))
    login(client, USER2_EMAIL)
    assert client.get("/api/governance/knowledge/conflicts").json()["items"] == []
    publisher_denied = client.post(
        f"/api/governance/knowledge/conflicts/{conflict_id}/decisions",
        json={"decision": "dismissed", "rationale": "Publisher permission alone is not review authority."},
    )
    assert publisher_denied.status_code == 403

    login(client, USER_EMAIL)
    queue = client.get("/api/governance/knowledge/conflicts")
    assert queue.status_code == 200, queue.text
    assert len(queue.json()["items"]) == 1
    item = queue.json()["items"][0]
    assert item["category"] == "policy" and item["status"] == "open"
    assert item["submitted"]["revision_id"] == revision_id
    assert "14 days" in item["submitted"]["statement"] and "30 days" in item["existing"]["statement"]
    assert item["submitted"]["sources"][0]["filename"] == "conflict-policy.pdf"
    assert item["review"] is None and queue.json()["next_cursor"] is None
    assert client.get("/api/governance/knowledge/conflicts?cursor=invalid").status_code == 422

    reviewed = client.post(
        f"/api/governance/knowledge/conflicts/{item['id']}/decisions",
        json={"decision": "confirmed", "rationale": "The compared source-backed time limits are inconsistent."},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "confirmed"
    history = client.get("/api/governance/knowledge/conflicts").json()["items"]
    assert len(history) == 1
    assert history[0]["review"]["decision"] == "confirmed"
    assert history[0]["review"]["reviewer_email"] == USER_EMAIL

    conflict_id = item["id"]
    assert run_db(
        lambda conn: conn.scalar(
            text("SELECT count(*) FROM knowledge_conflict_reviews WHERE conflict_id = :id"), {"id": conflict_id}
        )
    ) == 1
    with pytest.raises(Exception, match="append-only"):
        run_db(
            lambda conn: conn.execute(
                text("UPDATE knowledge_conflict_reviews SET rationale = 'edited' WHERE conflict_id = :id"),
                {"id": conflict_id},
            )
        )


def test_percent_symbol_and_word_forms_do_not_create_conflict_candidate(client):
    _grant_reviewers(client)
    login(client, ADMIN_EMAIL)
    source = "Customer discounts are capped at 5 percent annually. Reviewers must cite this source when checking the policy."
    document_id = ready_doc(client, "percent-policy.pdf", source, visibility="shared")
    _publish(client, _create(client, document_id, "Customer discounts are capped at 5 percent annually."))

    login(client, ADMIN_EMAIL)
    candidate = _create(client, document_id, "Customer discounts are capped at 5.0% annually.")
    revision_id = uuid.UUID(candidate["revisions"][0]["id"])
    submitted = client.post(f"/api/knowledge/{candidate['id']}/submit")
    assert submitted.status_code == 200, submitted.text
    assert run_db(
        lambda conn: conn.scalar(
            text("SELECT count(*) FROM knowledge_conflicts WHERE revision_id = :revision_id"),
            {"revision_id": revision_id},
        )
    ) == 0


def test_alias_prefilter_surfaces_low_overlap_conflict_candidate(client):
    _grant_reviewers(client)
    login(client, ADMIN_EMAIL)
    document_id = ready_doc(client, "alias-conflict-policy.pdf", SOURCE, visibility="shared")
    baseline = _create(client, document_id, "Staff may take corporate computers home.")
    _publish(client, baseline)

    login(client, ADMIN_EMAIL)
    submitted = _create(client, document_id, "Employees must keep company laptops at headquarters.")
    response = client.post(f"/api/knowledge/{submitted['id']}/submit")
    assert response.status_code == 200, response.text

    login(client, USER_EMAIL)
    queue = client.get("/api/governance/knowledge/conflicts")
    assert queue.status_code == 200, queue.text
    assert len(queue.json()["items"]) == 1
    item = queue.json()["items"][0]
    assert item["submitted"]["statement"] == "Employees must keep company laptops at headquarters."
    assert item["existing"]["statement"] == "Staff may take corporate computers home."


def test_alias_prefilter_surfaces_backup_location_conflict_candidate(client):
    _grant_reviewers(client)
    login(client, ADMIN_EMAIL)
    baseline_source = "Staff may store backups in cloud storage."
    baseline_document = ready_doc(client, "cloud-backup-policy.pdf", baseline_source, visibility="shared")
    baseline = _create(client, baseline_document, baseline_source)
    _publish(client, baseline)

    login(client, ADMIN_EMAIL)
    candidate_source = "Employees must keep backup copies on premises."
    candidate_document = ready_doc(client, "onsite-backup-policy.pdf", candidate_source, visibility="shared")
    candidate = _create(client, candidate_document, candidate_source)
    submitted = client.post(f"/api/knowledge/{candidate['id']}/submit")
    assert submitted.status_code == 200, submitted.text

    login(client, USER_EMAIL)
    queue = client.get("/api/governance/knowledge/conflicts")
    assert queue.status_code == 200, queue.text
    assert len(queue.json()["items"]) == 1
    item = queue.json()["items"][0]
    assert item["submitted"]["statement"] == candidate_source
    assert item["existing"]["statement"] == baseline_source


def test_local_embedding_surfaces_low_overlap_candidate_without_cross_topic_noise(client):
    _grant_reviewers(client)

    class ControlledLocalEmbedder:
        model_name = "test/controlled-local-embedder"
        dimension = 2
        available_locally = True

        @staticmethod
        def embed(texts):
            import numpy as np

            return np.asarray(
                [[0.0, 1.0] if "cafeteria" in text.casefold() else [1.0, 0.0] for text in texts],
                dtype=np.float32,
            )

        @staticmethod
        def embed_local(texts):
            return ControlledLocalEmbedder.embed(texts)

    login(client, ADMIN_EMAIL)
    client.app.state.rag.embedder = ControlledLocalEmbedder()
    baseline_text = "Managers must retain incident reports for seven years."
    baseline_document = ready_doc(client, "incident-retention-policy.pdf", baseline_text, visibility="shared")
    baseline = _create(client, baseline_document, baseline_text)
    _publish(client, baseline)

    login(client, ADMIN_EMAIL)
    unrelated_text = "The cafeteria closes at eight PM."
    unrelated_document = ready_doc(client, "cafeteria-hours-policy.pdf", unrelated_text, visibility="shared")
    unrelated = _create(client, unrelated_document, unrelated_text)
    _publish(client, unrelated)

    login(client, ADMIN_EMAIL)
    candidate_text = "Incident documentation may be deleted after one year."
    candidate_document = ready_doc(client, "incident-deletion-policy.pdf", candidate_text, visibility="shared")
    candidate = _create(client, candidate_document, candidate_text)
    submitted = client.post(f"/api/knowledge/{candidate['id']}/submit")
    assert submitted.status_code == 200, submitted.text

    login(client, USER_EMAIL)
    queue = client.get("/api/governance/knowledge/conflicts")
    assert queue.status_code == 200, queue.text
    assert len(queue.json()["items"]) == 1
    item = queue.json()["items"][0]
    assert item["submitted"]["statement"] == candidate_text
    assert item["existing"]["statement"] == baseline_text


def test_restricted_conflict_detection_and_review_stay_with_the_department(client):
    login(client, ADMIN_EMAIL)
    finance = new_group(client, "Conflict Finance Department", kind="department")
    legal = new_group(client, "Conflict Legal Department", kind="department")
    for group_id in (finance, legal):
        for email in (USER_EMAIL, USER2_EMAIL):
            assert client.post(
                f"/api/admin/access/groups/{group_id}/members", json={"email": email}
            ).status_code == 204
        _grant_reviewers(client, group_id)

    document_id = ready_doc(client, "department-conflict-policy.pdf", SOURCE)
    assert set_access(client, document_id, "restricted", groups=[finance, legal]).status_code == 200
    existing_statement = "Customer refund requests are permitted within 30 days after the invoice date."
    for group_id in (finance, legal):
        login(client, ADMIN_EMAIL)
        existing = _create(client, document_id, existing_statement, scope_group_id=group_id)
        _publish(client, existing)

    login(client, ADMIN_EMAIL)
    candidate = _create(
        client,
        document_id,
        "Customer refund requests are prohibited after 14 days from the invoice date.",
        scope_group_id=finance,
    )
    submitted = client.post(f"/api/knowledge/{candidate['id']}/submit")
    assert submitted.status_code == 200, submitted.text
    conflict_count = run_db(lambda conn: conn.scalar(text("SELECT count(*) FROM knowledge_conflicts")))
    assert conflict_count == 1, "the matching statement in Legal must not enter Finance's conflict queue"
    conflict_id = run_db(lambda conn: conn.scalar(text("SELECT id FROM knowledge_conflicts")))

    login(client, USER2_EMAIL)
    assert client.get("/api/governance/knowledge/conflicts").json()["items"] == []
    assert client.post(
        f"/api/governance/knowledge/conflicts/{conflict_id}/decisions",
        json={"decision": "dismissed", "rationale": "Publish permission does not grant conflict review."},
    ).status_code == 403

    login(client, USER_EMAIL)
    queue = client.get("/api/governance/knowledge/conflicts")
    assert queue.status_code == 200, queue.text
    assert len(queue.json()["items"]) == 1
    item = queue.json()["items"][0]
    assert item["submitted"]["scope_group_id"] == finance
    assert item["existing"]["scope_group_id"] == finance
    assert client.post(
        f"/api/governance/knowledge/conflicts/{item['id']}/decisions",
        json={"decision": "confirmed", "rationale": "The scoped source passages state inconsistent time limits."},
    ).status_code == 200
    with pytest.raises(Exception, match="knowledge conflict status requires a recorded review event"):
        run_db(
            lambda conn: conn.execute(
                text("UPDATE knowledge_conflicts SET status = 'dismissed' WHERE id = :id"), {"id": conflict_id}
            )
        )


def test_conflict_review_queue_paginates_and_reviews_more_than_fifty_candidates(client):
    _grant_reviewers(client)
    login(client, ADMIN_EMAIL)
    document_id = ready_doc(client, "paged-conflict-policy.pdf", SOURCE, visibility="shared")
    baseline = _create(
        client,
        document_id,
        "Customer refund requests are permitted within 30 days after the invoice date.",
    )
    _publish(client, baseline)

    login(client, ADMIN_EMAIL)
    for batch in range(1, 52):
        candidate = _create(
            client,
            document_id,
            f"Customer refund requests are prohibited after 14 days from the invoice date; batch {batch}.",
        )
        submitted = client.post(f"/api/knowledge/{candidate['id']}/submit")
        assert submitted.status_code == 200, submitted.text

    login(client, USER_EMAIL)
    first_page = client.get("/api/governance/knowledge/conflicts")
    assert first_page.status_code == 200, first_page.text
    first = first_page.json()
    assert len(first["items"]) == 50
    assert first["next_cursor"]

    second_page = client.get(
        "/api/governance/knowledge/conflicts",
        params={"cursor": first["next_cursor"]},
    )
    assert second_page.status_code == 200, second_page.text
    second = second_page.json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    assert len(first_ids) == 50 and len(second_ids) == 1 and first_ids.isdisjoint(second_ids)

    for item in (first["items"][0], second["items"][0]):
        decision = client.post(
            f"/api/governance/knowledge/conflicts/{item['id']}/decisions",
            json={"decision": "confirmed", "rationale": "Both cited statements describe inconsistent refund windows."},
        )
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == "confirmed"

    reviewed_count = run_db(
        lambda conn: conn.scalar(text("SELECT count(*) FROM knowledge_conflict_reviews"))
    )
    assert reviewed_count == 2


def test_conflict_queue_checks_both_source_acls_before_returning_statements(client):
    _grant_reviewers(client)
    login(client, ADMIN_EMAIL)
    document_id = ready_doc(client, "restricted-conflict-policy.pdf", SOURCE, visibility="shared")
    _publish(
        client,
        _create(client, document_id, "Customer refund requests are permitted within 30 days after the invoice date."),
    )
    login(client, ADMIN_EMAIL)
    candidate = _create(
        client, document_id, "Customer refund requests are prohibited after 14 days from the invoice date."
    )
    assert client.post(f"/api/knowledge/{candidate['id']}/submit").status_code == 200
    conflict_id = run_db(lambda conn: conn.scalar(text("SELECT id FROM knowledge_conflicts")))
    group_id = new_group(client, "Conflict Source Readers")
    assert set_access(client, document_id, "restricted", groups=[group_id]).status_code == 200

    login(client, USER_EMAIL)
    assert client.get("/api/governance/knowledge/conflicts").json()["items"] == []
    denied = client.post(
        f"/api/governance/knowledge/conflicts/{conflict_id}/decisions",
        json={"decision": "dismissed", "rationale": "The sources are no longer readable to this reviewer."},
    )
    assert denied.status_code == 404

    login(client, ADMIN_EMAIL)
    member = client.post(f"/api/admin/access/groups/{group_id}/members", json={"email": USER_EMAIL})
    assert member.status_code == 204
    login(client, USER_EMAIL)
    visible = client.get("/api/governance/knowledge/conflicts").json()["items"]
    assert len(visible) == 1 and visible[0]["id"] == str(conflict_id)
    assert visible[0]["submitted"]["sources"][0]["filename"] == "restricted-conflict-policy.pdf"


def test_reviewer_can_dismiss_a_lexical_candidate_with_persisted_rationale(client):
    _grant_reviewers(client)
    login(client, ADMIN_EMAIL)
    document_id = ready_doc(client, "dismissed-conflict-policy.pdf", SOURCE, visibility="shared")
    _publish(
        client,
        _create(client, document_id, "Customer refund requests are permitted within 30 days after the invoice date."),
    )
    login(client, ADMIN_EMAIL)
    candidate = _create(
        client, document_id, "Customer refund requests are prohibited after 14 days from the invoice date."
    )
    assert client.post(f"/api/knowledge/{candidate['id']}/submit").status_code == 200
    login(client, USER_EMAIL)
    item = client.get("/api/governance/knowledge/conflicts").json()["items"][0]
    response = client.post(
        f"/api/governance/knowledge/conflicts/{item['id']}/decisions",
        json={"decision": "dismissed", "rationale": "The cited context shows these rules apply to separate customer plans."},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "dismissed"
    assert client.get("/api/governance/knowledge/conflicts").json()["items"][0]["review"]["decision"] == "dismissed"
