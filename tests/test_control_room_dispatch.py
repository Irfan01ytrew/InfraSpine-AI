"""Step AF slice 4: alert dispatch worker. Real PostgreSQL *_test only.

Covers: the collector reads live signals; a fired alert is queued once and
sent once through a recording sender and audited; cooldown/dedupe survive a
second pass (the delivery log is the durable memory); quiet hours hold
non-breaking alerts and let breaking criticals through; a failed send is
marked failed and is not re-queued inside the cooldown; a disabled config
short-circuits; with no recipients the alert falls back to in-app.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.control_room.dispatch import (
    ChannelSender,
    collect_snapshot,
    dispatch_alerts,
    send_pending,
)
from app.core.config import get_settings
from app.db.session import create_sessionmaker
from app.db.session import test_engine_options as _engine_options_for_tests
from tests.conftest import TEST_DATABASE_URL, login, run_db
from tests.test_control_room_charts import seed_scope

NOON = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)  # 17:00 PKT, outside quiet hours
NIGHT = datetime(2026, 11, 28, 18, 30, tzinfo=UTC)  # 23:30 PKT, inside quiet hours


class RecordingSender(ChannelSender):
    """Stands in for WhatsApp/email: records every send, never touches a network."""

    def __init__(self, fail_channels=()):
        self.calls: list[tuple[str, str | None, str]] = []
        self.fail_channels = set(fail_channels)

    async def send(self, channel, recipient, message):
        if channel in self.fail_channels:
            raise RuntimeError(f"simulated {channel} outage")
        self.calls.append((channel, recipient, message))
        return "provider-1"


class FailingSender(ChannelSender):
    async def send(self, channel, recipient, message):
        raise RuntimeError("simulated transport outage")


def _run(coro):
    return asyncio.run(coro)


def _sessionmaker():
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_engine_options_for_tests())
    return create_sessionmaker(engine), engine


async def _dispatch(scope, *, sender, now=NOON, config=None):
    # Production passes real UUIDs (discover_scopes / the route); pass a
    # UUID here too so db.get() on the config row matches its UUID primary
    # key instead of relying on string coercion.
    from uuid import UUID

    maker, engine = _sessionmaker()
    try:
        async with maker() as db:
            return await dispatch_alerts(db, UUID(scope), get_settings(), now=now, sender=sender, config=config)
    finally:
        await engine.dispose()


def _deliveries(scope):
    async def query(conn):
        rows = (
            await conn.execute(
                text(
                    "SELECT rule_key, channel, recipient, status FROM control_room_alert_deliveries"
                    " WHERE scope_id=:s ORDER BY created_at"
                ),
                {"s": scope},
            )
        ).all()
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    return run_db(query)


def _audit_actions():
    async def query(conn):
        rows = (await conn.execute(text("SELECT action FROM audit_logs"))).all()
        return [r[0] for r in rows]

    return run_db(query)


def _seed_failed_runs(scope, people, count=6, anchor=NOON):
    """`count` failed runs finished 10 minutes before `anchor` (spike threshold is 5).

    Rows are anchored to the fixed `anchor`, not to the database clock: the
    tests dispatch at a fixed time, and a row stamped with the real clock
    would fall outside the one-hour failure window.
    """
    workflow = uuid4()
    version = uuid4()
    finished_at = anchor - timedelta(minutes=10)

    async def seed(conn):
        await conn.execute(
            text(
                "INSERT INTO wf_definitions(id,scope_id,owner_id,name,description,latest_version,enabled)"
                " VALUES (:id,:s,:o,'Alert workflow','',1,false)"
            ),
            {"id": workflow, "s": scope, "o": people["user@example.com"]},
        )
        await conn.execute(
            text(
                "INSERT INTO wf_versions(id,scope_id,workflow_id,number,spec,digest,status,author_id)"
                " VALUES (:id,:s,:w,1,'{}'::jsonb,'x','published',:o)"
            ),
            {"id": version, "s": scope, "w": workflow, "o": people["user@example.com"]},
        )
        for _ in range(count):
            run_id = uuid4()
            await conn.execute(
                text(
                    "INSERT INTO wf_runs(id,scope_id,workflow_id,version_id,requester_id,"
                    "idempotency_key,input_digest,payload,status,phase,event_sequence,event_hash,"
                    "failure_code,finished_at,simulation,priority)"
                    " VALUES (:id,:s,:w,:v,:o,:key,'digest','{}'::jsonb,'failed','done',0,"
                    "'0000000000000000000000000000000000000000000000000000000000000000',"
                    "'boom',:fin,false,0)"
                ),
                {"id": run_id, "s": scope, "w": workflow, "v": version,
                 "o": people["user@example.com"], "key": f"key-{run_id}", "fin": finished_at},
            )

    run_db(seed)


def _seed_finance_signals(scope, people, anchor=NOON):
    """1 failed payment (10 min before `anchor`), 1 payment awaiting approval
    for 30 h, 12 exceptions. Timestamps are anchored to `anchor`, not the
    database clock (see `_seed_failed_runs`)."""
    batch = uuid4()
    vendor = uuid4()
    failed_at = anchor - timedelta(minutes=10)
    awaiting_since = anchor - timedelta(hours=30)

    async def seed(conn):
        await conn.execute(
            text(
                "INSERT INTO fin_vendors(id,group_id,name,name_key,bank_account,currency)"
                " VALUES (:id,:g,'Acme','acme','PK00ACME','PKR')"
            ),
            {"id": vendor, "g": scope},
        )
        await conn.execute(
            text(
                "INSERT INTO fin_invoice_batches(id,group_id,name,created_by,status,currency,tolerance_pct,tolerance_abs)"
                " VALUES (:id,:g,'November',:by,'open','PKR',2.00,0)"
            ),
            {"id": batch, "g": scope, "by": people["admin@example.com"]},
        )
        await conn.execute(
            text(
                "INSERT INTO fin_payments(id,group_id,batch_id,vendor_id,round,amount,currency,payee_account,"
                "invoice_ids,status,created_at,updated_at)"
                " VALUES (:id,:g,:b,:v,1,100.00,'PKR','PK00ACME','[]'::jsonb,'failed',:at,:at)"
            ),
            {"id": uuid4(), "g": scope, "b": batch, "v": vendor, "at": failed_at},
        )
        await conn.execute(
            text(
                "INSERT INTO fin_payments(id,group_id,batch_id,vendor_id,round,amount,currency,payee_account,"
                "invoice_ids,status,created_at,updated_at)"
                " VALUES (:id,:g,:b,:v,1,50.00,'PKR','PK00ACME','[]'::jsonb,'awaiting_approval',:since,:since)"
            ),
            {"id": uuid4(), "g": scope, "b": batch, "v": vendor, "since": awaiting_since},
        )
        for seq in range(12):
            await conn.execute(
                text(
                    "INSERT INTO fin_invoices(id,group_id,batch_id,seq,invoice_number,source_kind,"
                    "source_digest,status,amount,currency,invoice_date,department)"
                    " VALUES (:id,:g,:b,:seq,:num,'json','digest','exception',10.00,'PKR',CURRENT_DATE,'Ops')"
                ),
                {"id": uuid4(), "g": scope, "b": batch, "seq": seq, "num": f"EXC-{seq}"},
            )

    run_db(seed)


def _seed_failed_document(people, anchor=NOON):
    failed_at = anchor - timedelta(minutes=10)

    async def seed(conn):
        await conn.execute(
            text(
                "INSERT INTO documents(id,owner_id,visibility,source_system,filename,file_format,"
                "storage_key,sha256,size_bytes,status,updated_at)"
                " VALUES (:id,:o,'private','upload','broken.pdf','pdf','k','"
                "0000000000000000000000000000000000000000000000000000000000000000',10,'failed',:at)"
            ),
            {"id": uuid4(), "o": people["admin@example.com"], "at": failed_at},
        )

    run_db(seed)


def _save_config(client, scope, payload):
    login(client, "admin@example.com")
    response = client.put(f"/api/control-room/scopes/{scope}/config", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_collect_snapshot_reads_live_signals(client):
    scope, people = seed_scope(client)
    _seed_failed_runs(scope, people, count=6)
    _seed_finance_signals(scope, people)
    _seed_failed_document(people)

    async def collect():
        maker, engine = _sessionmaker()
        try:
            async with maker() as db:
                return await collect_snapshot(db, scope, get_settings(), now=NOON)
        finally:
            await engine.dispose()

    snapshot = _run(collect())
    assert snapshot.failed_runs_last_hour == 6
    assert snapshot.failed_payments_last_hour == 1
    assert snapshot.exceptions_count == 12
    assert snapshot.failed_ingestions_last_hour == 1
    assert snapshot.oldest_approval_age_hours is not None
    assert snapshot.oldest_approval_age_hours >= 29
    # No persisted worker heartbeat yet: the tick age is honestly None.
    assert snapshot.last_tick_age_seconds is None


def test_dispatch_sends_fired_alert_once_and_audits(client):
    scope, people = seed_scope(client)
    _seed_failed_runs(scope, people, count=6)
    _save_config(client, scope, {"recipients": [{"channel": "whatsapp", "address": "+923001234567"}]})
    sender = RecordingSender()

    summary = _run(_dispatch(scope, sender=sender))
    assert summary["evaluated"] >= 1
    assert summary["queued"] >= 1
    assert summary["sent"] == summary["queued"]
    assert summary["failed"] == 0
    # The spike alert went to the configured WhatsApp number, once.
    spike = [c for c in sender.calls if "Failed runs spiking" in c[2]]
    assert len(spike) == 1
    assert spike[0][0] == "whatsapp"
    assert spike[0][1] == "+923001234567"
    rows = _deliveries(scope)
    assert all(status == "sent" for *_, status in rows)
    assert "control_room.alert_sent" in _audit_actions()


def test_dispatch_cooldown_survives_second_pass(client):
    """The delivery log — not process memory — is what dedupes a repeat pass."""
    scope, people = seed_scope(client)
    _seed_failed_runs(scope, people, count=6)
    _save_config(client, scope, {"recipients": [{"channel": "whatsapp", "address": "+923001234567"}]})
    sender = RecordingSender()

    first = _run(_dispatch(scope, sender=sender))
    assert first["sent"] >= 1
    sent_first = len(sender.calls)
    second = _run(_dispatch(scope, sender=sender))
    assert second["queued"] == 0
    assert second["sent"] == 0
    assert len(sender.calls) == sent_first  # nothing re-sent


def test_dispatch_quiet_hours_hold_warning_but_not_breaking_critical(client):
    scope, people = seed_scope(client)
    _seed_finance_signals(scope, people, anchor=NIGHT)  # signals fresh at 23:30, not at noon
    _save_config(client, scope, {"recipients": [{"channel": "email", "address": "owner@example.com"}]})
    sender = RecordingSender()

    summary = _run(_dispatch(scope, sender=sender, now=NIGHT))
    sent_rules = {row[0] for row in _deliveries(scope) if row[3] == "sent"}
    assert "failed_payment" in sent_rules  # breaks quiet hours
    assert "exceptions_backlog" not in sent_rules  # held until morning
    assert "approval_stuck" not in sent_rules


def test_dispatch_no_recipients_falls_back_to_in_app(client):
    scope, people = seed_scope(client)
    _seed_failed_runs(scope, people, count=6)
    sender = RecordingSender()

    summary = _run(_dispatch(scope, sender=sender))
    assert summary["queued"] >= 1
    assert sender.calls == []  # nothing left the building
    rows = _deliveries(scope)
    assert rows and all(channel == "in_app" and status == "sent" for _, channel, _, status in rows)


def test_dispatch_failed_send_marks_failed_and_waits_out_cooldown(client):
    scope, people = seed_scope(client)
    _seed_failed_runs(scope, people, count=6)
    _save_config(client, scope, {"recipients": [{"channel": "whatsapp", "address": "+923001234567"}]})

    summary = _run(_dispatch(scope, sender=FailingSender()))
    assert summary["sent"] == 0
    assert summary["failed"] >= 1
    rows = _deliveries(scope)
    assert rows and all(status == "failed" for *_, status in rows)

    # Immediate retry does not re-queue inside the rule cooldown.
    retry = _run(_dispatch(scope, sender=RecordingSender()))
    assert retry["queued"] == 0

    # After the cooldown the same alert may fire again. The original runs
    # are stale by then (outside the one-hour window), so fresh failures are
    # seeded at the later time — the point under test is the cooldown, not
    # the signal.
    later = NOON + timedelta(hours=2)
    _seed_failed_runs(scope, people, count=6, anchor=later)
    later_summary = _run(_dispatch(scope, sender=RecordingSender(), now=later))
    assert later_summary["queued"] >= 1
    assert later_summary["sent"] >= 1


def test_dispatch_disabled_config_short_circuits(client):
    scope, people = seed_scope(client)
    _seed_failed_runs(scope, people, count=6)
    _save_config(client, scope, {"enabled": False, "recipients": [{"channel": "whatsapp", "address": "+923001234567"}]})
    sender = RecordingSender()

    summary = _run(_dispatch(scope, sender=sender))
    assert summary == {"scope_id": scope, "evaluated": 0, "queued": 0, "sent": 0, "failed": 0, "disabled": True}
    assert sender.calls == []
    assert _deliveries(scope) == []


def test_send_pending_claims_each_row_once(client):
    scope, people = seed_scope(client)
    _seed_failed_runs(scope, people, count=6)
    _save_config(client, scope, {"recipients": [{"channel": "whatsapp", "address": "+923001234567"}]})
    sender = RecordingSender()
    first = _run(_dispatch(scope, sender=sender))
    assert first["sent"] >= 1

    async def resend():
        maker, engine = _sessionmaker()
        try:
            async with maker() as db:
                return await send_pending(db, sender=sender, now=NOON)
        finally:
            await engine.dispose()

    again = _run(resend())
    assert again == {"claimed": 0, "sent": 0, "failed": 0}
    assert len(sender.calls) == first["sent"]


def test_dispatch_respects_per_rule_threshold_override(client):
    scope, people = seed_scope(client)
    _seed_failed_runs(scope, people, count=6)
    # Raise the spike threshold above the 6 observed failures: no alert.
    _save_config(
        client,
        scope,
        {
            "recipients": [{"channel": "whatsapp", "address": "+923001234567"}],
            "rules": {"failed_runs_spike": {"threshold": 10}},
        },
    )
    sender = RecordingSender()
    summary = _run(_dispatch(scope, sender=sender))
    assert not [c for c in sender.calls if "Failed runs spiking" in c[2]]
