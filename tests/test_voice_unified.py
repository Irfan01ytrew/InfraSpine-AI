"""Slices 2-3 tests for Step O (voice-first founder mode).

A voice proposal persists, mirrors as a unified workflow run
(``origin='voice'``), waits for an independent platform approval, and executes
through ``execute_once`` with read-back verification of the proposal row.
Runs on a throwaway ``*_test`` database only (conftest migrates it to head,
which includes 0104).
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.db.models import User
from app.db.voice_models import VoiceProposal
from app.main import app
from app.platform.models import PlatformActionExecution, PlatformApprovalRequest
from app.platform.sync import role_permissions
from app.voice.service import submit_voice_note
from app.voice.unified import (
    mirror_pending_voice_proposals,
    mirror_voice_proposal,
    voice_spec,
)
from app.workflows.definition import Definition
from tests.conftest import (
    ADMIN_EMAIL,
    TEST_DATABASE_URL,
    USER2_EMAIL,
    USER_EMAIL,
    login,
    run_db,
)
from tests.test_finance_invoice_flow import pump
from tests.test_voice_first_proposals import (
    _Audio,
    _inline_thread,
    _temporary_directory,
)
from tests.test_workflow_engine import setup_scope

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MANAGER_EMAIL = USER2_EMAIL


def _submit_via_service(client, scope, transcript, email=ADMIN_EMAIL):
    """Submit a voice note through the service (fake transcriber, no Whisper)."""
    async def submit():
        async with app.state.sessionmaker() as db:
            user = await db.scalar(select(User).where(User.email == email))
            audio = _Audio("note.wav", [b"synthetic audio only"], size=20)
            created: list[Path] = []
            return await submit_voice_note(
                db, app.state.cipher, user, uuid.UUID(scope), audio,
                transcriber=lambda path: transcript,
                temporary_directory=_temporary_directory(created),
                run_in_thread=_inline_thread,
            )
    return client.portal.call(submit)


def _make_manager(client, scope, email=_MANAGER_EMAIL):
    """Promote a seeded user to department manager and reconcile their
    platform membership, so `voice.manage` is actually granted."""
    from app.platform import sync

    async def seed(conn):
        user_id = (await conn.execute(
            text("SELECT id FROM users WHERE email = :email"), {"email": email})).scalar_one()
        await conn.execute(
            text("UPDATE users SET role = 'manager' WHERE email = :email"), {"email": email})
        await conn.execute(
            text("INSERT INTO department_managers(group_id, manager_user_id) VALUES (:g, :u)"
                 " ON CONFLICT DO NOTHING"),
            {"g": scope, "u": user_id})
        await conn.run_sync(
            lambda c: sync.reconcile_group_member(c, uuid.UUID(scope), user_id))
        return str(user_id)
    return run_db(seed)


def _proposal(proposal_id):
    async def state(conn):
        row = (await conn.execute(text(
            "SELECT status, execution_result, failure_reason, approval_id, wf_run_id"
            " FROM voice_proposals WHERE id = :id"), {"id": str(proposal_id)})).mappings().first()
        return dict(row) if row else None
    return run_db(state)


def _approval(approval_id):
    async def state(conn):
        row = (await conn.execute(text(
            "SELECT status, action_hash, decided_by, requested_by FROM platform_approval_requests"
            " WHERE id = :id"), {"id": str(approval_id)})).mappings().first()
        return dict(row) if row else None
    return run_db(state)


def _run_status(proposal_id):
    async def state(conn):
        result = await conn.execute(text(
            "SELECT r.id, r.status FROM wf_runs r JOIN wf_definitions d ON d.id = r.workflow_id"
            " WHERE r.origin = 'voice' AND r.origin_ref = :id"), {"id": str(proposal_id)})
        rows = result.mappings().all()
        return [dict(row) for row in rows]
    return run_db(state)


def _executions(proposal_id):
    async def state(conn):
        result = await conn.execute(text(
            "SELECT idempotency_key, status FROM platform_action_executions"
            " WHERE idempotency_key LIKE :key"), {"key": f"voice:{proposal_id}%"})
        rows = result.mappings().all()
        return [dict(row) for row in rows]
    return run_db(state)


def _decide(client, approval_id, decision, presented_hash, email=_MANAGER_EMAIL):
    login(client, email)
    response = client.post(f"/api/approvals/{approval_id}/decide",
                           json={"decision": decision, "presented_hash": presented_hash})
    return response


def _pump_until_run(client, proposal_id, want="completed", limit=10):
    for _ in range(limit):
        rows = _run_status(proposal_id)
        if rows and rows[0]["status"] == want:
            return True
        pump(client, 2)
    rows = _run_status(proposal_id)
    return bool(rows) and rows[0]["status"] == want


def _seed_invoice(client, scope, *, number="INV-42", status="verified", amount="100.00"):
    async def seed(conn):
        admin_id = (await conn.execute(
            text("SELECT id FROM users WHERE email = :email"), {"email": ADMIN_EMAIL})).scalar_one()
        batch_id, invoice_id = uuid.uuid4(), uuid.uuid4()
        await conn.execute(text(
            "INSERT INTO fin_invoice_batches(id, group_id, name, created_by, status, currency,"
            " tolerance_pct, tolerance_abs, round) VALUES (:id, :g, 'voice batch', :u, 'verifying',"
            " 'PKR', 2.00, 0, 0)"), {"id": batch_id, "g": scope, "u": admin_id})
        await conn.execute(text(
            "INSERT INTO fin_invoices(id, group_id, batch_id, seq, invoice_number, vendor_name,"
            " amount, currency, source_kind, source_digest, status)"
            " VALUES (:id, :g, :b, 1, :n, 'Acme', :a, 'PKR', 'json', :d, :s)"),
            {"id": invoice_id, "g": scope, "b": batch_id, "n": number, "a": amount,
             "d": "x" * 64, "s": status})
        return str(invoice_id)
    return run_db(seed)


def _invoice_status(invoice_id):
    async def state(conn):
        return (await conn.execute(text(
            "SELECT status FROM fin_invoices WHERE id = :id"), {"id": invoice_id})).scalar_one()
    return run_db(state)


# --- spec / migration / permissions ----------------------------------------

def test_voice_spec_validates():
    spec = Definition.model_validate(voice_spec())
    nodes = {node.id: node for node in spec.nodes}
    assert nodes["approval"].config.handler == "voice.proposal_approval"
    assert nodes["approval"].config.auto is True
    assert nodes["execute"].config.handler == "voice.proposal_execute"
    assert nodes["execute"].config.permission == "voice.manage"


def test_origin_check_widened_by_0104():
    async def check(conn):
        row = (await conn.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = 'ck_wf_definitions_origin_valid'"))).scalar_one()
        return row
    assert "'voice'" in run_db(check)


def test_voice_manage_granted_to_managers_and_admins_only():
    assert "voice.manage" in role_permissions("group.manager")
    assert "voice.manage" in role_permissions("org.admin")
    assert "voice.manage" not in role_permissions("group.member")


def test_alembic_check_clean():
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    command.check(cfg)


# --- submit: persistence, approval, mirror ----------------------------------

def test_submit_persists_proposal_approval_and_run(client):
    scope, _people = setup_scope(client)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    assert submission.proposal is not None
    assert submission.approval is not None
    assert submission.run_id is not None
    row = _proposal(submission.proposal.id)
    assert row["status"] == "proposed"
    assert str(row["approval_id"]) == str(submission.approval.id)
    assert str(row["wf_run_id"]) == str(submission.run_id)
    approval = _approval(submission.approval.id)
    assert approval["status"] == "pending"
    assert approval["action_hash"] == submission.proposal.action_hash
    runs = _run_status(submission.proposal.id)
    assert len(runs) == 1 and runs[0]["status"] in {"queued", "running"}


def test_submit_unknown_transcript_persists_nothing(client):
    scope, _people = setup_scope(client)
    submission = _submit_via_service(client, scope, "Hello, how are you?")
    assert submission.proposal is None
    assert submission.approval is None
    assert submission.run_id is None
    assert submission.reason == "no recognised intent"

    async def count(conn):
        return (await conn.execute(text("SELECT count(*) FROM voice_proposals"))).scalar_one()
    assert run_db(count) == 0


def test_submit_forbidden_for_plain_member(client):
    scope, _people = setup_scope(client)
    with pytest.raises(PermissionDeniedError):
        _submit_via_service(client, scope, "kya chal raha hai aaj?", email=USER_EMAIL)


def test_submit_forbidden_for_unknown_group(client):
    with pytest.raises(NotFoundError):
        _submit_via_service(client, str(uuid.uuid4()), "kya chal raha hai aaj?")


def test_mirror_is_idempotent(client):
    scope, _people = setup_scope(client)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    proposal_id = submission.proposal.id

    async def mirror():
        async with app.state.sessionmaker() as db:
            row = await db.get(VoiceProposal, proposal_id)
            return await mirror_voice_proposal(db, app.state.cipher, row)
    again = client.portal.call(mirror)
    assert str(again.id) == str(submission.run_id)
    assert len(_run_status(proposal_id)) == 1


def test_pending_sweep_mirrors_unmirrored_proposal(client):
    """A proposal created without a mirror (e.g. the route mirror missed, or
    the definition was disabled) is picked up by the engine-tick sweep.

    Note: workflow evidence (wf_events / wf_decisions) is append-only by a
    database trigger and wf_runs rows are FK-RESTRICTed, so a mirrored run
    can never be deleted to fake this state — instead the proposal record is
    created directly, exactly as submit_voice_note does before mirroring.
    """
    from app.voice.intents import parse_voice_intent
    from app.voice.proposals import propose_voice_action
    from app.voice.records import create_voice_proposal_record
    from app.workflows.action_runtime import platform_scope_for

    scope, _people = setup_scope(client)
    _make_manager(client, scope)

    async def create_unmirrored():
        async with app.state.sessionmaker() as db:
            user = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
            scope_id = await platform_scope_for(db, uuid.UUID(scope))
            intent = parse_voice_intent("kya chal raha hai aaj?")
            proposal = propose_voice_action(
                intent, scope_id=scope_id, requested_by=user.id,
                transcript="kya chal raha hai aaj?")
            row = await create_voice_proposal_record(
                db, proposal, group_id=uuid.UUID(scope), scope_id=scope_id)
            await db.commit()
            return row.id
    proposal_id = client.portal.call(create_unmirrored)
    assert _run_status(proposal_id) == []
    count = client.portal.call(mirror_pending_voice_proposals, app.state.sessionmaker, app.state.cipher)
    assert count == 1
    assert len(_run_status(proposal_id)) == 1
    count = client.portal.call(mirror_pending_voice_proposals, app.state.sessionmaker, app.state.cipher)
    assert count == 0


# --- approval decision + execution, end to end through the engine -----------

def test_status_overview_executes_after_independent_approval(client):
    scope, _people = setup_scope(client)
    _make_manager(client, scope)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    proposal_id = submission.proposal.id
    assert not _pump_until_run(client, proposal_id, want="completed", limit=2)
    response = _decide(client, submission.approval.id, "approve", submission.approval.action_hash)
    assert response.status_code == 200, response.text
    assert _pump_until_run(client, proposal_id, want="completed")
    row = _proposal(proposal_id)
    assert row["status"] == "executed"
    result = row["execution_result"]
    assert result["kind"] == "status_overview"
    assert set(result["overview"]) >= {"workflow_runs", "invoice_batches", "pending_approvals"}
    executions = _executions(proposal_id)
    assert len(executions) == 1
    assert executions[0]["status"] == "verified"
    assert executions[0]["idempotency_key"] == f"voice:{proposal_id}"


def test_execution_replay_does_not_act_twice(client):
    scope, _people = setup_scope(client)
    _make_manager(client, scope)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    proposal_id = submission.proposal.id
    _decide(client, submission.approval.id, "approve", submission.approval.action_hash)
    assert _pump_until_run(client, proposal_id, want="completed")
    first = _proposal(proposal_id)["execution_result"]

    async def replay():
        async with app.state.sessionmaker() as db:
            actor = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
            row = await db.get(VoiceProposal, proposal_id)
            from app.services.audit import AuditContext
            from app.voice.unified import execute_voice_proposal
            return await execute_voice_proposal(
                db, actor, AuditContext(actor.id, actor.email, None, None), row)
    client.portal.call(replay)
    assert _proposal(proposal_id)["execution_result"] == first
    assert len(_executions(proposal_id)) == 1


def test_approval_rejected_fails_the_run(client):
    scope, _people = setup_scope(client)
    _make_manager(client, scope)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    proposal_id = submission.proposal.id
    response = _decide(client, submission.approval.id, "reject", submission.approval.action_hash)
    assert response.status_code == 200, response.text
    assert _pump_until_run(client, proposal_id, want="failed")
    assert _proposal(proposal_id)["status"] == "proposed"  # execution never wrote the row


def test_requester_cannot_approve_own_voice_note(client):
    scope, _people = setup_scope(client)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    response = _decide(client, submission.approval.id, "approve",
                       submission.approval.action_hash, email=ADMIN_EMAIL)
    assert response.status_code == 403, response.text
    assert _approval(submission.approval.id)["status"] == "pending"


def test_decide_with_wrong_hash_is_refused(client):
    scope, _people = setup_scope(client)
    _make_manager(client, scope)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    response = _decide(client, submission.approval.id, "approve", "0" * 64)
    assert response.status_code == 409, response.text
    assert _approval(submission.approval.id)["status"] == "pending"


def test_invoice_approve_verified_invoice_records_decision_without_changing_status(client):
    scope, _people = setup_scope(client)
    _make_manager(client, scope)
    invoice_id = _seed_invoice(client, scope, number="INV-42", status="verified")
    submission = _submit_via_service(client, scope, "Approve invoice INV-42 please")
    proposal_id = submission.proposal.id
    _decide(client, submission.approval.id, "approve", submission.approval.action_hash)
    assert _pump_until_run(client, proposal_id, want="completed")
    row = _proposal(proposal_id)
    assert row["status"] == "executed"
    result = row["execution_result"]
    assert result["invoice_number"] == "INV-42"
    assert result["status_before"] == "verified"
    assert "NOT changed" in result["decision"]
    assert _invoice_status(invoice_id) == "verified"  # payment-approval flow owns the transition


def test_invoice_approve_missing_invoice_fails(client):
    scope, _people = setup_scope(client)
    _make_manager(client, scope)
    submission = _submit_via_service(client, scope, "Approve invoice INV-99 please")
    proposal_id = submission.proposal.id
    _decide(client, submission.approval.id, "approve", submission.approval.action_hash)
    assert _pump_until_run(client, proposal_id, want="failed")
    row = _proposal(proposal_id)
    assert row["status"] == "failed"
    assert "INV-99" in (row["failure_reason"] or "")


def test_invoice_approve_pending_invoice_fails(client):
    scope, _people = setup_scope(client)
    _make_manager(client, scope)
    _seed_invoice(client, scope, number="INV-43", status="pending")
    submission = _submit_via_service(client, scope, "Approve invoice INV-43 please")
    proposal_id = submission.proposal.id
    _decide(client, submission.approval.id, "approve", submission.approval.action_hash)
    assert _pump_until_run(client, proposal_id, want="failed")
    assert _proposal(proposal_id)["status"] == "failed"


def test_meeting_summary_fails_closed_without_a_reference(client):
    scope, _people = setup_scope(client)
    _make_manager(client, scope)
    submission = _submit_via_service(client, scope, "meeting ka summary bhej do")
    proposal_id = submission.proposal.id
    _decide(client, submission.approval.id, "approve", submission.approval.action_hash)
    assert _pump_until_run(client, proposal_id, want="failed")
    row = _proposal(proposal_id)
    assert row["status"] == "failed"
    assert "names no meeting" in (row["failure_reason"] or "")


# --- status endpoints ---------------------------------------------------------

def test_status_endpoint_derives_approved_then_executed(client):
    scope, _people = setup_scope(client)
    _make_manager(client, scope)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    proposal_id = submission.proposal.id
    login(client, ADMIN_EMAIL)
    body = client.get(f"/api/voice/proposals/{proposal_id}").json()
    assert body["status"] == "proposed"
    _decide(client, submission.approval.id, "approve", submission.approval.action_hash)
    login(client, ADMIN_EMAIL)
    body = client.get(f"/api/voice/proposals/{proposal_id}").json()
    assert body["status"] == "approved"
    assert _pump_until_run(client, proposal_id, want="completed")
    body = client.get(f"/api/voice/proposals/{proposal_id}").json()
    assert body["status"] == "executed"
    assert body["execution_result"]["kind"] == "status_overview"


def test_status_endpoint_hides_other_users_proposals(client):
    scope, _people = setup_scope(client)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    login(client, USER_EMAIL)
    response = client.get(f"/api/voice/proposals/{submission.proposal.id}")
    assert response.status_code == 404


def test_list_proposals_scoped_to_caller(client):
    scope, _people = setup_scope(client)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    login(client, USER_EMAIL)
    assert client.get(f"/api/voice/proposals?group_id={scope}").json() == {"proposals": []}
    login(client, ADMIN_EMAIL)
    listed = client.get(f"/api/voice/proposals?group_id={scope}").json()["proposals"]
    assert [item["id"] for item in listed] == [str(submission.proposal.id)]


def test_route_submit_with_monkeypatched_transcriber(client, monkeypatch):
    scope, _people = setup_scope(client)
    _make_manager(client, scope)

    async def fake_transcribe(audio, **kwargs):
        return ("kya chal raha hai aaj?", "wav", 12)

    monkeypatch.setattr("app.voice.service.transcribe_voice_upload", fake_transcribe)
    login(client, ADMIN_EMAIL)
    response = client.post("/api/voice/note", data={"group_id": scope},
                           files={"audio": ("note.wav", b"fake-audio-bytes", "audio/wav")})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["intent_kind"] == "status_overview"
    assert body["proposal"]["status"] == "proposed"
    assert body["approval"]["action_hash"] == body["proposal"]["action_hash"]
    assert body["wf_run_id"]


def test_route_submit_forbidden_for_plain_member(client, monkeypatch):
    scope, _people = setup_scope(client)

    async def fake_transcribe(audio, **kwargs):
        return ("kya chal raha hai aaj?", "wav", 12)

    monkeypatch.setattr("app.voice.service.transcribe_voice_upload", fake_transcribe)
    login(client, USER_EMAIL)
    response = client.post("/api/voice/note", data={"group_id": scope},
                           files={"audio": ("note.wav", b"fake-audio-bytes", "audio/wav")})
    assert response.status_code == 403, response.text


# --- migration guard -----------------------------------------------------------

def test_migration_0104_downgrade_refuses_with_voice_runs(client):
    scope, _people = setup_scope(client)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    assert _run_status(submission.proposal.id)
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    with pytest.raises(RuntimeError, match="Voice runs exist"):
        command.downgrade(cfg, "0103_unified_studio_governance")

    async def head(conn):
        return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    assert run_db(head) == "0104_voice_origin"


# --- slice 4: audit trail ------------------------------------------------------

def test_audit_events_recorded_for_proposal_and_execution(client):
    """Slice 4: ``voice.proposal_created`` at submit and
    ``voice.action_executed`` at execution both land in the append-only audit
    trail, targeted at the proposal id. The audit events are the proof that a
    voice action happened — the founder can see exactly what was proposed and
    what ran, and nothing else is recorded for this proposal."""
    scope, _people = setup_scope(client)
    _make_manager(client, scope)
    submission = _submit_via_service(client, scope, "kya chal raha hai aaj?")
    proposal_id = submission.proposal.id

    def _audit_actions():
        async def query(conn):
            rows = (await conn.execute(text(
                "SELECT action, target, outcome FROM audit_logs"
                " WHERE target = :t ORDER BY id"), {"t": str(proposal_id)})).mappings().all()
            return [(row["action"], row["target"], row["outcome"]) for row in rows]
        return run_db(query)

    assert ("voice.proposal_created", str(proposal_id), "success") in _audit_actions()
    _decide(client, submission.approval.id, "approve", submission.approval.action_hash)
    assert _pump_until_run(client, proposal_id, want="completed")
    assert _proposal(proposal_id)["status"] == "executed"
    assert ("voice.action_executed", str(proposal_id), "success") in _audit_actions()
