"""Payment connector interface with a test connector, an HTTP bank connector and failover.

Contract (every connector):
* `submit(req)` is idempotent on `req.idempotency_key`: submitting the same key twice returns the same transfer.
* `get(ref)` reads the transfer back from the bank/ERP. A payment is only "done" when the read-back shows the
  approved amount, currency and payee account (`execute_once` verify step).
* `find(idempotency_key)` reconciles an ambiguous submit (e.g. a timeout after the bank accepted it).

Connector selection: FINANCE_PAYMENT_CONNECTOR = mock | http, FINANCE_PAYMENT_FALLBACK = none | mock | http.
The mock connector is for tests and demos; its references start with "MOCK-" and the UI labels it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class ConnectorUnavailable(Exception):
    """The connector could not be reached *before* it accepted anything (safe to try the fallback)."""


@dataclass(frozen=True)
class TransferRequest:
    idempotency_key: str
    amount: Decimal
    currency: str
    payee_account: str
    payee_name: str
    reference: str


@dataclass(frozen=True)
class TransferStatus:
    ref: str
    status: str  # accepted | completed | failed | unknown
    amount: Decimal
    currency: str
    payee_account: str
    reference: str


class PaymentConnector(Protocol):
    name: str

    async def submit(self, req: TransferRequest) -> str: ...

    async def get(self, ref: str) -> TransferStatus: ...

    async def find(self, idempotency_key: str) -> TransferStatus | None: ...


class MockPaymentConnector:
    """In-process test bank. Deterministic, idempotent, records every submit for assertions."""

    name = 'mock'

    def __init__(self) -> None:
        self.transfers: dict[str, TransferStatus] = {}
        self.by_key: dict[str, str] = {}
        self.submit_calls = 0
        self.fail_next_submit: Exception | None = None
        self.accept_then_fail: Exception | None = None  # bank accepts, the response is lost
        self.fail_reads = 0
        self.tamper_amount: Decimal | None = None

    def reset(self) -> None:
        self.__init__()

    async def submit(self, req: TransferRequest) -> str:
        self.submit_calls += 1
        if self.fail_next_submit is not None:
            exc, self.fail_next_submit = self.fail_next_submit, None
            raise exc
        ref = self.by_key.get(req.idempotency_key)
        if ref is None:
            ref = f'MOCK-{uuid.uuid4().hex[:16].upper()}'
            amount = self.tamper_amount if self.tamper_amount is not None else req.amount
            self.transfers[ref] = TransferStatus(ref, 'completed', amount, req.currency, req.payee_account, req.reference)
            self.by_key[req.idempotency_key] = ref
        if self.accept_then_fail is not None:
            exc, self.accept_then_fail = self.accept_then_fail, None
            raise exc
        return ref

    async def get(self, ref: str) -> TransferStatus:
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise ConnectorUnavailable('read-back unavailable')
        status = self.transfers.get(ref)
        if status is None:
            return TransferStatus(ref, 'unknown', Decimal(0), '', '', '')
        return status

    async def find(self, idempotency_key: str) -> TransferStatus | None:
        ref = self.by_key.get(idempotency_key)
        return self.transfers.get(ref) if ref else None


class HttpPaymentConnector:
    """Generic bank/ERP REST adapter: POST {base}/transfers (Idempotency-Key header), GET {base}/transfers/{ref},
    GET {base}/transfers?idempotency_key=…  Configure FINANCE_BANK_API_URL and FINANCE_BANK_API_TOKEN."""

    name = 'http'

    def __init__(self, base_url: str, token: str | None, *, timeout: float = 15.0, transport=None) -> None:
        self.base_url, self.token, self.timeout, self.transport = base_url.rstrip('/'), token, timeout, transport

    def _client(self):
        import httpx
        headers = {'Authorization': f'Bearer {self.token}'} if self.token else {}
        return httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout, transport=self.transport)

    @staticmethod
    def _status(data: dict) -> TransferStatus:
        return TransferStatus(str(data['id']), str(data.get('status', 'unknown')), Decimal(str(data.get('amount', '0'))),
                              str(data.get('currency', '')), str(data.get('payee_account', '')), str(data.get('reference', '')))

    async def submit(self, req: TransferRequest) -> str:
        import httpx
        body = {'amount': format(req.amount, 'f'), 'currency': req.currency, 'payee_account': req.payee_account,
                'payee_name': req.payee_name, 'reference': req.reference}
        try:
            async with self._client() as client:
                response = await client.post('/transfers', json=body, headers={'Idempotency-Key': req.idempotency_key})
        except httpx.ConnectError as exc:  # nothing reached the bank
            raise ConnectorUnavailable(type(exc).__name__) from None
        response.raise_for_status()
        return str(response.json()['id'])

    async def get(self, ref: str) -> TransferStatus:
        async with self._client() as client:
            response = await client.get(f'/transfers/{ref}')
        response.raise_for_status()
        return self._status(response.json())

    async def find(self, idempotency_key: str) -> TransferStatus | None:
        async with self._client() as client:
            response = await client.get('/transfers', params={'idempotency_key': idempotency_key})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        items = response.json().get('items', [])
        return self._status(items[0]) if items else None


class FailoverPaymentConnector:
    """Primary first; the fallback only when the primary is unreachable *before* accepting (ConnectorUnavailable).
    References are prefixed with the connector name so reads go to the connector that holds the transfer."""

    def __init__(self, primary: PaymentConnector, fallback: PaymentConnector | None, *, settings=None) -> None:
        self.primary, self.fallback, self.settings = primary, fallback, settings
        self.name = primary.name if fallback is None else f'{primary.name}+{fallback.name}'

    def _pick(self, ref: str):
        prefix, _, raw = ref.partition(':')
        for connector in (self.primary, self.fallback):
            if connector is not None and connector.name == prefix:
                return connector, raw
        if prefix in {'mock', 'http'} and self.settings is not None:
            # The transfer was made before an administrator switched connectors: read it where it lives.
            try:
                other = _single(prefix, self.settings)
            except ConnectorUnavailable:
                other = None
            if other is not None:
                return other, raw
        return self.primary, ref

    async def submit(self, req: TransferRequest) -> str:
        try:
            return f'{self.primary.name}:{await self.primary.submit(req)}'
        except ConnectorUnavailable:
            if self.fallback is None:
                raise
            return f'{self.fallback.name}:{await self.fallback.submit(req)}'

    async def get(self, ref: str) -> TransferStatus:
        connector, raw = self._pick(ref)
        status = await connector.get(raw)
        return TransferStatus(ref, status.status, status.amount, status.currency, status.payee_account, status.reference)

    async def find(self, idempotency_key: str) -> TransferStatus | None:
        for connector in (self.primary, self.fallback):
            if connector is None:
                continue
            try:
                found = await connector.find(idempotency_key)
            except ConnectorUnavailable:
                continue
            if found is not None:
                return TransferStatus(f'{connector.name}:{found.ref}', found.status, found.amount, found.currency,
                                      found.payee_account, found.reference)
        return None


MOCK = MockPaymentConnector()  # one test bank per process
_override: PaymentConnector | None = None


def set_connector_override(connector: PaymentConnector | None) -> None:
    """Tests and the admin 'test connector' switch."""
    global _override
    _override = connector


def _single(name: str, settings) -> PaymentConnector | None:
    if name in {'', 'none'}:
        return None
    if name == 'mock':
        if getattr(settings, 'app_env', '') == 'production' and not getattr(settings, 'finance_allow_mock_in_production', False):
            raise ConnectorUnavailable('The mock payment connector is disabled in production')
        return MOCK
    if name == 'http':
        url = getattr(settings, 'finance_bank_api_url', None)
        if not url:
            raise ConnectorUnavailable('FINANCE_BANK_API_URL is not configured')
        token = getattr(settings, 'finance_bank_api_token', None)
        return HttpPaymentConnector(url, token.get_secret_value() if token else None)
    raise ConnectorUnavailable(f'Unknown payment connector {name}')


MODES = {'mock': ('mock', 'none'), 'http': ('http', 'none'), 'http+mock': ('http', 'mock')}


def get_connector(settings=None, *, mode: str | None = None) -> PaymentConnector:
    """`mode` is the administrator's choice (org settings); None = the environment configuration."""
    if _override is not None:
        return _override
    if settings is None:
        from app.core.config import get_settings
        settings = get_settings()
    if mode in MODES:
        primary_name, fallback_name = MODES[mode]
    else:
        primary_name = getattr(settings, 'finance_payment_connector', 'mock')
        fallback_name = getattr(settings, 'finance_payment_fallback', 'none')
    try:
        primary = _single(primary_name, settings)
    except ConnectorUnavailable:
        primary = None
    # A fallback that cannot be built (e.g. mock refused in production, or an
    # http fallback with no URL) must not break a usable primary: drop it and
    # keep the primary alone, instead of failing the whole selection.
    try:
        fallback = _single(fallback_name, settings) if fallback_name not in {primary_name} else None
    except ConnectorUnavailable:
        fallback = None
    if primary is None:
        if fallback is None:
            raise ConnectorUnavailable('No payment connector is configured')
        primary, fallback = fallback, None
    return FailoverPaymentConnector(primary, fallback, settings=settings)


async def admin_mode(db) -> str | None:
    from app.platform.models import OrgSettings
    row = await db.get(OrgSettings, 1, populate_existing=True)
    return row.finance_payment_connector if row is not None else None


async def resolve_connector(db, settings=None) -> PaymentConnector:
    """The connector in force right now: the administrator's choice if set, else the environment."""
    return get_connector(settings, mode=await admin_mode(db))


async def connector_status(db, settings, *, probe: bool = False) -> dict:
    """What an administrator sees: which connector is active, where the choice comes from, and whether the
    real bank is configured. Never returns the token."""
    from urllib.parse import urlparse
    mode = await admin_mode(db)
    env_mode = settings.finance_payment_connector + (
        f'+{settings.finance_payment_fallback}' if settings.finance_payment_fallback not in {'none', settings.finance_payment_connector} else '')
    out = {
        'source': 'admin' if mode else 'environment',
        'admin_mode': mode,
        'environment_mode': env_mode,
        'modes': list(MODES),
        'http_configured': bool(settings.finance_bank_api_url),
        'http_host': urlparse(settings.finance_bank_api_url).hostname if settings.finance_bank_api_url else None,
        'http_token_set': settings.finance_bank_api_token is not None,
        'mock_allowed': settings.app_env != 'production' or settings.finance_allow_mock_in_production,
        'production': settings.app_env == 'production',
        'test_override': _override is not None,
    }
    try:
        connector = get_connector(settings, mode=mode)
        out.update(active=connector.name, available=True, problem=None,
                   is_mock='mock' in connector.name.split('+')[:1])
    except ConnectorUnavailable as exc:
        out.update(active=None, available=False, problem=str(exc), is_mock=False)
        return out
    out['real_bank'] = connector.name.startswith('http')
    if probe and out['real_bank']:
        try:
            await connector.find('infraspine-connectivity-probe')
            out['reachable'] = True
        except Exception as exc:  # report, never raise: this is a status page
            out['reachable'], out['probe_error'] = False, type(exc).__name__
    return out
