"""Unit and integration tests for Enterprise SSO (OIDC/SAML) and SCIM 2.0 User Provisioning."""

import base64
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import SecretStr
from fastapi import HTTPException

from app.core.config import Settings
from app.db.models import User
from app.schemas.sso import ScimPatchOperation, ScimPatchRequest, ScimUserCreate
from app.services.sso import (
    get_sso_authorization_url,
    process_oidc_callback,
    process_saml_assertion,
    scim_create_user,
    scim_delete_user,
    scim_get_user,
    scim_list_users,
    scim_patch_user,
)
from app.api.routes.scim import verify_scim_token
from app.core.errors import ConflictError, InvalidInputError


def _make_jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode("ascii")
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=").decode("ascii")
    return f"{header}.{payload}.signature"


def _make_saml_xml(email: str, name_id: str | None = None) -> str:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_12345" Version="2.0">
  <saml:Assertion ID="_67890" Version="2.0">
    <saml:Subject>
      <saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{name_id or email}</saml:NameID>
    </saml:Subject>
    <saml:AttributeStatement>
      <saml:Attribute Name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress">
        <saml:AttributeValue>{email}</saml:AttributeValue>
      </saml:Attribute>
    </saml:AttributeStatement>
  </saml:Assertion>
</samlp:Response>"""
    return base64.b64encode(xml.encode("utf-8")).decode("ascii")


def test_sso_authorization_url():
    settings = MagicMock(spec=Settings)
    settings.sso_enabled = True
    settings.sso_authorization_endpoint = "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize"
    settings.sso_client_id = "azure-client-123"

    url = get_sso_authorization_url(settings, state="state_xyz")
    assert url.startswith("https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize?")
    assert "client_id=azure-client-123" in url
    assert "state=state_xyz" in url
    assert "response_type=code" in url


def test_oidc_callback_and_saml_parsing():
    import asyncio

    async def _run():
        settings = MagicMock(spec=Settings)
        settings.sso_enabled = True
        settings.sso_token_endpoint = "https://auth.company.com/oauth/token"
        settings.sso_client_id = "test_client"
        settings.sso_client_secret = SecretStr("test_secret")
        settings.sso_default_role = "employee"
        settings.sso_auto_provision = True
        settings.session_ttl_minutes = 480

        # Mock DB
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None  # New user
        mock_db.execute.return_value = mock_result

        # Test OIDC with ID token
        id_token = _make_jwt({"email": "alice@company.com", "sub": "azure_sub_999"})
        user, token = await process_oidc_callback(
            code="auth_code_123",
            db=mock_db,
            settings=settings,
            id_token=id_token,
        )
        assert user.email == "alice@company.com"
        assert user.role == "employee"
        assert user.external_id == "azure_sub_999"
        assert len(token) > 20

        # Test SAML assertion
        saml_b64 = _make_saml_xml("bob@company.com", name_id="bob_sid_456")
        user_saml, token_saml = await process_saml_assertion(
            saml_response_b64=saml_b64,
            db=mock_db,
            settings=settings,
        )
        assert user_saml.email == "bob@company.com"
        assert len(token_saml) > 20

    asyncio.run(_run())


def test_scim_user_provisioning_and_lifecycle(monkeypatch):
    import asyncio
    from app.services import users as users_service

    async def _run():
        settings = MagicMock(spec=Settings)
        settings.sso_default_role = "employee"

        # Mock DB
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        admin = User(
            id=uuid.uuid4(), email="admin@company.com", password_hash="hash", role="admin", is_active=True
        )

        class FakeUserAdminService:
            def __init__(self, db, selected_admin, ctx):
                self.db = db
                self.selected_admin = selected_admin
                self.ctx = ctx

            async def set_active(self, user_id, active, knowledge_transfer_to=None):
                assert user_id == existing_user.id
                assert active is False
                assert knowledge_transfer_to == admin.id
                existing_user.is_active = False
                await self.db.commit()

        monkeypatch.setattr(users_service, "UserAdminService", FakeUserAdminService)

        # 1. SCIM Create User
        create_payload = ScimUserCreate(
            userName="carol@company.com",
            externalId="directory-carol-001",
            roles=["employee"],
            active=True,
        )
        created = await scim_create_user(mock_db, create_payload, settings)
        assert created.userName == "carol@company.com"
        assert created.externalId == "directory-carol-001"
        assert created.active is True
        assert "employee" in created.roles

        # 2. SCIM Patch (deactivation)
        existing_user = User(
            id=uuid.uuid4(),
            email="carol@company.com",
            password_hash="hash",
            role="employee",
            is_active=True,
        )
        mock_db.get.return_value = existing_user
        locked_user_result = MagicMock()
        locked_user_result.scalar_one_or_none.return_value = existing_user
        external_id_result = MagicMock()
        external_id_result.scalar_one_or_none.return_value = None
        active_admin_result = MagicMock()
        active_admin_result.scalars.return_value = [admin]
        mock_db.execute.side_effect = [MagicMock(), locked_user_result, external_id_result, active_admin_result]

        patch_payload = ScimPatchRequest(
            Operations=[
                ScimPatchOperation(op="replace", path="active", value=False),
                ScimPatchOperation(op="replace", path="externalId", value="directory-carol-002"),
            ]
        )
        patched = await scim_patch_user(mock_db, existing_user.id, patch_payload)
        assert patched.active is False
        assert patched.externalId == "directory-carol-002"

        removed_external_id = await scim_patch_user(
            mock_db,
            existing_user.id,
            ScimPatchRequest(Operations=[ScimPatchOperation(op="remove", path="externalId")]),
        )
        assert removed_external_id.externalId is None

        # 3. SCIM Delete (deprovision)
        locked_user_result = MagicMock()
        locked_user_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.side_effect = [MagicMock(), locked_user_result]
        await scim_delete_user(mock_db, existing_user.id)
        assert existing_user.is_active is False

    asyncio.run(_run())


def test_scim_directory_identity_filter_is_supported_and_unknown_filter_rejected():
    import asyncio

    from app.services.sso import scim_list_users

    async def _run():
        db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        users_result = MagicMock()
        users_result.scalars.return_value.all.return_value = []
        db.execute.side_effect = [count_result, users_result]

        response = await scim_list_users(db, filter_expr='externalId eq "directory-42"')
        assert response.totalResults == 0
        assert "users.external_id" in str(db.execute.await_args_list[0].args[0])
        assert "users.external_id" in str(db.execute.await_args_list[1].args[0])

        invalid_db = AsyncMock()
        with pytest.raises(InvalidInputError):
            await scim_list_users(invalid_db, filter_expr='password eq "unknown"')
        invalid_db.execute.assert_not_awaited()

    asyncio.run(_run())


def test_scim_external_id_conflict_is_rejected():
    import asyncio

    async def _run():
        settings = MagicMock(spec=Settings)
        settings.sso_default_role = "employee"
        db = AsyncMock()
        email_lookup = MagicMock()
        email_lookup.scalar_one_or_none.return_value = None
        external_lookup = MagicMock()
        external_lookup.scalar_one_or_none.return_value = uuid.uuid4()
        db.execute.side_effect = [email_lookup, external_lookup]

        with pytest.raises(ConflictError):
            await scim_create_user(
                db,
                ScimUserCreate(userName="new@company.com", externalId="directory-duplicate"),
                settings,
            )
        db.add.assert_not_called()

    asyncio.run(_run())


def test_scim_token_uses_case_insensitive_bearer_and_rejects_invalid_token():
    settings = MagicMock()
    settings.scim_enabled = True
    settings.scim_bearer_token = SecretStr("synthetic-scim-token")

    verify_scim_token(settings, "bEaReR synthetic-scim-token")
    with pytest.raises(HTTPException) as error:
        verify_scim_token(settings, "Bearer wrong-token")
    assert error.value.status_code == 401


def test_scim_api_external_id_filter_and_pagination_validation(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.core.config import get_settings
    from app.main import app
    from app.services import users as users_service

    settings = MagicMock()
    settings.scim_enabled = True
    settings.scim_bearer_token = SecretStr("synthetic-scim-token")
    settings.sso_default_role = "employee"
    db = AsyncMock()
    empty_lookup = MagicMock()
    empty_lookup.scalar_one_or_none.return_value = None
    count_result = MagicMock()
    count_result.scalar_one.return_value = 1
    users_result = MagicMock()
    users_result.scalars.return_value.all.side_effect = lambda: [created_user["value"]]
    created_user = {}
    def add_created_user(entity):
        if isinstance(entity, User):
            created_user["value"] = entity

    db.add = MagicMock(side_effect=add_created_user)
    db.get.side_effect = lambda *_args: created_user.get("value")
    user_lookup = MagicMock()
    user_lookup.scalar_one_or_none.side_effect = lambda: created_user.get("value")
    admin = User(
        id=uuid.uuid4(), email="scim-admin@example.test", password_hash="hash", role="admin", is_active=True
    )
    admins_result = MagicMock()
    admins_result.scalars.return_value = [admin]

    class FakeUserAdminService:
        def __init__(self, _db, _admin, _ctx):
            pass

        async def set_active(self, user_id, active, knowledge_transfer_to=None):
            assert active is False
            created_user["value"].is_active = False

    monkeypatch.setattr(users_service, "UserAdminService", FakeUserAdminService)
    db.execute.side_effect = [
        empty_lookup, empty_lookup, empty_lookup,
        MagicMock(), user_lookup, admins_result,
        count_result, users_result,
    ]

    async def override_db():
        yield db

    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: settings)
    monkeypatch.setitem(app.dependency_overrides, get_db, override_db)
    client = TestClient(app)
    headers = {"Authorization": "bEaReR synthetic-scim-token"}
    created = client.post(
        "/api/scim/v2/Users",
        json={"userName": "scim-user@example.test", "externalId": "directory-42", "active": True},
        headers=headers,
    )
    assert created.status_code == 201
    user_id = created.json()["id"]
    assert created.json()["externalId"] == "directory-42"

    fetched = client.get(f"/api/scim/v2/Users/{user_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["externalId"] == "directory-42"

    patched = client.patch(
        f"/api/scim/v2/Users/{user_id}",
        json={"Operations": [{"op": "replace", "path": "externalId", "value": "directory-43"}]},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["externalId"] == "directory-43"

    deleted = client.delete(f"/api/scim/v2/Users/{user_id}", headers=headers)
    assert deleted.status_code == 204
    assert created_user["value"].is_active is False

    response = client.get(
        "/api/scim/v2/Users",
        params={"filter": 'externalId eq "directory-43"'},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["Resources"][0]["externalId"] == "directory-43"
    assert response.json()["Resources"][0]["active"] is False

    invalid_filter = client.get(
        "/api/scim/v2/Users",
        params={"filter": 'password eq "user@example.test"'},
        headers=headers,
    )
    assert invalid_filter.status_code == 422
    assert db.execute.await_count == 8

    invalid_page = client.get("/api/scim/v2/Users?startIndex=0", headers=headers)
    assert invalid_page.status_code == 422


def test_sso_and_scim_api_endpoints():
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    # 1. SSO config endpoint
    resp = client.get("/api/auth/sso/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "enabled" in data
    assert "provider_type" in data

    # 2. SCIM discovery endpoints
    resp = client.get("/api/scim/v2/ServiceProviderConfig")
    assert resp.status_code == 200
    assert resp.json()["patch"]["supported"] is True

    resp = client.get("/api/scim/v2/Schemas")
    assert resp.status_code == 200
    assert resp.json()["totalResults"] == 2
    assert {item["id"] for item in resp.json()["Resources"]} == {
        "urn:ietf:params:scim:schemas:core:2.0:User",
        "urn:ietf:params:scim:schemas:core:2.0:Group",
    }

    # 3. SCIM protected endpoint without token returns 401 or 403
    resp = client.get("/api/scim/v2/Users")
    assert resp.status_code in (401, 403)
