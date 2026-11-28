"""Bank / payment connector for Step V wider integrations (slice 6).

Exposes ``app.finance.connectors`` through the same registry interface as
the other Step V connectors: ``verify`` + approval-gated ``bank.transfer``
+ read-back ``get`` + ``reconcile`` by idempotency key, and untrusted
inbound-event normalisation.

Safety — the same rules as the Gmail/ERP connectors, and the finance
connector contract in ``app/finance/connectors.py``:

- ``submit_transfer`` never executes without an approval whose digest
  covers the exact validated action (operation + payload + idempotency
  key) and whose expiry is still in the future. Without it, zero submits
  are recorded on the underlying payment connector.
- Money is ``Decimal`` only, never float. Amount must be > 0, currency a
  3-letter code, payee account supplied by the caller (the vendor master
  in the finance flow) — never taken from document text.
- After submit, the transfer is read back; a mismatch raises
  ``BankReconciliationError``. An outcome that may have accepted the
  transfer but lost the reply raises ``BankAmbiguousError`` and is resolved
  only by ``reconcile()``, never by replaying the submit. A
  ``ConnectorUnavailable`` from the payment connector means the transfer
  was definitely *not* accepted, so it is re-raised unchanged (safe to
  retry / fall back, exactly as the finance contract defines it).
- ``verify`` performs a read-only probe (``find`` with a probe key); it
  never submits anything. A probe failure is reported as
  ``reachable: False``, never raised: this is a status check.
- The connector never holds credentials itself: the HTTP bank URL/token
  live in settings / org settings and are read by ``get_connector``; the
  mock stays the default and no real bank is contacted unless an
  administrator configured the HTTP connector.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.core.errors import InvalidInputError
from app.finance.connectors import (
    ConnectorUnavailable,
    TransferRequest,
    get_connector,
)
from app.integrations import registry
from app.integrations.security import digest

_VERIFY_PROBE_KEY = "infraspine-verify-probe"

_CURRENCY_PATTERN = re.compile(r"^[A-Za-z]{3}$")
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:\-/]{1,128}$")


class ApprovalRequiredError(InvalidInputError):
    """Raised when a bank transfer has no live approval covering the exact action."""


class BankAmbiguousError(InvalidInputError):
    """Raised when a submit outcome is unknown (lost reply / failed
    read-back). The bank may or may not have accepted the transfer —
    reconcile by idempotency key first, never blindly replay."""


class BankReconciliationError(InvalidInputError):
    """Raised when the read-back disagrees with what was submitted.
    Needs a human decision."""


def _validate_transfer(amount, currency, payee_account, *, payee_name, reference, idempotency_key) -> dict:
    """Validate a transfer request. Returns the canonical payload dict
    (amount as a plain decimal string). Money never goes through float."""
    if isinstance(amount, bool) or isinstance(amount, float):
        raise InvalidInputError("Transfer amount must be a Decimal or decimal string, never float.")
    try:
        parsed = Decimal(amount)
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidInputError("Transfer amount is not a valid decimal.") from None
    if parsed <= 0:
        raise InvalidInputError("Transfer amount must be greater than zero.")
    if not isinstance(currency, str) or not _CURRENCY_PATTERN.fullmatch(currency):
        raise InvalidInputError("Currency must be a 3-letter code.")
    if not isinstance(payee_account, str) or not payee_account.strip() or len(payee_account) > 128:
        raise InvalidInputError("Payee account must be a non-empty string.")
    if not isinstance(payee_name, str) or len(payee_name) > 200:
        raise InvalidInputError("Payee name must be a string of at most 200 characters.")
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 200:
        raise InvalidInputError("Reference must be a non-empty string.")
    if not isinstance(idempotency_key, str) or not _KEY_PATTERN.fullmatch(idempotency_key):
        raise InvalidInputError("Idempotency key is invalid.")
    return {
        "amount": format(parsed, "f"),
        "currency": currency.upper(),
        "payee_account": payee_account.strip(),
        "payee_name": payee_name,
        "reference": reference.strip(),
    }


def _status_out(status) -> dict:
    """Reduce a finance TransferStatus to a plain dict."""
    return {
        "ref": status.ref,
        "status": status.status,
        "amount": format(status.amount, "f"),
        "currency": status.currency,
        "payee_account": status.payee_account,
        "reference": status.reference,
    }


class BankConnector:
    """Bank ``verify`` + approval-gated ``bank.transfer`` + read-back."""

    provider = "bank"

    def __init__(self, payment_connector):
        self.payment_connector = payment_connector

    @classmethod
    def from_settings(cls, settings=None) -> "BankConnector":
        """Wrap the connector in force right now (administrator's choice if
        set, else the environment). No real bank is contacted here."""
        return cls(get_connector(settings))

    @property
    def name(self) -> str:
        return self.payment_connector.name

    @property
    def is_mock(self) -> bool:
        """True when the active connector is the mock (same rule as
        ``finance.connectors.connector_status``)."""
        return "mock" in self.payment_connector.name.split("+")[:1]

    async def verify(self) -> dict:
        """Read-only probe: can the active connector answer a lookup?
        Never submits anything. A probe failure is reported, never raised."""
        reachable = True
        probe_error = None
        try:
            await self.payment_connector.find(_VERIFY_PROBE_KEY)
        except Exception as exc:  # status check: report, never raise
            reachable, probe_error = False, type(exc).__name__
        return {
            "ok": True,
            "connector": self.payment_connector.name,
            "is_mock": self.is_mock,
            "reachable": reachable,
            "probe_error": probe_error,
        }

    def _check_approval(self, payload: dict, idempotency_key: str, approval: object) -> None:
        """The approval must digest-cover this exact transfer and be unexpired."""
        expected = digest(
            {"operation": "bank.transfer", "payload": payload, "idempotency_key": idempotency_key}
        )
        if not isinstance(approval, dict):
            raise ApprovalRequiredError("Bank transfer needs an approval: approval_required.")
        if approval.get("digest") != expected:
            raise ApprovalRequiredError("Approval does not cover this exact action.")
        expires_at = approval.get("expires_at")
        if not isinstance(expires_at, datetime) or expires_at <= datetime.now(UTC):
            raise ApprovalRequiredError("Approval expired or missing expiry.")

    async def submit_transfer(
        self,
        amount,
        currency: str,
        payee_account: str,
        *,
        payee_name: str = "",
        reference: str,
        idempotency_key: str,
        approval: object = None,
    ) -> dict:
        """Validate, check the approval, submit once, then read back and
        verify the transfer before claiming success. Never auto-submits."""
        payload = _validate_transfer(
            amount, currency, payee_account,
            payee_name=payee_name, reference=reference, idempotency_key=idempotency_key,
        )
        self._check_approval(payload, idempotency_key, approval)
        request = TransferRequest(
            idempotency_key=idempotency_key,
            amount=Decimal(payload["amount"]),
            currency=payload["currency"],
            payee_account=payload["payee_account"],
            payee_name=payload["payee_name"],
            reference=payload["reference"],
        )
        try:
            ref = await self.payment_connector.submit(request)
        except ConnectorUnavailable:
            # Definitely not accepted (finance contract): safe to retry.
            raise
        except Exception as exc:
            # The bank may have accepted it and the reply was lost.
            raise BankAmbiguousError(
                "Bank transfer outcome unknown; reconcile by idempotency key."
            ) from exc
        return await self._read_back(ref, payload)

    async def _read_back(self, ref: str, payload: dict) -> dict:
        """Read the transfer back and verify amount/currency/payee match."""
        try:
            status = await self.payment_connector.get(ref)
        except ConnectorUnavailable as exc:
            raise BankAmbiguousError(
                "Bank transfer submitted but the read-back is unavailable; "
                "reconcile by idempotency key."
            ) from exc
        if (
            status.amount != Decimal(payload["amount"])
            or status.currency != payload["currency"]
            or status.payee_account != payload["payee_account"]
        ):
            raise BankReconciliationError(
                "Read-back disagrees with the submitted transfer; needs a human decision."
            )
        return {"ok": True, **_status_out(status)}

    async def get_transfer(self, ref: str) -> dict:
        """Read one transfer back from the bank (no approval needed: read-only)."""
        if not isinstance(ref, str) or not ref or len(ref) > 256:
            raise InvalidInputError("Invalid transfer reference.")
        return _status_out(await self.payment_connector.get(ref))

    async def reconcile(self, idempotency_key: str) -> dict:
        """Resolve an ambiguous submit by idempotency key. Returns
        ``found: False`` when nothing was submitted under the key; never
        replays the submit."""
        if not isinstance(idempotency_key, str) or not _KEY_PATTERN.fullmatch(idempotency_key):
            raise InvalidInputError("Idempotency key is invalid.")
        found = await self.payment_connector.find(idempotency_key)
        if found is None:
            return {"found": False, "idempotency_key": idempotency_key}
        return {"found": True, "idempotency_key": idempotency_key, **_status_out(found)}

    def receive_event(self, raw: dict) -> dict:
        """Normalise an inbound bank event (e.g. a statement webhook).
        Always untrusted."""
        return registry.normalize_inbound_event("bank", raw)

    @staticmethod
    def base_url() -> str:
        # No single base URL: the HTTP bank URL comes from settings when an
        # administrator configures the HTTP connector; the mock has none.
        return ""
