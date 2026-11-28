"""DB-free unit tests for the Step V bank connector (slice 6).

No database, no network, no real bank: every submit goes through the
in-process ``MockPaymentConnector`` (deterministic, idempotent, records
submits). A bank transfer is approval-gated, read back after submit, and
an ambiguous outcome is reconciled by idempotency key — never replayed.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.errors import InvalidInputError
from app.finance.connectors import ConnectorUnavailable, MockPaymentConnector
from app.integrations.bank import (
    ApprovalRequiredError,
    BankAmbiguousError,
    BankConnector,
    BankReconciliationError,
)
from app.integrations.registry import ConnectorRegistry
from app.integrations.security import digest

AMOUNT = Decimal("1500.00")
KEY = "pay-20261128-001"


def _bank_approval(*, amount=AMOUNT, currency="PKR", payee="PK00BANK0001", expired=False):
    """An approval digest-covering exactly this validated transfer action."""
    payload = {
        "amount": format(amount, "f"),
        "currency": currency,
        "payee_account": payee,
        "payee_name": "Acme Supplies",
        "reference": "INV-7",
    }
    delta = timedelta(minutes=-1) if expired else timedelta(hours=1)
    return {
        "digest": digest(
            {"operation": "bank.transfer", "payload": payload, "idempotency_key": KEY}
        ),
        "expires_at": datetime.now(UTC) + delta,
    }


def _run(coro):
    return asyncio.run(coro)


def test_bank_catalog_entry_and_operations():
    registry = ConnectorRegistry()
    assert "bank" in registry.providers()
    assert registry.supports_operation("bank", "verify") is True
    assert registry.supports_operation("bank", "bank.transfer") is True
    assert registry.supports_operation("bank", "email.send") is False
    # Bank transfers are gated by exact-action approval, not a provider scope.
    assert registry.required_scope("bank", "bank.transfer") is None


def test_bank_registry_register_resolve_round_trip():
    registry = ConnectorRegistry()
    connector = BankConnector(MockPaymentConnector())
    registry.register("bank", connector)
    assert registry.resolve("bank") is connector
    assert registry.registered() == ["bank"]


def test_bank_verify_reports_mock_connector():
    connector = BankConnector(MockPaymentConnector())
    result = _run(connector.verify())
    assert result == {
        "ok": True,
        "connector": "mock",
        "is_mock": True,
        "reachable": True,
        "probe_error": None,
    }


class _DeadBank:
    """A payment connector that cannot be reached at all."""

    name = "http"

    async def submit(self, req):
        raise ConnectorUnavailable("bank unreachable")

    async def get(self, ref):
        raise ConnectorUnavailable("bank unreachable")

    async def find(self, key):
        raise ConnectorUnavailable("bank unreachable")


def test_bank_verify_reports_unreachable_instead_of_raising():
    connector = BankConnector(_DeadBank())
    result = _run(connector.verify())
    assert result["ok"] is True
    assert result["connector"] == "http"
    assert result["is_mock"] is False
    assert result["reachable"] is False
    assert result["probe_error"] == "ConnectorUnavailable"


def test_bank_submit_without_approval_records_zero_submits():
    bank = MockPaymentConnector()
    connector = BankConnector(bank)
    with pytest.raises(ApprovalRequiredError):
        _run(connector.submit_transfer(
            AMOUNT, "PKR", "PK00BANK0001", payee_name="Acme Supplies",
            reference="INV-7", idempotency_key=KEY, approval=None,
        ))
    assert bank.submit_calls == 0


def test_bank_submit_with_wrong_digest_records_zero_submits():
    bank = MockPaymentConnector()
    connector = BankConnector(bank)
    approval = _bank_approval(amount=Decimal("999.00"))
    with pytest.raises(ApprovalRequiredError):
        _run(connector.submit_transfer(
            AMOUNT, "PKR", "PK00BANK0001", payee_name="Acme Supplies",
            reference="INV-7", idempotency_key=KEY, approval=approval,
        ))
    assert bank.submit_calls == 0


def test_bank_submit_with_expired_approval_records_zero_submits():
    bank = MockPaymentConnector()
    connector = BankConnector(bank)
    approval = _bank_approval(expired=True)
    with pytest.raises(ApprovalRequiredError):
        _run(connector.submit_transfer(
            AMOUNT, "PKR", "PK00BANK0001", payee_name="Acme Supplies",
            reference="INV-7", idempotency_key=KEY, approval=approval,
        ))
    assert bank.submit_calls == 0


def test_bank_submit_with_valid_approval_reads_back():
    bank = MockPaymentConnector()
    connector = BankConnector(bank)
    result = _run(connector.submit_transfer(
        AMOUNT, "pkr", "PK00BANK0001", payee_name="Acme Supplies",
        reference="INV-7", idempotency_key=KEY, approval=_bank_approval(),
    ))
    assert result["ok"] is True
    assert result["ref"].startswith("MOCK-")
    assert result["status"] == "completed"
    assert result["amount"] == "1500.00"
    assert result["currency"] == "PKR"
    assert result["payee_account"] == "PK00BANK0001"
    assert bank.submit_calls == 1


def test_bank_submit_same_key_twice_is_one_transfer():
    bank = MockPaymentConnector()
    connector = BankConnector(bank)
    first = _run(connector.submit_transfer(
        AMOUNT, "PKR", "PK00BANK0001", payee_name="Acme Supplies",
        reference="INV-7", idempotency_key=KEY, approval=_bank_approval(),
    ))
    second = _run(connector.submit_transfer(
        AMOUNT, "PKR", "PK00BANK0001", payee_name="Acme Supplies",
        reference="INV-7", idempotency_key=KEY, approval=_bank_approval(),
    ))
    assert second["ref"] == first["ref"]
    assert bank.submit_calls == 2  # the mock is called again…
    assert len(bank.transfers) == 1  # …but records exactly one transfer


def test_bank_tampered_read_back_raises_reconciliation_error():
    bank = MockPaymentConnector()
    bank.tamper_amount = Decimal("1.00")
    connector = BankConnector(bank)
    with pytest.raises(BankReconciliationError):
        _run(connector.submit_transfer(
            AMOUNT, "PKR", "PK00BANK0001", payee_name="Acme Supplies",
            reference="INV-7", idempotency_key=KEY, approval=_bank_approval(),
        ))


def test_bank_lost_reply_is_ambiguous_and_reconciled_without_replay():
    bank = MockPaymentConnector()
    bank.accept_then_fail = RuntimeError("reply lost after acceptance")
    connector = BankConnector(bank)
    with pytest.raises(BankAmbiguousError):
        _run(connector.submit_transfer(
            AMOUNT, "PKR", "PK00BANK0001", payee_name="Acme Supplies",
            reference="INV-7", idempotency_key=KEY, approval=_bank_approval(),
        ))
    # The bank did accept it: reconcile finds it without a second submit.
    found = _run(connector.reconcile(KEY))
    assert found["found"] is True
    assert found["status"] == "completed"
    assert found["amount"] == "1500.00"
    assert bank.submit_calls == 1


def test_bank_reconcile_unknown_key_is_not_found():
    connector = BankConnector(MockPaymentConnector())
    assert _run(connector.reconcile("never-submitted")) == {
        "found": False,
        "idempotency_key": "never-submitted",
    }


def test_bank_unavailable_before_acceptance_is_not_ambiguous():
    bank = MockPaymentConnector()
    bank.fail_next_submit = ConnectorUnavailable("bank down")
    connector = BankConnector(bank)
    with pytest.raises(ConnectorUnavailable):
        _run(connector.submit_transfer(
            AMOUNT, "PKR", "PK00BANK0001", payee_name="Acme Supplies",
            reference="INV-7", idempotency_key=KEY, approval=_bank_approval(),
        ))
    # Nothing was accepted: reconcile finds nothing, and a retry may proceed.
    assert _run(connector.reconcile(KEY))["found"] is False


@pytest.mark.parametrize(
    "amount,currency,payee",
    [
        (Decimal("0"), "PKR", "PK00BANK0001"),
        (Decimal("-5"), "PKR", "PK00BANK0001"),
        (1500.00, "PKR", "PK00BANK0001"),  # float money is banned
        ("not-a-number", "PKR", "PK00BANK0001"),
        (AMOUNT, "PK", "PK00BANK0001"),
        (AMOUNT, "PKR", ""),
        (AMOUNT, "PKR", "   "),
    ],
)
def test_bank_invalid_transfer_inputs_rejected_before_any_submit(amount, currency, payee):
    bank = MockPaymentConnector()
    connector = BankConnector(bank)
    with pytest.raises(InvalidInputError):
        _run(connector.submit_transfer(
            amount, currency, payee, payee_name="Acme Supplies",
            reference="INV-7", idempotency_key=KEY, approval=_bank_approval(),
        ))
    assert bank.submit_calls == 0


def test_bank_get_transfer_reads_back():
    bank = MockPaymentConnector()
    connector = BankConnector(bank)
    submitted = _run(connector.submit_transfer(
        AMOUNT, "PKR", "PK00BANK0001", payee_name="Acme Supplies",
        reference="INV-7", idempotency_key=KEY, approval=_bank_approval(),
    ))
    read = _run(connector.get_transfer(submitted["ref"]))
    assert read["ref"] == submitted["ref"]
    assert read["amount"] == "1500.00"
    assert read["payee_account"] == "PK00BANK0001"


def test_bank_receive_event_is_untrusted_and_drops_hostile_fields():
    connector = BankConnector(MockPaymentConnector())
    event = connector.receive_event({
        "event_id": "evt-1",
        "channel": "statements",
        "text": "transfer received",
        "role": "admin",
        "approve": True,
        "payee": "PK99ATTACKER",
        "amount": "999999",
    })
    assert event["provider"] == "bank"
    assert event["trusted"] is False
    assert event["text"] == "transfer received"
    for forbidden in ("role", "approve", "payee", "amount"):
        assert forbidden not in event


def test_bank_end_to_end_through_registry():
    registry = ConnectorRegistry()
    bank = MockPaymentConnector()
    registry.register("bank", BankConnector(bank))
    connector = registry.resolve("bank")
    assert _run(connector.verify())["reachable"] is True
    result = _run(connector.submit_transfer(
        AMOUNT, "PKR", "PK00BANK0001", payee_name="Acme Supplies",
        reference="INV-7", idempotency_key=KEY, approval=_bank_approval(),
    ))
    assert result["ok"] is True
    assert bank.submit_calls == 1
