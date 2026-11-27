from pathlib import Path

from sqlalchemy import text


def test_voice_question_is_temporary_acl_filtered_and_not_traced(client, fake_provider, monkeypatch):
    from tests.conftest import USER_EMAIL, USER2_EMAIL, login, run_db
    from tests.rag_helpers import activate_provider, make_pdf, process_all, upload

    activate_provider(client)
    login(client, USER_EMAIL)
    private = upload(
        client,
        "voice-private.pdf",
        make_pdf("Refund policy: customers may request a refund within 30 days of purchase."),
        visibility="private",
    ).json()
    process_all(client)
    fake_provider.rag_reply = lambda _message: "Refunds are available within 30 days [S1]."

    temporary_paths: list[Path] = []

    def local_transcription(path: Path) -> str:
        assert path.is_file()
        temporary_paths.append(path)
        return "What is the refund window?"

    monkeypatch.setattr("app.rag.voice_questions.transcribe_spoken_question", local_transcription)
    trace_count_before = run_db(lambda conn: conn.scalar(text("SELECT count(*) FROM rag_traces")))

    response = client.post(
        "/api/rag/voice-question",
        files={"audio": ("question.wav", b"synthetic audio only", "audio/wav")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["transcript"] == "What is the refund window?"
    assert body["answer"]["citations"][0]["document_id"] == private["id"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert temporary_paths and not temporary_paths[0].exists() and not temporary_paths[0].parent.exists()

    # The same endpoint respects the real retrieval ACL when another employee names the private source.
    provider_call_count = len(fake_provider.calls)
    login(client, USER2_EMAIL)
    response = client.post(
        "/api/rag/voice-question",
        data={"focus_document_id": private["id"]},
        files={"audio": ("question.wav", b"synthetic audio only", "audio/wav")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["answer"]["citations"] == []
    assert len(fake_provider.calls) == provider_call_count

    trace_count_after = run_db(lambda conn: conn.scalar(text("SELECT count(*) FROM rag_traces")))
    assert trace_count_after == trace_count_before
    assert run_db(lambda conn: conn.scalar(text("SELECT count(*) FROM conversations"))) == 0
    async def read_audit_details(conn):
        result = await conn.execute(
            text("SELECT details FROM audit_logs WHERE action = 'rag.voice_question_answered'")
        )
        return result.scalars().all()

    audit_details = run_db(read_audit_details)
    assert len(audit_details) == 2
    assert all("What is the refund window?" not in str(details) for details in audit_details)
