"""Focused SCIM offboarding guards; database mutation is delegated to the shared user lifecycle."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import PermissionDeniedError
from app.db.models import User
from app.services import users as users_service
from app.services.sso import scim_delete_user


def _result(*, one=None, many=None):
    result = MagicMock()
    result.scalar_one_or_none.return_value = one
    result.scalars.return_value = list(many or [])
    return result


def test_scim_deprovision_uses_shared_offboarding_and_keeps_directory_audit_actor(monkeypatch):
    async def run():
        departing = User(
            id=uuid.uuid4(), email="departing@example.test", password_hash="hash", role="employee", is_active=True
        )
        successor = User(
            id=uuid.uuid4(), email="admin@example.test", password_hash="hash", role="admin", is_active=True
        )
        db = AsyncMock()
        db.execute.side_effect = [_result(), _result(one=departing), _result(many=[successor])]
        calls = {}

        class FakeUserAdminService:
            def __init__(self, session, admin, ctx):
                calls.update(session=session, admin=admin, ctx=ctx)

            async def set_active(self, user_id, active, knowledge_transfer_to=None):
                calls.update(user_id=user_id, active=active, successor=knowledge_transfer_to)

        monkeypatch.setattr(users_service, "UserAdminService", FakeUserAdminService)
        await scim_delete_user(db, departing.id)

        assert calls["session"] is db
        assert calls["admin"] is successor
        assert calls["ctx"].actor_user_id is None
        assert calls["ctx"].actor_email == "SCIM directory"
        assert calls["user_id"] == departing.id
        assert calls["active"] is False
        assert calls["successor"] == successor.id

    asyncio.run(run())


def test_scim_deprovision_rejects_last_active_administrator_without_mutation(monkeypatch):
    async def run():
        admin = User(
            id=uuid.uuid4(), email="only-admin@example.test", password_hash="hash", role="admin", is_active=True
        )
        db = AsyncMock()
        db.execute.side_effect = [_result(), _result(one=admin), _result(many=[admin])]
        service_calls = []

        class FakeUserAdminService:
            def __init__(self, *_args, **_kwargs):
                service_calls.append("constructed")

        monkeypatch.setattr(users_service, "UserAdminService", FakeUserAdminService)
        with pytest.raises(PermissionDeniedError, match="last active administrator"):
            await scim_delete_user(db, admin.id)

        assert service_calls == []
        db.commit.assert_not_awaited()

    asyncio.run(run())


def test_scim_deprovision_of_already_inactive_user_is_idempotent():
    async def run():
        inactive = User(
            id=uuid.uuid4(), email="inactive@example.test", password_hash="hash", role="employee", is_active=False
        )
        db = AsyncMock()
        db.execute.side_effect = [_result(), _result(one=inactive)]

        await scim_delete_user(db, inactive.id)

        assert db.execute.await_count == 2
        db.commit.assert_not_awaited()

    asyncio.run(run())
