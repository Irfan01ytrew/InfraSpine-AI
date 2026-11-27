"""Adaptive practice uses only a learner's persisted scores and current cited course material."""

import json

import pytest
from sqlalchemy import text

from app.rag.training import select_adaptive_question_indexes
from tests.conftest import ADMIN_EMAIL, USER2_EMAIL, USER_EMAIL, login, run_db
from tests.rag_helpers import activate_provider, make_pdf, ready
from tests.test_access_control import set_access


def test_adaptive_question_selector_prioritizes_recent_misses_then_unseen_items():
    answer_key = [{"correct_index": 0} for _ in range(6)]
    attempts = [
        ([0, 1, 2, 3, 4], [0, 1, 1, 0, 0]),  # newest: questions 1 and 2 are missed
        ([0, 1, 2, 3, 4], [0, 0, 0, 0, 0]),
    ]

    assert select_adaptive_question_indexes(answer_key, attempts, limit=4) == [1, 2, 5, 0]
    assert select_adaptive_question_indexes(answer_key, [], limit=5) == [0, 1, 2, 3, 4]
    assert select_adaptive_question_indexes(answer_key, attempts, limit=99) == []


def _training_course(client, fake_provider):
    activate_provider(client)
    source = ready(
        client,
        "adaptive-practice-policy.pdf",
        make_pdf("Recovery tokens rotate every 90 days. The recovery owner is verified before a token is issued."),
        visibility="shared",
    )

    questions = [
        {
            "question": f"Which documented recovery policy fact is item {number}?",
            "choices": [
                "Rotate every 90 days",
                "Disable recovery permanently",
                "Skip owner verification",
                "Delete the token",
            ],
            "correct_index": 0,
            "explanation": "The source documents rotation every 90 days. [S1]",
            "sources": ["S1"],
        }
        for number in range(1, 4)
    ]

    def answer(message: str) -> str:
        if "Create a source-grounded multiple-choice quiz as JSON only." in message:
            return json.dumps({"questions": questions})
        if "\nANSWER:\n" in message and "Passage 1 (" in message:
            return "SUPPORTED"
        return "The recovery token policy requires rotation every 90 days [S1]."

    fake_provider.rag_reply = answer
    login(client, USER_EMAIL)
    response = client.post(
        "/api/rag/training",
        json={"topic": "Recovery token handling", "document_ids": [source["id"]]},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["quiz"]) == 3
    return source, response.json()


def test_adaptive_practice_is_persisted_audited_private_and_does_not_certify(client, fake_provider):
    source, plan = _training_course(client, fake_provider)
    course_id = plan["id"]

    # One failed full quiz makes the missed questions the first adaptive targets.
    failed = client.post(f"/api/rag/training/{course_id}/quiz-attempts", json={"answers": [0, 1, 0]})
    assert failed.status_code == 200, failed.text
    scenario_response = client.post(f"/api/rag/training/{course_id}/adaptive-practice", json={})
    assert scenario_response.status_code == 200, scenario_response.text
    scenario = scenario_response.json()
    assert [row["question_index"] for row in scenario["questions"]] == [1, 0]
    assert all(question["question"]["citations"] for question in scenario["questions"])
    assert "correct_index" not in scenario_response.text

    login(client, USER2_EMAIL)
    assert client.get(f"/api/rag/training/{course_id}/adaptive-practice").status_code == 404
    assert client.post(
        f"/api/rag/training/{course_id}/adaptive-practice/{scenario['scenario_id']}/attempts",
        json={"answers": [0, 0]},
    ).status_code == 404

    login(client, USER_EMAIL)
    result_response = client.post(
        f"/api/rag/training/{course_id}/adaptive-practice/{scenario['scenario_id']}/attempts",
        json={"answers": [0, 0]},
    )
    assert result_response.status_code == 200, result_response.text
    result = result_response.json()
    assert result["score"] == result["max_score"] == 2 and result["passed"] is True
    quiz_attempts = client.get(f"/api/rag/training/{course_id}/quiz-attempts").json()["attempts"]
    assert len(quiz_attempts) == 1
    assert quiz_attempts[0]["attempt_id"] == failed.json()["attempt_id"]
    assert quiz_attempts[0]["score"] == 2 and quiz_attempts[0]["max_score"] == 3
    history = client.get(f"/api/rag/training/{course_id}/adaptive-practice").json()["scenarios"]
    assert len(history) == 1
    assert history[0]["scenario_id"] == scenario["scenario_id"]
    assert history[0]["score"] == 2 and history[0]["max_score"] == 2
    duplicate = client.post(
        f"/api/rag/training/{course_id}/adaptive-practice/{scenario['scenario_id']}/attempts",
        json={"answers": [0, 0]},
    )
    assert duplicate.status_code == 409

    # Adaptive practice is formative, so a perfect practice round cannot satisfy course completion.
    completion = client.post(f"/api/rag/training/{course_id}/complete")
    assert completion.status_code == 409
    passed_quiz = client.post(f"/api/rag/training/{course_id}/quiz-attempts", json={"answers": [0, 0, 0]})
    assert passed_quiz.status_code == 200 and passed_quiz.json()["passed"] is True
    assert client.post(f"/api/rag/training/{course_id}/complete").status_code == 200
    pending = client.post(f"/api/rag/training/{course_id}/adaptive-practice", json={})
    assert pending.status_code == 200, pending.text

    login(client, ADMIN_EMAIL)
    assert set_access(client, source["id"], "private").status_code == 200
    login(client, USER_EMAIL)
    revoked_source_attempt = client.post(
        f"/api/rag/training/{course_id}/adaptive-practice/{pending.json()['scenario_id']}/attempts",
        json={"answers": [0, 0]},
    )
    assert revoked_source_attempt.status_code == 409
    stale_scenario = client.post(f"/api/rag/training/{course_id}/adaptive-practice", json={})
    assert stale_scenario.status_code == 409

    async def read_practice_audit(conn):
        result = await conn.execute(
            text(
                "SELECT action, details->>'question_indexes' AS question_indexes "
                "FROM audit_logs WHERE action LIKE 'training.adaptive_practice_%' ORDER BY id"
            )
        )
        return result.all()

    events = run_db(read_practice_audit)
    assert [row.action for row in events] == [
        "training.adaptive_practice_created",
        "training.adaptive_practice_submitted",
        "training.adaptive_practice_created",
    ]
    assert events[0].question_indexes == events[1].question_indexes == "[1, 0]"
    assert len(json.loads(events[2].question_indexes)) == 2

    with pytest.raises(Exception, match="training practice scenarios are immutable"):
        run_db(
            lambda conn: conn.execute(
                text("UPDATE training_practice_scenarios SET question_indexes = '[]'::jsonb WHERE id = :id"),
                {"id": scenario["scenario_id"]},
            )
        )
