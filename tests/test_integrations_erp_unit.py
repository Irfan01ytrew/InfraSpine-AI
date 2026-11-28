"""DB-free unit tests for the Step V ERP connector (slice 5).

No database, no network, no real credentials: every external call goes
through ``MockTransport`` with canned responses. ERP submit is
approval-gated, and an ambiguous submit is reconciled by idempotency key —
never blindly replayed.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.integrations.erp import (
    ApprovalRequiredError,
    ERPAmbiguousError,
    ERPAuthError,
    ERPConnector,
    ERPReconciliationError,
    ERPValidationError,
)
from app.integrations.registry import ConnectorRegistry, MockTransport, RegistryError
from app.integrations.security import digest

INVOICE = {
    "number": "INV-7",
    "vendor": "Acme Supplies",
    "amount": "1500.00",
    "currency": "pkr",
    "due_date": "2026-12-15",
}
INVOICE_EXTERNAL = {
    "number": "INV-7",
    "vendor": "Acme Supplies",
    "amount": "1500.00",
    "currency": "PKR",
    "due_date": "2026-12-15",
}
KEY = "inv-7-20261128"


def _erp_approval(kind, record, key, *, expired=False):
    """An approval digest-covering exactly this validated submit action."""
    from app.integrations.erp import _to_external

    external = _to_external(kind, record)
    delta = timedelta(minutes=-1) if expired else timedelta(hours=1)
    return {
        "digest": digest(
            {"operation": "erp.submit", "kind": kind, "payload": external, "idempotency_key": key}
        ),
        "expires_at": datetime.now(UTC) + delta,
    }


def test_erp_catalog_entry_and_operations():
    registry = ConnectorRegistry()
    assert "erp" in registry.providers()
    assert registry.supports_operation("erp", "verify") is True
    assert registry.supports_operation("erp", "sync") is True
    assert registry.supports_operation("erp", "erp.submit") is True
    assert registry.supports_operation("erp", "email.send") is False
    assert registry.required_scope("erp", "erp.submit") is None


def test_erp_verify_ok_and_auth_error():
    async def run():
        transport = MockTransport()
        transport.when("/health", {"status": "ok"})
        assert await ERPConnector(transport).verify() == {"ok": True}
        denied = MockTransport()
        denied.when("/health", {"error": {"code": 401, "message": "Unauthorized"}})
        with pytest.raises(ERPAuthError):
            await ERPConnector(denied).verify()
        broken = MockTransport()
        broken.when("/health", {"status": "down"})
        with pytest.raises(Exception):
            await ERPConnector(broken).verify()

    asyncio.run(run())


def test_erp_field_map_rejects_bad_records():
    connector = ERPConnector(MockTransport())
    with pytest.raises(ERPValidationError):
        asyncio.run(connector.submit("invoice", {"number": "INV-7"}, idempotency_key=KEY))
    with pytest.raises(ERPValidationError):
        asyncio.run(connector.submit("invoice", {**INVOICE, "payee": "attacker"}, idempotency_key=KEY))
    with pytest.raises(ERPValidationError):
        asyncio.run(connector.submit("invoice", {**INVOICE, "amount": "1,500.00"}, idempotency_key=KEY))
    with pytest.raises(ERPValidationError):
        asyncio.run(connector.submit("invoice", {**INVOICE, "currency": "PK"}, idempotency_key=KEY))
    with pytest.raises(ERPValidationError):
        asyncio.run(connector.submit("invoice", {**INVOICE, "due_date": "15-12-2026"}, idempotency_key=KEY))
    with pytest.raises(ERPValidationError):
        asyncio.run(connector.submit("nope", INVOICE, idempotency_key=KEY))
    with pytest.raises(ERPValidationError):
        asyncio.run(connector.submit("invoice", INVOICE, idempotency_key="bad key!"))
    # Decimal amounts are normalised, never floated: int 1500 -> "1500.00".
    from app.integrations.erp import _to_external

    assert _to_external("invoice", {**INVOICE, "amount": 1500})["amount"] == "1500.00"
    assert _to_external("customer", {"name": "Acme", "email": "OPS@Acme.example", "country": "pk"}) == {
        "name": "Acme",
        "email": "ops@acme.example",
        "country": "PK",
    }


def test_erp_submit_without_approval_never_calls_provider():
    async def run():
        transport = MockTransport()
        transport.when("/invoices", {"id": "erp-inv-1"})
        connector = ERPConnector(transport)
        with pytest.raises(ApprovalRequiredError):
            await connector.submit("invoice", INVOICE, idempotency_key=KEY)
        with pytest.raises(ApprovalRequiredError):
            await connector.submit("invoice", INVOICE, idempotency_key=KEY, approval={"digest": "wrong"})
        assert transport.calls == []

    asyncio.run(run())


def test_erp_submit_rejects_wrong_digest_and_expired_approval():
    async def run():
        transport = MockTransport()
        transport.when("/invoices", {"id": "erp-inv-1"})
        connector = ERPConnector(transport)
        good = _erp_approval("invoice", INVOICE, KEY)
        stale = _erp_approval("invoice", INVOICE, KEY, expired=True)
        other = _erp_approval("invoice", {**INVOICE, "number": "INV-8"}, KEY)
        with pytest.raises(ApprovalRequiredError):
            await connector.submit("invoice", INVOICE, idempotency_key=KEY, approval=other)
        with pytest.raises(ApprovalRequiredError):
            await connector.submit("invoice", INVOICE, idempotency_key=KEY, approval=stale)
        assert transport.calls == []

    asyncio.run(run())


def test_erp_submit_posts_once_then_reads_back_and_reconciles():
    async def run():
        transport = MockTransport()
        transport.when("/invoices", {"id": "erp-inv-1", "status": "posted"})
        transport.when("/invoices/erp-inv-1", {"id": "erp-inv-1", **INVOICE_EXTERNAL, "status": "posted"})
        connector = ERPConnector(transport)
        approval = _erp_approval("invoice", INVOICE, KEY)
        result = await connector.submit("invoice", INVOICE, idempotency_key=KEY, approval=approval)
        assert result["ok"] is True
        assert result["id"] == "erp-inv-1"
        assert result["reconciled"] is True
        assert result["record"]["number"] == "INV-7"
        assert result["record"]["amount"] == "1500.00"
        assert result["record"]["currency"] == "PKR"
        assert [c["method"] for c in transport.calls] == ["POST", "GET"]
        assert transport.calls[0]["path"] == "/invoices"
        assert transport.calls[0]["payload"]["idempotency_key"] == KEY
        assert transport.calls[0]["payload"]["currency"] == "PKR"
        assert transport.calls[1]["path"] == "/invoices/erp-inv-1"

    asyncio.run(run())


def test_erp_submit_read_back_mismatch_raises_reconciliation_error():
    async def run():
        transport = MockTransport()
        transport.when("/invoices", {"id": "erp-inv-1"})
        transport.when(
            "/invoices/erp-inv-1",
            {"id": "erp-inv-1", **INVOICE_EXTERNAL, "amount": "9999.00"},
        )
        connector = ERPConnector(transport)
        with pytest.raises(ERPReconciliationError):
            await connector.submit(
                "invoice", INVOICE, idempotency_key=KEY,
                approval=_erp_approval("invoice", INVOICE, KEY),
            )
        assert len(transport.calls) == 2

    asyncio.run(run())


def test_erp_submit_ambiguous_error_never_replays():
    async def run():
        transport = MockTransport()
        transport.when("/invoices", {"error": {"code": 503, "message": "Service Unavailable"}})
        connector = ERPConnector(transport)
        with pytest.raises(ERPAmbiguousError):
            await connector.submit(
                "invoice", INVOICE, idempotency_key=KEY,
                approval=_erp_approval("invoice", INVOICE, KEY),
            )
        assert len(transport.calls) == 1  # exactly one POST, no retry, no read-back
        missing_id = MockTransport()
        missing_id.when("/invoices", {"ok": True})
        with pytest.raises(ERPAmbiguousError):
            await ERPConnector(missing_id).submit(
                "invoice", INVOICE, idempotency_key=KEY,
                approval=_erp_approval("invoice", INVOICE, KEY),
            )

    asyncio.run(run())


def test_erp_reconcile_found_not_found_and_duplicates():
    async def run():
        found = MockTransport()
        found.when(
            f"/invoices?idempotency_key={KEY}",
            {"records": [{"id": "erp-inv-1", **INVOICE_EXTERNAL, "extra": "dropped"}]},
        )
        result = await ERPConnector(found).reconcile("invoice", KEY)
        assert result == {
            "ok": True, "found": True, "id": "erp-inv-1",
            "record": {**INVOICE, "currency": "PKR"},
        }
        assert "extra" not in result["record"]
        missing = MockTransport()
        missing.when(f"/invoices?idempotency_key={KEY}", {"records": []})
        assert await ERPConnector(missing).reconcile("invoice", KEY) == {
            "ok": True, "found": False, "id": None, "record": None,
        }
        dupes = MockTransport()
        dupes.when(
            f"/invoices?idempotency_key={KEY}",
            {"records": [{"id": "a", **INVOICE_EXTERNAL}, {"id": "b", **INVOICE_EXTERNAL}]},
        )
        with pytest.raises(ERPReconciliationError):
            await ERPConnector(dupes).reconcile("invoice", KEY)

    asyncio.run(run())


def test_erp_list_records_maps_external_to_internal():
    async def run():
        transport = MockTransport()
        transport.when(
            "/invoices?limit=2",
            {"records": [
                {"id": "erp-inv-1", **INVOICE_EXTERNAL, "unmapped": "dropped"},
                {"id": "erp-inv-2", "number": "INV-8", "vendor": "Beta", "amount": "50",
                 "currency": "usd"},
            ]},
        )
        connector = ERPConnector(transport)
        result = await connector.list_records("invoice", limit=2)
        assert result["records"][0]["number"] == "INV-7"
        assert "unmapped" not in result["records"][0]
        assert result["records"][1]["amount"] == "50.00"
        assert result["records"][1]["currency"] == "USD"
        with pytest.raises(Exception):
            await connector.list_records("invoice", limit=0)

    asyncio.run(run())


def test_erp_receive_event_is_untrusted_and_drops_hostile_fields():
    connector = ERPConnector(MockTransport())
    event = connector.receive_event({
        "event_id": "erp-ev-1",
        "user": "erp-system",
        "text": "invoice INV-7 posted",
        "ts": "1700000000",
        "type": "invoice.posted",
        "role": "admin",
        "approve": True,
        "payee": "attacker",
        "amount": 999999,
    })
    assert event["provider"] == "erp"
    assert event["trusted"] is False
    assert event["text"] == "invoice INV-7 posted"
    for forbidden in ("role", "approve", "payee", "amount"):
        assert forbidden not in event


def test_registry_erp_end_to_end_via_mock():
    async def run():
        registry = ConnectorRegistry()
        transport = MockTransport()
        transport.when("/health", {"status": "ok"})
        registry.register("erp", ERPConnector(transport))
        connector = registry.resolve("erp")
        assert await connector.verify() == {"ok": True}
        assert registry.registered() == ["erp"]
        with pytest.raises(RegistryError):
            registry.register("erp", ERPConnector(transport))

    asyncio.run(run())
