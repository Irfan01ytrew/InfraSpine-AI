"""ERP connector for Step V wider integrations (mock-transport based).

REST-based, with a declarative field map per record kind (invoice / order /
customer), idempotency keys, and submit → read-back reconciliation — mirroring
the bank-connector rule that an ambiguous submit is reconciled, never blindly
replayed. Runs on an injected transport, so tests and this slice never touch
a real ERP. **Submit is approval-gated**: ``submit`` refuses to call the
provider unless it is given an approval whose digest covers the exact
validated action (kind + mapped payload + idempotency key) and whose expiry
is still in the future — the same rule as the Gmail connector. Inbound
events are normalised with ``registry.normalize_inbound_event`` and are
always untrusted. The ERP endpoint / API key belongs to the owner and is
never stored here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.core.errors import InvalidInputError
from app.integrations import registry
from app.integrations.security import digest

# Declarative field map, per record kind. Key = ERP (external) field name;
# value = (InfraSpine (internal) field name, transform, required).
# The same table drives both directions: submit maps internal -> external,
# list/reconcile maps external -> internal. Fields outside the map are
# rejected on submit and dropped on read — external content is untrusted.
ERP_FIELD_MAPS: dict[str, dict[str, tuple[str, str, bool]]] = {
    "invoice": {
        "number": ("number", "string", True),
        "vendor": ("vendor", "string", True),
        "amount": ("amount", "decimal", True),
        "currency": ("currency", "currency", True),
        "due_date": ("due_date", "date", False),
        "status": ("status", "lowercase", False),
    },
    "order": {
        "number": ("number", "string", True),
        "customer": ("customer", "string", True),
        "amount": ("amount", "decimal", True),
        "currency": ("currency", "currency", True),
        "status": ("status", "lowercase", False),
    },
    "customer": {
        "name": ("name", "string", True),
        "email": ("email", "email", False),
        "tax_id": ("tax_id", "string", False),
        "country": ("country", "country", False),
    },
}

_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,200}$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_COUNTRY_PATTERN = re.compile(r"^[A-Z]{2}$")


class ERPValidationError(InvalidInputError):
    """Raised when a record fails the declarative field map."""


class ERPAuthError(InvalidInputError):
    """Raised when the ERP rejects the credentials (401)."""


class ERPAmbiguousError(InvalidInputError):
    """Raised when a submit outcome is unknown (5xx / no record id / failed
    read-back). The ERP may or may not have recorded the submit — reconcile
    by idempotency key first, never blindly replay."""


class ERPReconciliationError(InvalidInputError):
    """Raised when the read-back disagrees with what was submitted, or one
    idempotency key maps to several records. Needs a human decision."""


class ERPError(InvalidInputError):
    """Raised for other ERP provider rejections (4xx)."""


def _error_from(result: object):
    """Map an ERP error envelope to a typed exception, or None."""
    if not isinstance(result, dict) or not isinstance(result.get("error"), dict):
        return None
    error = result["error"]
    code = error.get("code")
    if code == 401:
        return ERPAuthError("ERP credentials rejected.")
    if code in (500, 502, 503, 504):
        return ERPAmbiguousError("ERP submit outcome unknown; reconcile by idempotency key.")
    if isinstance(code, int) and 400 <= code < 500:
        return ERPError("ERP request rejected.")
    return ERPError("ERP request failed.")


def _apply_transform(field: str, transform: str, value: object):
    """Validate + convert one field value. Money never goes through float."""
    try:
        if transform == "string":
            if not isinstance(value, str) or not value or len(value) > 500 or any(ord(c) < 32 for c in value):
                raise ValueError
            return value
        if transform == "integer":
            if isinstance(value, bool):
                raise ValueError
            if isinstance(value, int):
                return value
            if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
                return int(value)
            raise ValueError
        if transform == "decimal":
            if isinstance(value, bool):
                raise ValueError
            amount = Decimal(str(value))
            return str(amount.quantize(Decimal("0.01")))
        if transform == "lowercase":
            if not isinstance(value, str) or not value:
                raise ValueError
            return value.lower()
        if transform == "uppercase":
            if not isinstance(value, str) or not value:
                raise ValueError
            return value.upper()
        if transform == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "false", "1", "0"):
                return value.lower() in ("true", "1")
            raise ValueError
        if transform == "date":
            if not isinstance(value, str):
                raise ValueError
            datetime.strptime(value, "%Y-%m-%d")
            return value
        if transform == "currency":
            if not isinstance(value, str):
                raise ValueError
            upper = value.upper()
            if not _CURRENCY_PATTERN.fullmatch(upper):
                raise ValueError
            return upper
        if transform == "country":
            if not isinstance(value, str):
                raise ValueError
            upper = value.upper()
            if not _COUNTRY_PATTERN.fullmatch(upper):
                raise ValueError
            return upper
        if transform == "email":
            if not isinstance(value, str) or len(value) > 254 or not _EMAIL_PATTERN.fullmatch(value):
                raise ValueError
            return value.lower()
    except (ValueError, InvalidOperation, ArithmeticError):
        raise ERPValidationError(f"Invalid value for field '{field}'.") from None
    raise ERPValidationError(f"Unknown transform '{transform}' for field '{field}'.")


def _field_map(kind: str) -> dict[str, tuple[str, str, bool]]:
    try:
        return ERP_FIELD_MAPS[kind]
    except KeyError:
        raise ERPValidationError(f"Unknown ERP record kind: {kind}") from None


def _to_external(kind: str, record: dict) -> dict:
    """Map an InfraSpine record to the ERP payload. Strict: unknown fields
    are rejected, required fields must be present."""
    if not isinstance(record, dict):
        raise ERPValidationError("ERP record must be a JSON object.")
    field_map = _field_map(kind)
    internal_to_external = {
        internal: (external, transform, required)
        for external, (internal, transform, required) in field_map.items()
    }
    unknown = sorted(set(record) - set(internal_to_external))
    if unknown:
        raise ERPValidationError(f"Unknown fields for {kind}: {', '.join(unknown)}.")
    external: dict = {}
    for internal, (external_name, transform, required) in internal_to_external.items():
        if internal not in record:
            if required:
                raise ERPValidationError(f"Missing required field '{internal}' for {kind}.")
            continue
        external[external_name] = _apply_transform(internal, transform, record[internal])
    return external


def _to_internal(kind: str, row: dict) -> dict:
    """Map an ERP row to an InfraSpine record. Fields outside the map are
    dropped; mapped fields that fail validation raise."""
    if not isinstance(row, dict):
        raise ERPValidationError("ERP record must be a JSON object.")
    field_map = _field_map(kind)
    internal: dict = {}
    for external_name, value in row.items():
        mapping = field_map.get(external_name)
        if mapping is None:
            continue
        internal_name, transform, _ = mapping
        internal[internal_name] = _apply_transform(internal_name, transform, value)
    return internal


def _same_value(sent: object, echoed: object) -> bool:
    """Compare a submitted value with its read-back echo. Numeric values
    compare by decimal value, so '1500.00' and 1500.0 agree; anything else
    must match exactly."""
    if sent == echoed:
        return True
    try:
        return Decimal(str(sent)) == Decimal(str(echoed))
    except (InvalidOperation, ValueError, ArithmeticError):
        return False


class ApprovalRequiredError(InvalidInputError):
    """Raised when an ERP submit has no live approval covering the exact action."""


class ERPConnector:
    """ERP ``verify`` + ``sync`` + approval-gated ``erp.submit`` + reconcile."""

    provider = "erp"

    def __init__(self, transport, *, base_url: str = "", token: str = "mock-token"):
        self.transport = transport
        self._base_url = base_url
        self.token = token

    async def verify(self) -> dict:
        """Call ``/health``; returns ok when the ERP answers healthy."""
        result = await self.transport.get("/health")
        error = _error_from(result)
        if error is not None:
            raise error
        if not isinstance(result, dict) or (result.get("status") != "ok" and result.get("ok") is not True):
            raise ERPError("ERP verification failed.")
        return {"ok": True}

    async def list_records(self, kind: str, *, limit: int = 10) -> dict:
        """List records of one kind, mapped external -> internal."""
        _field_map(kind)
        if not 1 <= limit <= 100:
            raise ERPValidationError("limit must be between 1 and 100.")
        result = await self.transport.get(f"/{kind}s?limit={limit}")
        error = _error_from(result)
        if error is not None:
            raise error
        if not isinstance(result, dict) or not isinstance(result.get("records"), list):
            raise ERPError("ERP list failed.")
        return {"records": [_to_internal(kind, row) for row in result["records"]]}

    def _check_approval(self, kind: str, external: dict, idempotency_key: str, approval: object) -> None:
        """The approval must digest-cover this exact submit and be unexpired."""
        expected = digest(
            {"operation": "erp.submit", "kind": kind, "payload": external, "idempotency_key": idempotency_key}
        )
        if not isinstance(approval, dict):
            raise ApprovalRequiredError("ERP submit needs an approval: approval_required.")
        if approval.get("digest") != expected:
            raise ApprovalRequiredError("Approval does not cover this exact action.")
        expires_at = approval.get("expires_at")
        if not isinstance(expires_at, datetime) or expires_at <= datetime.now(UTC):
            raise ApprovalRequiredError("Approval expired or missing expiry.")

    async def submit(
        self, kind: str, record: dict, *, idempotency_key: str, approval: object = None
    ) -> dict:
        """Validate + map the record, check the approval, submit once, then
        read the record back and compare. A mismatch raises instead of
        silently accepting a different record; an ambiguous submit raises
        ``ERPAmbiguousError`` — reconcile, never replay."""
        _field_map(kind)
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_PATTERN.fullmatch(idempotency_key):
            raise ERPValidationError("idempotency_key must be 1-200 safe characters.")
        external = _to_external(kind, record)
        self._check_approval(kind, external, idempotency_key, approval)
        payload = {"idempotency_key": idempotency_key, **external}
        result = await self.transport.post(f"/{kind}s", payload)
        error = _error_from(result)
        if error is not None:
            raise error
        if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
            raise ERPAmbiguousError("ERP submit returned no record id; reconcile by idempotency key.")
        record_id = result["id"]
        read_back = await self.transport.get(f"/{kind}s/{record_id}")
        read_error = _error_from(read_back)
        if read_error is not None:
            raise ERPAmbiguousError("ERP read-back failed after submit; reconcile by idempotency key.")
        if not isinstance(read_back, dict):
            raise ERPAmbiguousError("ERP read-back failed after submit; reconcile by idempotency key.")
        for field, sent in external.items():
            if field not in read_back or not _same_value(sent, read_back[field]):
                raise ERPReconciliationError(
                    f"ERP read-back disagrees on field '{field}'; needs a human decision."
                )
        return {
            "ok": True,
            "id": record_id,
            "reconciled": True,
            "record": _to_internal(kind, read_back),
        }

    async def reconcile(self, kind: str, idempotency_key: str) -> dict:
        """Find what a (possibly ambiguous) submit actually did, by
        idempotency key. ``found: False`` means the submit truly never
        landed — the caller decides the next step; this method never
        replays the submit itself. Several records for one key is a
        reconciliation error."""
        _field_map(kind)
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_PATTERN.fullmatch(idempotency_key):
            raise ERPValidationError("idempotency_key must be 1-200 safe characters.")
        result = await self.transport.get(f"/{kind}s?idempotency_key={idempotency_key}")
        error = _error_from(result)
        if error is not None:
            raise error
        if not isinstance(result, dict) or not isinstance(result.get("records"), list):
            raise ERPError("ERP reconcile failed.")
        records = result["records"]
        if not records:
            return {"ok": True, "found": False, "id": None, "record": None}
        if len(records) > 1:
            raise ERPReconciliationError(
                "Several ERP records share one idempotency key; needs a human decision."
            )
        row = records[0]
        record_id = row.get("id") if isinstance(row, dict) else None
        return {
            "ok": True,
            "found": True,
            "id": record_id,
            "record": _to_internal(kind, row),
        }

    def receive_event(self, raw: dict) -> dict:
        """Normalise an inbound ERP event. Always untrusted."""
        return registry.normalize_inbound_event("erp", raw)

    def base_url(self) -> str:
        """The ERP endpoint is owner-supplied per deployment (empty here)."""
        return self._base_url
