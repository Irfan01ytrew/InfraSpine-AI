"""Focused checks for deployment-enforced MFA gates; synthetic in-memory sessions only."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.api import deps
from app.core.errors import MfaEnrollmentRequiredError, MfaReauthenticationRequiredError
from app.db.models import User
from app.services import auth


class _Result:
    def __init__(self, row):
        self.row = row

    def one_or_none(self):
        return self.row


class _Db:
    def __init__(self, row=None):
        self.row = row
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        return _Result(self.row)

    async def run_sync(self, _fn):
        # Session-only hook used by get_current_user to reconcile org membership; not under test here.
        return None


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"cookie", b"infraspine_session=unit-test-token")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 80),
        }
    )


def _user() -> User:
    return User(id=uuid4(), email="mfa-user@synthetic.test", password_hash="unused", role="employee", is_active=True)


def test_org_mfa_state_distinguishes_enrollment_and_old_session_reauth():
    user = _user()
    policy = SimpleNamespace(org_mfa_required=True)
    enabled = datetime.now(UTC)
    verified = datetime.now(UTC)

    assert asyncio.run(auth.mfa_policy_state(_Db((None, None)), user, "token", policy)) == (True, False)
    assert asyncio.run(auth.mfa_policy_state(_Db((enabled, None)), user, "token", policy)) == (False, True)
    assert asyncio.run(auth.mfa_policy_state(_Db((enabled, verified)), user, "token", policy)) == (False, False)


def test_disabled_org_policy_does_not_query_or_gate():
    db = _Db((None, None))
    assert asyncio.run(auth.mfa_policy_state(db, _user(), "token", SimpleNamespace(org_mfa_required=False))) == (
        False,
        False,
    )
    assert db.calls == 0


def test_enrollment_gate_allows_only_identity_and_factor_setup(monkeypatch):
    user = _user()
    settings = SimpleNamespace(org_mfa_required=True)

    async def resolve(_db, _token):
        return user

    async def enrollment_state(_db, _user, _token, _settings):
        return True, False

    monkeypatch.setattr(auth, "resolve_session", resolve)
    monkeypatch.setattr(auth, "mfa_policy_state", enrollment_state)
    assert asyncio.run(deps.get_current_user(_request("/api/auth/mfa/enrollment"), _Db(), settings)) is user
    with pytest.raises(MfaEnrollmentRequiredError):
        asyncio.run(deps.get_current_user(_request("/api/rag/training"), _Db(), settings))


def test_unverified_existing_session_can_only_reauthenticate_or_sign_out(monkeypatch):
    user = _user()
    settings = SimpleNamespace(org_mfa_required=True)

    async def resolve(_db, _token):
        return user

    async def reauthentication_state(_db, _user, _token, _settings):
        return False, True

    monkeypatch.setattr(auth, "resolve_session", resolve)
    monkeypatch.setattr(auth, "mfa_policy_state", reauthentication_state)
    assert asyncio.run(deps.get_current_user(_request("/api/auth/logout"), _Db(), settings)) is user
    with pytest.raises(MfaReauthenticationRequiredError):
        asyncio.run(deps.get_current_user(_request("/api/rag/training"), _Db(), settings))
