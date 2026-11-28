"""Tests for Step L slice 5: live-work wiring.

Pure tests cover the live-work contract (normalize, fingerprint,
changes, refresh gating, attach, snapshot refresh, summarize).
DB tests run the reflection loaders + the end-to-end refresh +
the scheduler/CLI wiring against throwaway ``*_test`` DBs (the
conftest migrates TEST_DATABASE_URL to head, so ``wf_runs``,
``automation_runs``, ``platform_approval_requests``,
``fin_invoice_batches`` and ``fin_payments`` all exist there);
rows are inserted through the real models, so the Postgres CHECK
constraints enforce the status sets the loaders rely on.
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.company_graph.models import GraphTenant
from app.db.finance_models import FinInvoiceBatch, FinPayment, FinVendor
from app.db.models import AccessGroup, AutomationApproval, AutomationRun, AutomationWorkflow, User
from app.db.session import test_engine_options as _test_engine_options
from app.db.workflow_engine_models import Workflow, WorkflowRun, WorkflowVersion
from app.platform.models import PlatformApprovalRequest, PlatformScope
from app.predictive_continuity.live_work import (
    MAX_LIVE_WORK_ITEMS,
    attach_live_work,
    live_work_changes,
    live_work_fingerprint,
    live_work_refresh_needed,
    load_automation_runs,
    load_invoice_batches,
    load_live_work,
    load_payments,
    load_pending_approvals,
    load_wf_runs,
    load_workflow_runs,
    normalize_live_work_item,
    normalize_live_work_items,
    refresh_forecast_snapshot,
    refresh_snapshot_live_work,
    summarize_live_work,
)
from app.predictive_continuity.models import ForecastRecord
from app.predictive_continuity.scheduler import (
    format_scan_summary,
    load_state,
    run_forecast_scan,
)
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL

pytestmark = pytest.mark.usefixtures("clean_tables")

PKT = ZoneInfo("Asia/Karachi")
NOW = datetime(2026, 11, 28, 17, 0, tzinfo=PKT)  # daytime
ROOT = Path(__file__).resolve().parent.parent


def run(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _item(**overrides):
    item = {"kind": "workflow_run", "id": "wr-1", "name": "Invoice run 7",
            "status": "running"}
    item.update(overrides)
    return item


# --- normalize ---------------------------------------------------------------

def test_normalize_full_item():
    item = normalize_live_work_item({
        "kind": "approval", "id": " ap-1 ", "name": " invoice:INV-204 ",
        "status": "pending", "process_id": "pr-1", "due_at": "2026-12-02",
        "amount": Decimal("1500.00"), "currency": "pkr",
        "subject": "invoice:INV-204", "source": "platform_approval_requests",
    })
    assert item["id"] == "ap-1"
    assert item["name"] == "invoice:INV-204"
    assert item["due_at"] == "2026-12-02"
    assert item["amount"] == 1500.0 and isinstance(item["amount"], float)
    assert item["currency"] == "PKR"


def test_normalize_minimal_item():
    item = normalize_live_work_item({"kind": "payment", "id": "pay-1"})
    assert item == {"kind": "payment", "id": "pay-1", "name": None,
                    "status": None, "process_id": None, "due_at": None,
                    "amount": None, "currency": None, "subject": None,
                    "source": None}


def test_normalize_rejects_bad_items():
    with pytest.raises(ValueError):
        normalize_live_work_item({"kind": "person", "id": "p-1"})
    with pytest.raises(ValueError):
        normalize_live_work_item({"kind": "approval", "id": "   "})
    with pytest.raises(ValueError):
        normalize_live_work_item({"kind": "approval", "id": "a-1", "bogus": 1})
    with pytest.raises(ValueError):
        normalize_live_work_item({"kind": "approval", "id": "a-1", "currency": "PKRX"})
    with pytest.raises(ValueError):
        normalize_live_work_item({"kind": "approval", "id": "a-1", "amount": -5})
    with pytest.raises(ValueError):
        normalize_live_work_item({"kind": "approval", "id": "a-1", "amount": True})
    with pytest.raises(ValueError):
        normalize_live_work_item({"kind": "approval", "id": "a-1", "due_at": "soon"})
    with pytest.raises(ValueError):
        normalize_live_work_items([_item(), _item()])  # duplicate (kind, id)
    with pytest.raises(ValueError):
        normalize_live_work_items([_item(id=f"w-{i}") for i in range(MAX_LIVE_WORK_ITEMS + 1)])
    with pytest.raises(ValueError):
        normalize_live_work_items(None)


def test_normalize_sorts_by_kind_name_id():
    items = normalize_live_work_items([
        _item(kind="approval", id="a-2", name="B"),
        _item(kind="approval", id="a-1", name="A"),
        _item(kind="workflow_run", id="w-1", name="Z"),
    ])
    assert [(i["kind"], i["id"]) for i in items] == [
        ("approval", "a-1"), ("approval", "a-2"), ("workflow_run", "w-1")]


# --- fingerprint / changes / gating ------------------------------------------

def test_fingerprint_stable_order_independent_and_status_sensitive():
    a = normalize_live_work_items([_item(), _item(kind="approval", id="a-1", status="pending")])
    b = normalize_live_work_items([_item(kind="approval", id="a-1", status="pending"), _item()])
    assert live_work_fingerprint(a) == live_work_fingerprint(b)
    c = normalize_live_work_items([_item(status="completed"),
                                   _item(kind="approval", id="a-1", status="pending")])
    assert live_work_fingerprint(a) != live_work_fingerprint(c)


def test_live_work_changes_added_removed_changed():
    before = [_item(), _item(kind="approval", id="a-1", status="pending")]
    after = [_item(status="completed"), _item(kind="invoice_batch", id="b-1", status="open")]
    diff = live_work_changes(before, after)
    assert [(i["kind"], i["id"]) for i in diff["added"]] == [("invoice_batch", "b-1")]
    assert [(i["kind"], i["id"]) for i in diff["removed"]] == [("approval", "a-1")]
    assert diff["changed"] == [{"kind": "workflow_run", "id": "wr-1",
                                "fields": ["status"],
                                "previous_status": "running",
                                "current_status": "completed"}]
    assert live_work_changes(before, before) == {"added": [], "removed": [], "changed": []}


def test_live_work_refresh_needed():
    fp = live_work_fingerprint([_item()])
    assert live_work_refresh_needed(None, "twin", None, fp) is True
    assert live_work_refresh_needed("twin", "twin", fp, fp) is False
    assert live_work_refresh_needed("twin", "twin2", fp, fp) is True
    assert live_work_refresh_needed("twin", "twin", fp, "other") is True
    assert live_work_refresh_needed("twin", "twin", None, fp) is True


def test_summarize_live_work_counts_only():
    summary = summarize_live_work([
        _item(), _item(id="wr-2", status="queued"),
        _item(kind="approval", id="a-1", status="pending"),
    ])
    assert summary == {"total": 3,
                       "by_kind": {"approval": 1, "workflow_run": 2},
                       "by_status": {"running": 1, "queued": 1, "pending": 1}}


# --- attach ------------------------------------------------------------------

def _forecast():
    return {"processes": [
        {"id": "pr-1", "name": "Pay suppliers",
         "live_work_items": [{"id": "wr-0", "name": "Old run",
                              "kind": "workflow_run", "status": "queued"}]},
        {"id": "pr-2", "name": "Onboard vendor", "live_work_items": []},
    ]}


def test_attach_to_matching_process_and_replaces_stale_entry():
    original = _forecast()
    result = attach_live_work(original, [
        {"kind": "workflow_run", "id": "wr-0", "status": "running", "process_id": "pr-1"},
        {"kind": "approval", "id": "a-1", "status": "pending", "process_id": "pr-1"},
        {"kind": "invoice_batch", "id": "b-9", "status": "open", "process_id": "pr-9"},
        {"kind": "payment", "id": "pay-1", "status": "approved"},
    ])
    assert result["attached"] == 2
    assert [(i["kind"], i["id"]) for i in result["unmatched"]] == [
        ("invoice_batch", "b-9"), ("payment", "pay-1")]
    items = result["forecast"]["processes"][0]["live_work_items"]
    assert [(i["kind"], i["id"], i["status"]) for i in items] == [
        ("approval", "a-1", "pending"), ("workflow_run", "wr-0", "running")]
    # The caller's forecast is never mutated.
    assert original["processes"][0]["live_work_items"][0]["status"] == "queued"
    assert result["forecast"]["processes"][1]["live_work_items"] == []


def test_attach_rejects_forecast_without_processes():
    with pytest.raises(ValueError):
        attach_live_work({"affected": []}, [_item(process_id="pr-1")])


# --- snapshot refresh ---------------------------------------------------------

def _snapshot_with_live_work(run_id="wr-1", batch_id="b-1"):
    entities = [
        {"id": "p-1", "kind": "person", "name": "Ayesha",
         "attributes": {"skills": ["invoicing", "approvals"]}},
        {"id": "pr-1", "kind": "process", "name": "Pay suppliers",
         "attributes": {"required_skills": ["approvals"], "due_at": "2026-12-03"}},
        {"id": batch_id, "kind": "invoice_batch", "name": "March batch",
         "attributes": {"amount": 1500.00, "currency": "PKR",
                        "status": "awaiting_approval", "due_at": "2026-12-02"}},
        {"id": run_id, "kind": "workflow_run", "name": "Invoice run 7",
         "attributes": {"status": "queued"}},
    ]
    relationships = [
        {"id": "e-1", "source_id": "p-1", "target_id": "pr-1", "kind": "approves", "confidence": 1.0},
        {"id": "e-7", "source_id": batch_id, "target_id": "pr-1", "kind": "depends_on", "confidence": 1.0},
        {"id": "e-8", "source_id": run_id, "target_id": "pr-1", "kind": "depends_on", "confidence": 1.0},
    ]
    return {"entities": entities, "relationships": relationships,
            "as_of": "2026-11-28T00:00:00+00:00"}


def test_refresh_snapshot_updates_status_and_reports_added_stale():
    snapshot = _snapshot_with_live_work()
    report = refresh_snapshot_live_work(snapshot, [
        {"kind": "workflow_run", "id": "wr-1", "status": "running", "source": "wf_runs"},
        {"kind": "invoice_batch", "id": "b-1", "status": "awaiting_approval"},
        {"kind": "approval", "id": "a-1", "status": "pending"},
    ])
    assert report["matched"] == 2
    assert report["updated"] == [{"kind": "workflow_run", "id": "wr-1",
                                  "previous_status": "queued", "status": "running"}]
    assert [(i["kind"], i["id"]) for i in report["added"]] == [("approval", "a-1")]
    assert report["stale"] == []
    assert report["fingerprint_before"] != report["fingerprint_after"]
    refreshed = report["snapshot"]
    run_entity = next(e for e in refreshed["entities"] if e["id"] == "wr-1")
    assert run_entity["attributes"]["status"] == "running"
    # The input snapshot is never mutated; nothing else changed.
    assert next(e for e in snapshot["entities"] if e["id"] == "wr-1")["attributes"]["status"] == "queued"
    assert next(e for e in refreshed["entities"] if e["id"] == "b-1")["attributes"]["status"] == "awaiting_approval"


def test_refresh_snapshot_stale_entity_kept_with_snapshot_status():
    snapshot = _snapshot_with_live_work()
    report = refresh_snapshot_live_work(snapshot, [])
    assert report["matched"] == 0
    assert report["updated"] == []
    assert report["added"] == []
    assert [(s["kind"], s["id"]) for s in report["stale"]] == [
        ("invoice_batch", "b-1"), ("workflow_run", "wr-1")]
    assert report["fingerprint_before"] == report["fingerprint_after"]
    assert report["snapshot"]["entities"] == snapshot["entities"]


def test_refresh_snapshot_rejects_malformed_input():
    with pytest.raises(ValueError):
        refresh_snapshot_live_work({"relationships": []}, [])
    bad = _snapshot_with_live_work()
    bad["entities"][2]["attributes"]["amount"] = -5
    with pytest.raises(ValueError):
        refresh_snapshot_live_work(bad, [])
    with pytest.raises(ValueError):
        refresh_snapshot_live_work(_snapshot_with_live_work(), [{"kind": "person", "id": "p-1"}])


# --- DB seeding ---------------------------------------------------------------

def _seed_live_work():
    """Seed one in-flight + one terminal row per source; return the ids."""
    async def go(sm):
        async with sm() as db:
            admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
            group = AccessGroup(name="LiveWorkDept", kind="department")
            db.add(group)
            await db.flush()
            scope = await db.scalar(select(PlatformScope).where(PlatformScope.kind == "org"))
            if scope is None:
                scope = PlatformScope(kind="org", name="LiveWorkOrg")
                db.add(scope)
                await db.flush()
            workflow = Workflow(scope_id=group.id, owner_id=admin.id, name="Pay suppliers flow")
            db.add(workflow)
            await db.flush()
            version = WorkflowVersion(scope_id=group.id, workflow_id=workflow.id,
                                      number=1, spec={}, digest="0" * 64,
                                      status="published", author_id=admin.id)
            db.add(version)
            await db.flush()
            wf_run = WorkflowRun(scope_id=group.id, workflow_id=workflow.id,
                                 version_id=version.id, requester_id=admin.id,
                                 idempotency_key="lw-wf-1", input_digest="0" * 64,
                                 payload={}, status="running",
                                 deadline_at=datetime(2026, 12, 5, tzinfo=UTC))
            wf_done = WorkflowRun(scope_id=group.id, workflow_id=workflow.id,
                                  version_id=version.id, requester_id=admin.id,
                                  idempotency_key="lw-wf-2", input_digest="0" * 64,
                                  payload={}, status="completed")
            db.add_all([wf_run, wf_done])
            auto_wf = AutomationWorkflow(
                definition_key=uuid.uuid4(), version=1, name="Nightly sweep",
                department_group_id=group.id, trigger_type="manual",
                action_type="create_task", action_config={})
            db.add(auto_wf)
            await db.flush()
            # Legacy automation runs are trigger-guarded: INSERT must begin
            # pending_approval (attempts 0); reaching 'queued' needs a real
            # automation_approvals row from a *different*, active admin
            # approver; only the legal transitions may follow
            # (pending_approval -> queued -> processing -> completed/failed).
            # Seed through the machine, one flush per hop.
            approver = User(email="lw-approver@example.com",
                            password_hash="x" * 60, role="admin", is_active=True)
            db.add(approver)
            await db.flush()
            auto_run = AutomationRun(workflow_id=auto_wf.id, requester_user_id=admin.id,
                                     department_group_id=group.id,
                                     idempotency_key="lw-auto-1", status="pending_approval")
            auto_run_done = AutomationRun(workflow_id=auto_wf.id, requester_user_id=admin.id,
                                          department_group_id=group.id,
                                          idempotency_key="lw-auto-2", status="pending_approval")
            db.add_all([auto_run, auto_run_done])
            await db.flush()
            db.add_all([
                AutomationApproval(run_id=auto_run.id, approver_user_id=approver.id,
                                   decision="approved",
                                   rationale="Approved for live-work test seeding."),
                AutomationApproval(run_id=auto_run_done.id, approver_user_id=approver.id,
                                   decision="approved",
                                   rationale="Approved for live-work test seeding."),
            ])
            await db.flush()
            auto_run.status = "queued"
            await db.flush()
            auto_run_done.status = "queued"
            await db.flush()
            auto_run_done.status = "processing"
            await db.flush()
            auto_run_done.status = "failed"
            await db.flush()
            approval = PlatformApprovalRequest(
                scope_id=scope.id, action_type="finance.pay",
                action_hash="a" * 64, action={}, subject="invoice:INV-204",
                amount=Decimal("1500.00"), currency="PKR",
                required_permission="finance.approve", requested_by=admin.id,
                requested_via="test", status="pending",
                expires_at=datetime(2026, 12, 10, tzinfo=UTC), trace_id=uuid.uuid4())
            approval_done = PlatformApprovalRequest(
                scope_id=scope.id, action_type="finance.pay",
                action_hash="b" * 64, action={}, subject="invoice:INV-205",
                amount=Decimal("10.00"), currency="PKR",
                required_permission="finance.approve", requested_by=admin.id,
                requested_via="test", status="approved",
                expires_at=datetime(2026, 12, 10, tzinfo=UTC), trace_id=uuid.uuid4())
            db.add_all([approval, approval_done])
            batch = FinInvoiceBatch(group_id=group.id, name="March batch",
                                    created_by=admin.id, status="awaiting_approval",
                                    currency="PKR", wf_run_id=wf_run.id)
            batch_done = FinInvoiceBatch(group_id=group.id, name="Feb batch",
                                         created_by=admin.id, status="completed",
                                         currency="PKR")
            db.add_all([batch, batch_done])
            vendor = FinVendor(group_id=group.id, name="Acme Supplies",
                               name_key="acme-supplies", bank_account="PK00ACME01",
                               currency="PKR")
            db.add(vendor)
            await db.flush()
            payment = FinPayment(group_id=group.id, batch_id=batch.id,
                                 vendor_id=vendor.id, amount=Decimal("1500.00"),
                                 currency="PKR", payee_account="PK00ACME01",
                                 invoice_ids=[], approval_id=approval.id,
                                 status="awaiting_approval")
            payment_done = FinPayment(group_id=group.id, batch_id=batch.id,
                                      vendor_id=vendor.id, amount=Decimal("10.00"),
                                      currency="PKR", payee_account="PK00ACME01",
                                      invoice_ids=[], status="verified")
            db.add_all([payment, payment_done])
            await db.commit()
            return {
                "group_id": group.id, "scope_id": scope.id,
                "wf_run_id": wf_run.id, "auto_run_id": auto_run.id,
                "approval_id": approval.id, "batch_id": batch.id,
                "payment_id": payment.id,
            }

    return run(go)


def _run_loader(fn):
    """Run an async loader call against the test DB, return its result."""
    async def go(sm):
        async with sm() as db:
            return await fn(db)

    return run(go)


# --- loaders -------------------------------------------------------------------

def test_load_wf_runs_returns_in_flight_only_with_names_and_deadline():
    ids = _seed_live_work()
    rows = _run_loader(lambda db: load_wf_runs(db))
    assert [(r["kind"], r["id"]) for r in rows] == [("workflow_run", str(ids["wf_run_id"]))]
    row = rows[0]
    assert row["name"] == "Pay suppliers flow"
    assert row["status"] == "running"
    assert row["due_at"] == "2026-12-05T00:00:00+00:00"
    assert row["source"] == "wf_runs"


def test_load_automation_runs_returns_in_flight_only():
    ids = _seed_live_work()
    rows = _run_loader(lambda db: load_automation_runs(db))
    assert [(r["kind"], r["id"]) for r in rows] == [("workflow_run", str(ids["auto_run_id"]))]
    assert rows[0]["name"] == "Nightly sweep"
    assert rows[0]["status"] == "queued"
    assert rows[0]["source"] == "automation_runs"


def test_load_workflow_runs_combines_both_sources():
    ids = _seed_live_work()
    rows = _run_loader(lambda db: load_workflow_runs(db))
    assert {(r["id"], r["source"]) for r in rows} == {
        (str(ids["wf_run_id"]), "wf_runs"),
        (str(ids["auto_run_id"]), "automation_runs"),
    }


def test_load_pending_approvals_only_pending_with_amounts():
    ids = _seed_live_work()
    rows = _run_loader(lambda db: load_pending_approvals(db))
    assert [(r["kind"], r["id"]) for r in rows] == [("approval", str(ids["approval_id"]))]
    row = rows[0]
    assert row["name"] == "invoice:INV-204"
    assert row["subject"] == "invoice:INV-204"
    assert row["amount"] == 1500.0 and isinstance(row["amount"], float)
    assert row["currency"] == "PKR"
    assert row["due_at"] == "2026-12-10T00:00:00+00:00"


def test_load_invoice_batches_only_in_flight():
    ids = _seed_live_work()
    rows = _run_loader(lambda db: load_invoice_batches(db))
    assert [(r["kind"], r["id"]) for r in rows] == [("invoice_batch", str(ids["batch_id"]))]
    assert rows[0]["name"] == "March batch"
    assert rows[0]["status"] == "awaiting_approval"
    assert rows[0]["currency"] == "PKR"


def test_load_payments_only_in_flight_with_batch_subject():
    ids = _seed_live_work()
    rows = _run_loader(lambda db: load_payments(db))
    assert [(r["kind"], r["id"]) for r in rows] == [("payment", str(ids["payment_id"]))]
    row = rows[0]
    assert row["amount"] == 1500.0
    assert row["subject"] == f"batch:{ids['batch_id']}"
    assert row["status"] == "awaiting_approval"


def test_load_live_work_combines_all_sources():
    ids = _seed_live_work()
    rows = _run_loader(lambda db: load_live_work(db))
    assert {(r["kind"], r["id"]) for r in rows} == {
        ("workflow_run", str(ids["wf_run_id"])),
        ("workflow_run", str(ids["auto_run_id"])),
        ("approval", str(ids["approval_id"])),
        ("invoice_batch", str(ids["batch_id"])),
        ("payment", str(ids["payment_id"])),
    }
    assert summarize_live_work(rows)["total"] == 5


def test_loaders_return_empty_when_nothing_in_flight():
    assert _run_loader(lambda db: load_live_work(db)) == []
    assert _run_loader(lambda db: load_wf_runs(db)) == []


def test_loaders_reject_bad_limit():
    with pytest.raises(ValueError):
        _run_loader(lambda db: load_live_work(db, limit=0))


def test_loader_columns_match_models():
    """The loader's pinned column sets must exist on the real models.

    If a model renames a column, this fails first — the loader must
    be updated, not silently read the wrong table.
    """
    from app.db.models import AutomationRun, AutomationWorkflow
    from app.db.workflow_engine_models import Workflow as WfDefinition
    from app.predictive_continuity.live_work import (
        _APPROVAL_COLUMNS,
        _AUTOMATION_RUN_COLUMNS,
        _AUTOMATION_WORKFLOW_COLUMNS,
        _BATCH_COLUMNS,
        _PAYMENT_COLUMNS,
        _WF_DEFINITION_COLUMNS,
        _WF_RUN_COLUMNS,
    )

    assert _WF_RUN_COLUMNS <= set(WorkflowRun.__table__.columns.keys())
    assert _WF_DEFINITION_COLUMNS <= set(WfDefinition.__table__.columns.keys())
    assert _AUTOMATION_RUN_COLUMNS <= set(AutomationRun.__table__.columns.keys())
    assert _AUTOMATION_WORKFLOW_COLUMNS <= set(AutomationWorkflow.__table__.columns.keys())
    assert _APPROVAL_COLUMNS <= set(PlatformApprovalRequest.__table__.columns.keys())
    assert _BATCH_COLUMNS <= set(FinInvoiceBatch.__table__.columns.keys())
    assert _PAYMENT_COLUMNS <= set(FinPayment.__table__.columns.keys())


# --- end to end: refresh + forecast ---------------------------------------------

def _scan_snapshot(ids):
    """Scheduler-fixture twin, but live-work ids are the seeded DB rows.

    Snapshot statuses are deliberately stale (queued / open /
    approved) so the refresh visibly replaces them with the live
    ones (running / awaiting_approval / pending).
    """
    run_id, batch_id = str(ids["wf_run_id"]), str(ids["batch_id"])
    approval_id, payment_id = str(ids["approval_id"]), str(ids["payment_id"])
    auto_run_id = str(ids["auto_run_id"])
    entities = [
        {"id": "p-1", "kind": "person", "name": "Ayesha",
         "attributes": {"skills": ["invoicing", "approvals"]}},
        {"id": "p-2", "kind": "person", "name": "Bilal",
         "attributes": {"skills": ["invoicing", "approvals"]}},
        {"id": "t-1", "kind": "team", "name": "Finance", "attributes": {}},
        {"id": "sys-1", "kind": "system", "name": "ERP", "attributes": {}},
        {"id": "sup-1", "kind": "supplier", "name": "Acme Supplies", "attributes": {}},
        {"id": "pr-1", "kind": "process", "name": "Pay suppliers",
         "attributes": {"required_skills": ["approvals"], "due_at": "2026-12-03"}},
        {"id": "pr-2", "kind": "process", "name": "Onboard vendor",
         "attributes": {"due_at": "2026-12-20"}},
        {"id": "pr-3", "kind": "process", "name": "File tax return",
         "attributes": {"sla_days": 3}},
        {"id": batch_id, "kind": "invoice_batch", "name": "March batch",
         "attributes": {"amount": 1500.00, "currency": "PKR",
                        "status": "open", "due_at": "2026-12-02"}},
        {"id": run_id, "kind": "workflow_run", "name": "Invoice run 7",
         "attributes": {"status": "queued"}},
        {"id": auto_run_id, "kind": "workflow_run", "name": "Nightly sweep",
         "attributes": {"status": "pending_approval"}},
        {"id": approval_id, "kind": "approval", "name": "invoice:INV-204",
         "attributes": {"status": "approved"}},
        {"id": payment_id, "kind": "payment", "name": "Payment",
         "attributes": {"amount": 1500.00, "currency": "PKR", "status": "approved"}},
    ]
    relationships = [
        {"id": "e-1", "source_id": "p-1", "target_id": "pr-1", "kind": "approves", "confidence": 1.0},
        {"id": "e-2", "source_id": "p-1", "target_id": "pr-2", "kind": "owns", "confidence": 1.0},
        {"id": "e-3", "source_id": "p-1", "target_id": "pr-3", "kind": "responsible_for", "confidence": 1.0},
        {"id": "e-4", "source_id": "p-2", "target_id": "pr-1", "kind": "backup_for", "confidence": 1.0},
        {"id": "e-5", "source_id": "pr-1", "target_id": "sys-1", "kind": "depends_on", "confidence": 1.0},
        {"id": "e-6", "source_id": "sup-1", "target_id": "sys-1", "kind": "supplies", "confidence": 1.0},
        {"id": "e-7", "source_id": batch_id, "target_id": "pr-1", "kind": "depends_on", "confidence": 1.0},
        {"id": "e-8", "source_id": run_id, "target_id": "pr-1", "kind": "depends_on", "confidence": 1.0},
        {"id": "e-9", "source_id": approval_id, "target_id": "pr-1", "kind": "depends_on", "confidence": 1.0},
        {"id": "e-10", "source_id": payment_id, "target_id": "pr-1", "kind": "depends_on", "confidence": 1.0},
        {"id": "e-11", "source_id": "p-1", "target_id": "t-1", "kind": "member_of", "confidence": 1.0},
        {"id": "e-12", "source_id": "p-2", "target_id": "t-1", "kind": "member_of", "confidence": 1.0},
        {"id": "e-13", "source_id": auto_run_id, "target_id": "pr-1", "kind": "depends_on", "confidence": 1.0},
    ]
    return {"entities": entities, "relationships": relationships,
            "as_of": "2026-11-28T00:00:00+00:00"}


def test_refresh_forecast_snapshot_end_to_end():
    ids = _seed_live_work()
    snapshot = _scan_snapshot(ids)
    report = _run_loader(lambda db: refresh_forecast_snapshot(snapshot, db))
    assert report["matched"] == 5
    assert len(report["updated"]) == 5
    assert report["added"] == []
    assert report["stale"] == []
    from app.predictive_continuity.forecast import forecast_unavailability
    forecast = forecast_unavailability(report["snapshot"], ["p-1"], horizon_days=7, now=NOW)
    process = next(p for p in forecast["processes"] if p["id"] == "pr-1")
    live = {(i["kind"], i["id"]): i["status"] for i in process["live_work_items"]}
    assert live == {
        ("workflow_run", str(ids["wf_run_id"])): "running",
        ("workflow_run", str(ids["auto_run_id"])): "queued",
        ("invoice_batch", str(ids["batch_id"])): "awaiting_approval",
        ("approval", str(ids["approval_id"])): "pending",
        ("payment", str(ids["payment_id"])): "awaiting_approval",
    }


# --- scheduler -------------------------------------------------------------------

@pytest.fixture
def tenant():
    async def go(sm):
        async with sm() as db:
            row = GraphTenant(name="LiveWorkCo")
            db.add(row)
            await db.commit()
            admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
            return {"tenant_id": row.id, "admin_id": admin.id}

    return run(go)


def _scan_with_live_work(tenant_id, snapshot, items, **kwargs):
    async def go(sm):
        async with sm() as db:
            return await run_forecast_scan(
                db, tenant_id, snapshot, now=NOW,
                live_work_items=items, **kwargs)

    return run(go)


def _live_work_items():
    return _run_loader(lambda db: load_live_work(db))


def _record_for_root_horizon(tenant_id, root_id, horizon_days):
    async def go(sm):
        async with sm() as db:
            return await db.scalar(select(ForecastRecord).where(
                ForecastRecord.tenant_id == tenant_id,
                ForecastRecord.root_id == root_id,
                ForecastRecord.horizon_days == horizon_days,
                ForecastRecord.superseded.is_(False)))

    return run(go)


def test_run_scan_with_live_work_records_refreshed_statuses(tenant):
    ids = _seed_live_work()
    items = _live_work_items()
    summary = _scan_with_live_work(tenant["tenant_id"], _scan_snapshot(ids), items,
                                   created_by=tenant["admin_id"])
    assert summary["refreshed"] is True
    assert summary["live_work"]["updated"] == 5
    assert summary["live_work"]["matched"] == 5
    assert summary["live_work"]["changed"] is True
    assert "live-work updated=5" in format_scan_summary(summary)
    record = _record_for_root_horizon(tenant["tenant_id"], "p-1", 7)
    assert record is not None
    process = next(p for p in record.forecast["processes"] if p["id"] == "pr-1")
    live = {(i["kind"], i["id"]): i["status"] for i in process["live_work_items"]}
    assert live == {
        ("workflow_run", str(ids["wf_run_id"])): "running",
        ("workflow_run", str(ids["auto_run_id"])): "queued",
        ("invoice_batch", str(ids["batch_id"])): "awaiting_approval",
        ("approval", str(ids["approval_id"])): "pending",
        ("payment", str(ids["payment_id"])): "awaiting_approval",
    }


def test_run_scan_refreshes_when_only_live_work_changes(tenant):
    ids = _seed_live_work()
    snapshot = _scan_snapshot(ids)
    first = _scan_with_live_work(tenant["tenant_id"], snapshot, _live_work_items(),
                                 created_by=tenant["admin_id"])
    assert first["refreshed"] is True
    assert first["recorded_new"] == 12
    second = _scan_with_live_work(
        tenant["tenant_id"], snapshot, _live_work_items(),
        created_by=tenant["admin_id"],
        previous_fingerprint=first["fingerprint"], sent_at=first["sent_at"])
    assert second["refreshed"] is False
    assert second["recorded_new"] == 0
    # Only the live work changes: the run pauses. The same snapshot
    # file now refreshes again and supersedes the first records.
    async def pause(sm):
        async with sm() as db:
            row = await db.scalar(select(WorkflowRun).where(
                WorkflowRun.id == ids["wf_run_id"]))
            row.status = "paused"
            await db.commit()

    run(pause)
    third = _scan_with_live_work(
        tenant["tenant_id"], snapshot, _live_work_items(),
        created_by=tenant["admin_id"],
        previous_fingerprint=first["fingerprint"], sent_at=first["sent_at"])
    assert third["refreshed"] is True
    assert third["recorded_new"] == 12
    assert third["live_work"]["changed"] is True
    # The loader itself sees the paused run now.
    paused = next(i for i in _live_work_items()
                  if i["id"] == str(ids["wf_run_id"]))
    assert paused["status"] == "paused"
    record = _record_for_root_horizon(tenant["tenant_id"], "p-1", 7)
    process = next(p for p in record.forecast["processes"] if p["id"] == "pr-1")
    live = {(i["kind"], i["id"]): i["status"] for i in process["live_work_items"]}
    assert live[("workflow_run", str(ids["wf_run_id"]))] == "paused"


def test_run_scan_without_live_work_has_no_live_work_block(tenant):
    ids = _seed_live_work()
    summary = _scan_with_live_work(tenant["tenant_id"], _scan_snapshot(ids), None,
                                   created_by=tenant["admin_id"])
    assert summary["refreshed"] is True
    assert summary["live_work"] is None
    assert "live-work" not in format_scan_summary(summary)


# --- CLI -------------------------------------------------------------------------

def _cli(*argv, env=None):
    merged = dict(os.environ)
    merged["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    merged["TEST_DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *argv],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300, env=merged)


def test_cli_help_lists_live_work():
    result = _cli("forecast-scan", "--help")
    assert result.returncode == 0
    assert "--live-work" in result.stdout


def test_cli_dry_run_live_work_exits_2(tmp_path):
    snap = tmp_path / "twin.json"
    snap.write_text(json.dumps(_scan_snapshot(_seed_live_work())), encoding="utf-8")
    result = _cli("forecast-scan", "--tenant-id", str(uuid.uuid4()),
                  "--snapshot", str(snap), "--dry-run", "--live-work")
    assert result.returncode == 2
    assert "live-work" in result.stderr.lower()


def test_cli_live_work_real_run_records_refreshed_statuses(tenant, tmp_path):
    ids = _seed_live_work()
    snap = tmp_path / "twin.json"
    snap.write_text(json.dumps(_scan_snapshot(ids)), encoding="utf-8")
    state = tmp_path / "state.json"
    args = ["forecast-scan", "--tenant-id", str(tenant["tenant_id"]),
            "--snapshot", str(snap), "--state-file", str(state),
            "--created-by", str(tenant["admin_id"]), "--live-work"]
    first = _cli(*args)
    assert first.returncode == 0, first.stderr
    assert "refreshed=True" in first.stdout
    assert "live-work updated=5" in first.stdout
    assert "recorded new=12" in first.stdout
    assert load_state(state)["fingerprint"]
    second = _cli(*args)
    assert second.returncode == 0, second.stderr
    assert "refreshed=False" in second.stdout
    assert "recorded new=0" in second.stdout
    record = _record_for_root_horizon(tenant["tenant_id"], "p-1", 7)
    process = next(p for p in record.forecast["processes"] if p["id"] == "pr-1")
    live = {(i["kind"], i["id"]): i["status"] for i in process["live_work_items"]}
    assert live[("workflow_run", str(ids["wf_run_id"]))] == "running"
