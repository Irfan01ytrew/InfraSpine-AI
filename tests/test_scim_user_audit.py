"""SCIM provisioning audit payloads contain event metadata, not credential or directory values."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

from app.db.models import AuditLog, User
from app.schemas.sso import ScimPatchOperation, ScimPatchRequest, ScimUserCreate
from app.services import sso


def test_scim_user_create_and_patch_audit_actions_omit_password_and_external_id_values(monkeypatch):
    async def run():
        monkeypatch.setattr(sso, "hash_password", lambda _password: "synthetic-password-hash")
        db = AsyncMock()
        db.add = MagicMock()
        empty = MagicMock()
        empty.scalar_one_or_none.return_value = None
        db.execute.side_effect = [empty, empty]
        settings = MagicMock()
        settings.sso_default_role = "employee"

        created = await sso.scim_create_user(
            db,
            ScimUserCreate(
                userName="audit-user@example.test",
                externalId="sensitive-directory-object-id",
                active=True,
            ),
            settings,
        )
        user = next(call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], User))
        created_audit = next(call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], AuditLog))

        assert created.id == str(user.id)
        assert created_audit.action == "user.scim_created"
        assert created_audit.actor_email == "SCIM directory"
        assert created_audit.target == str(user.id)
        assert created_audit.details == {"role": "employee", "active": True}
        assert "sensitive-directory-object-id" not in repr(created_audit.details)
        assert "synthetic-password-hash" not in repr(created_audit.details)

        db.execute.side_effect = [empty]
        db.get.return_value = user
        await sso.scim_patch_user(
            db,
            user.id,
            ScimPatchRequest(
                Operations=[
                    ScimPatchOperation(op="replace", path="externalId", value="changed-directory-id"),
                    ScimPatchOperation(op="replace", path="roles", value=["manager"]),
                ]
            ),
        )
        update_audit = [
            call.args[0]
            for call in db.add.call_args_list
            if isinstance(call.args[0], AuditLog) and call.args[0].action == "user.scim_updated"
        ][0]
        assert update_audit.actor_email == "SCIM directory"
        assert update_audit.details == {"fields": ["externalId", "role"]}
        assert "changed-directory-id" not in repr(update_audit.details)

    asyncio.run(run())
