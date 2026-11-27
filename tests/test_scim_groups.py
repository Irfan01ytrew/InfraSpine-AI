"""SCIM directory-group lifecycle: scoped imports, strict members, patch, and deprovisioning."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from app.core.errors import InvalidInputError
from app.db.models import AccessGroup, User
from app.schemas.sso import ScimGroupCreate, ScimPatchOperation, ScimPatchRequest
from app.services import source_acl, sso


def _result(*, one=None, many=None, scalar=None, rowcount=0):
    result = MagicMock()
    result.scalar_one_or_none.return_value = one
    result.scalar_one.return_value = scalar
    result.scalars.return_value = list(many or [])
    result.rowcount = rowcount
    return result


def test_scim_group_patch_replaces_name_and_adds_resolved_user_members(monkeypatch):
    async def run():
        group = AccessGroup(
            id=uuid.uuid4(), name="Old Group", kind="custom", source="directory",
            external_id="idp-group-22", lifecycle_status="active",
        )
        first = User(id=uuid.uuid4(), email="first@example.test", password_hash="hash", role="employee")
        second = User(id=uuid.uuid4(), email="second@example.test", password_hash="hash", role="employee")
        db = AsyncMock()
        db.execute.side_effect = [
            _result(one=group),
            _result(many=[first.id]),
            _result(many=[second.id]),
            _result(many=[first]),
        ]
        sync_call = {}

        async def fake_sync(_db, external_id, name, kind, member_external_ids, ctx, *, member_user_ids=None):
            sync_call.update(
                external_id=external_id, name=name, kind=kind, external_ids=member_external_ids,
                user_ids=member_user_ids, actor=ctx.actor_email,
            )
            group.name = name
            return group

        monkeypatch.setattr(source_acl, "sync_directory_group", fake_sync)
        response = await sso.scim_patch_group(
            db,
            group.id,
            ScimPatchRequest(
                Operations=[
                    ScimPatchOperation(op="replace", path="displayName", value="Renamed Group"),
                    ScimPatchOperation(
                        op="add", path="members", value=[{"value": str(second.id), "type": "User"}]
                    ),
                    ScimPatchOperation(
                        op="remove", path=f'members[value eq "{second.id}"]'
                    ),
                ]
            ),
        )

        assert sync_call == {
            "external_id": "idp-group-22",
            "name": "Renamed Group",
            "kind": "custom",
            "external_ids": [],
            "user_ids": {first.id},
            "actor": "SCIM directory",
        }
        assert response.displayName == "Renamed Group"
        assert {member.value for member in response.members} == {str(first.id)}

    asyncio.run(run())


def test_scim_group_rejects_unknown_member_before_sync(monkeypatch):
    async def run():
        group = AccessGroup(
            id=uuid.uuid4(), name="Group", kind="custom", source="directory",
            external_id="idp-group-23", lifecycle_status="active",
        )
        unknown_id = uuid.uuid4()
        db = AsyncMock()
        db.execute.side_effect = [_result(one=group), _result(many=[]), _result(many=[])]
        sync = AsyncMock()
        monkeypatch.setattr(source_acl, "sync_directory_group", sync)

        with pytest.raises(InvalidInputError, match="members do not exist"):
            await sso.scim_patch_group(
                db,
                group.id,
                ScimPatchRequest(
                    Operations=[
                        ScimPatchOperation(
                            op="add", path="members", value=[{"value": str(unknown_id), "type": "User"}]
                        )
                    ]
                ),
            )

        sync.assert_not_awaited()
        db.commit.assert_not_awaited()

    asyncio.run(run())


def test_scim_group_delete_archives_and_removes_members_with_audit(monkeypatch):
    async def run():
        group = AccessGroup(
            id=uuid.uuid4(), name="Group", kind="custom", source="directory",
            external_id="idp-group-24", lifecycle_status="active",
        )
        db = AsyncMock()
        db.execute.side_effect = [_result(one=group), _result(rowcount=3)]
        audit_record = MagicMock()
        from app.services import audit

        monkeypatch.setattr(audit, "record", audit_record)

        await sso.scim_delete_group(db, group.id)

        assert group.lifecycle_status == "archived"
        db.commit.assert_awaited_once()
        assert audit_record.call_args.args[2] == "access.directory_group_deprovisioned"
        assert audit_record.call_args.kwargs["details"] == {"members_removed": 3}

    asyncio.run(run())


def test_directory_group_sync_can_apply_resolved_scim_user_ids():
    async def run():
        user_id = uuid.uuid4()
        group_id = uuid.uuid4()
        db = AsyncMock()
        db.add = MagicMock()
        added = []
        db.add.side_effect = added.append
        db.execute.side_effect = [_result(one=None), _result(many=[user_id]), _result(rowcount=0)]

        def assign_group_id():
            added[0].id = group_id

        db.flush.side_effect = assign_group_id
        from app.services.audit import AuditContext

        group = await source_acl.sync_directory_group(
            db,
            "scim-directory-group-1",
            "QA custom group",
            "custom",
            [],
            AuditContext(None, "SCIM directory", None, None),
            member_user_ids={user_id},
        )

        assert group.id == group_id
        assert group.source == "directory"
        assert group.lifecycle_status == "active"
        membership = next(value for value in added if hasattr(value, "group_id"))
        assert membership.group_id == group_id
        assert membership.user_id == user_id
        db.commit.assert_awaited_once()

    asyncio.run(run())


def test_scim_group_filter_is_exact_and_rejects_unknown_attributes():
    condition = sso._scim_group_filter_condition('displayName eq "Operations"')
    assert "lower(access_groups.name)" in str(condition)
    with pytest.raises(InvalidInputError):
        sso._scim_group_filter_condition('members eq "user"')


def test_scim_group_api_requires_token_and_advertises_group_schema(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.core.config import get_settings
    from app.main import app

    settings = MagicMock()
    settings.scim_enabled = True
    settings.scim_bearer_token = SecretStr("synthetic-scim-group-token")
    db = AsyncMock()
    created = sso.ScimGroupResponse(
        id=str(uuid.uuid4()), externalId="idp-group-25", displayName="QA Group", members=[],
        meta={"resourceType": "Group"},
    )
    create_group = AsyncMock(return_value=created)
    list_groups = AsyncMock(return_value=sso.ScimGroupListResponse(
        totalResults=1, startIndex=1, itemsPerPage=1, Resources=[created]
    ))
    get_group = AsyncMock(return_value=created)
    patch_group = AsyncMock(return_value=created)
    delete_group = AsyncMock()
    monkeypatch.setattr(sso, "scim_create_group", create_group)
    monkeypatch.setattr(sso, "scim_list_groups", list_groups)
    monkeypatch.setattr(sso, "scim_get_group", get_group)
    monkeypatch.setattr(sso, "scim_patch_group", patch_group)
    monkeypatch.setattr(sso, "scim_delete_group", delete_group)

    async def override_db():
        yield db

    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: settings)
    monkeypatch.setitem(app.dependency_overrides, get_db, override_db)
    client = TestClient(app)
    assert client.get("/api/scim/v2/Groups").status_code == 401

    headers = {"Authorization": "Bearer synthetic-scim-group-token"}
    schemas = client.get("/api/scim/v2/Schemas")
    assert schemas.status_code == 200
    assert schemas.json()["totalResults"] == 2
    assert schemas.json()["Resources"][1]["name"] == "Group"

    response = client.post(
        "/api/scim/v2/Groups",
        json={"externalId": "idp-group-25", "displayName": "QA Group", "members": []},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["externalId"] == "idp-group-25"
    group_id = response.json()["id"]

    fetched = client.get(f"/api/scim/v2/Groups/{group_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["displayName"] == "QA Group"

    patched = client.patch(
        f"/api/scim/v2/Groups/{group_id}",
        json={"Operations": [{"op": "replace", "path": "displayName", "value": "QA Group Renamed"}]},
        headers=headers,
    )
    assert patched.status_code == 200

    deleted = client.delete(f"/api/scim/v2/Groups/{group_id}", headers=headers)
    assert deleted.status_code == 204
    delete_group.assert_awaited_once()

    listed = client.get(
        "/api/scim/v2/Groups", params={"filter": 'displayName eq "QA Group"'}, headers=headers
    )
    assert listed.status_code == 200
    assert listed.json()["Resources"][0]["displayName"] == "QA Group"
    list_groups.assert_awaited_once()
