"""Synthetic tests for per-installation setup metadata and offline license verification."""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.errors import ConflictError
from app.db.models import User, UserInvitation
from app.main import path_is_licensed, required_license_feature
from app.schemas.deployment import CompanyProfile, LicenseClaims, SignedLicense
from app.services.audit import AuditContext
from app.services.deployment import (
    _canonical_payload,
    deployment_id,
    license_claims,
    get_company_profile,
    license_status,
    save_company_profile,
    save_license,
    validate_license,
)
from app.services.users import UserAdminService
from tests.conftest import ADMIN_EMAIL, login


def _signing_pair():
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return private, base64.b64encode(raw).decode("ascii")


def _signed(
    settings, private, *, issued_at=None, expires_at=None, seat_limit=25, features=None,
    license_id="license-test-0001",
):
    now = datetime.now(UTC).replace(microsecond=0)
    claims = LicenseClaims(
        product="infraspine-ai",
        license_id=license_id,
        customer="Synthetic Northwind",
        deployment_id=deployment_id(settings),
        issued_at=issued_at or now,
        expires_at=expires_at or now + timedelta(days=30),
        seat_limit=seat_limit,
        features=features if features is not None else ["research", "reporting"],
    )
    signature = base64.b64encode(private.sign(_canonical_payload(claims))).decode("ascii")
    return SignedLicense(payload=claims, signature=signature)


def test_company_profile_is_local_and_defaults_to_setup_required(tmp_path):
    settings = get_settings().model_copy(update={"rag_data_dir": tmp_path})

    default = get_company_profile(settings)
    assert default.setup_complete is False
    assert default.industry == "General"

    profile = CompanyProfile(
        company_name="Northwind Traders",
        industry="Manufacturing",
        brand_name="Northwind Knowledge",
        primary_color="#245A72",
    )
    save_company_profile(settings, profile)
    assert get_company_profile(settings) == profile
    assert (tmp_path / "company-profile.json").exists()


def test_license_signature_deployment_binding_and_seat_claims(tmp_path):
    private, public_key = _signing_pair()
    settings = get_settings().model_copy(
        update={"rag_data_dir": tmp_path, "license_public_key": public_key, "license_enforcement_enabled": True}
    )
    envelope = _signed(settings, private)

    assert validate_license(settings, envelope).seat_limit == 25
    save_license(settings, envelope)
    status = license_status(settings)
    assert status.status == "valid"
    assert status.customer == "Synthetic Northwind"
    assert status.features == ["research", "reporting"]

    changed = envelope.model_copy(update={"payload": envelope.payload.model_copy(update={"seat_limit": 250})})
    with pytest.raises(ValueError, match="signature"):
        validate_license(settings, changed)

    other_deployment = get_settings().model_copy(update={"rag_data_dir": tmp_path / "other"})
    other = _signed(other_deployment, private)
    with pytest.raises(ValueError, match="different deployment"):
        validate_license(settings, other)


def test_expired_license_is_rejected_and_status_is_non_valid(tmp_path):
    private, public_key = _signing_pair()
    settings = get_settings().model_copy(update={"rag_data_dir": tmp_path, "license_public_key": public_key})
    expired = _signed(
        settings,
        private,
        issued_at=datetime.now(UTC) - timedelta(days=60),
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    with pytest.raises(ValueError, match="expired"):
        validate_license(settings, expired)
    (tmp_path / "license.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "license.json").write_text(expired.model_dump_json(), encoding="utf-8")
    assert license_status(settings).status == "expired"
    assert license_claims(settings) is None


def test_signed_renewal_replaces_expiring_license_and_updates_entitlements(tmp_path):
    private, public_key = _signing_pair()
    settings = get_settings().model_copy(update={"rag_data_dir": tmp_path, "license_public_key": public_key})
    now = datetime.now(UTC).replace(microsecond=0)
    expiring = _signed(
        settings,
        private,
        issued_at=now - timedelta(days=29),
        expires_at=now + timedelta(hours=1),
        seat_limit=3,
        features=["research"],
    )
    renewed = _signed(
        settings,
        private,
        issued_at=now,
        expires_at=now + timedelta(days=365),
        seat_limit=40,
        features=["research", "reporting", "training"],
        license_id="license-test-0002",
    )

    save_license(settings, expiring)
    assert license_claims(settings).seat_limit == 3
    assert license_status(settings).features == ["research"]

    save_license(settings, renewed)
    claims = license_claims(settings)
    status = license_status(settings)
    assert claims is not None
    assert claims.license_id == renewed.payload.license_id
    assert claims.seat_limit == 40
    assert status.status == "valid"
    assert status.expires_at == renewed.payload.expires_at
    assert status.features == ["research", "reporting", "training"]

    expired = _signed(
        settings,
        private,
        issued_at=now - timedelta(days=2),
        expires_at=now - timedelta(seconds=1),
        seat_limit=1,
        features=[],
    )
    with pytest.raises(ValueError, match="expired"):
        save_license(settings, expired)
    # A rejected renewal must leave the last valid license active on disk.
    assert license_claims(settings).seat_limit == 40
    assert license_status(settings).status == "valid"


def test_concurrent_signed_renewals_leave_a_complete_verified_license(tmp_path):
    """Atomic license replacement must not expose a partial envelope during concurrent renewal."""
    private, public_key = _signing_pair()
    settings = get_settings().model_copy(update={"rag_data_dir": tmp_path, "license_public_key": public_key})
    # Initialize the installation identity before racing license writes.
    deployment_id(settings)
    now = datetime.now(UTC).replace(microsecond=0)
    licenses = [
        _signed(
            settings,
            private,
            issued_at=now,
            expires_at=now + timedelta(days=90 + index),
            seat_limit=10 + index,
            features=["research", "reporting"] if index % 2 else ["research"],
            license_id=f"license-renewal-{index:02d}",
        )
        for index in range(8)
    ]
    start = Barrier(len(licenses))

    def renew(license):
        start.wait(timeout=10)
        return save_license(settings, license)

    with ThreadPoolExecutor(max_workers=len(licenses)) as pool:
        results = list(pool.map(renew, licenses))

    assert {result.license_id for result in results} == {item.payload.license_id for item in licenses}
    stored = SignedLicense.model_validate_json((tmp_path / "license.json").read_bytes())
    assert validate_license(settings, stored).license_id in {item.payload.license_id for item in licenses}
    assert license_status(settings).status == "valid"
    assert not list(tmp_path.glob(".license.json.*.tmp"))


def test_license_enforcement_whitelist_and_optional_features():
    assert path_is_licensed("/api/rag/chat")
    assert not path_is_licensed("/api/auth/login")
    assert not path_is_licensed("/api/admin/deployment/license")
    assert not path_is_licensed("/api/deployment/profile")
    assert required_license_feature("/api/rag/research") == "research"
    assert required_license_feature("/api/rag/training") == "training"
    assert required_license_feature("/api/rag/training/123") == "training"
    assert required_license_feature("/api/rag/training/123/complete") == "training"
    assert required_license_feature("/api/rag/market-research") == "market_research"
    assert required_license_feature("/api/admin/reports/usage") == "reporting"
    assert required_license_feature("/api/rag/chat") is None


def test_license_expiry_and_optional_entitlement_gate_real_api_requests(tmp_path, monkeypatch):
    """Exercise middleware gates before auth/route execution using a separately configured app."""
    from fastapi.testclient import TestClient

    import app.main as main_module

    private, public_key = _signing_pair()
    settings = get_settings().model_copy(
        update={
            "rag_data_dir": tmp_path,
            "license_public_key": public_key,
            "license_enforcement_enabled": True,
        }
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    licensed_app = main_module.create_app()

    # A cryptographically valid, unexpired license without reporting may use base APIs,
    # while the reporting API is denied before route authentication or data access.
    save_license(settings, _signed(settings, private, features=["research"]))
    with TestClient(licensed_app) as licensed_client:
        missing_entitlement = licensed_client.get("/api/admin/reports/usage")
        assert missing_entitlement.status_code == 403
        assert missing_entitlement.json()["error"]["code"] == "license_feature_not_included"

        save_license(settings, _signed(settings, private, features=["reporting"]))
        entitled_but_unauthenticated = licensed_client.get("/api/admin/reports/usage")
        assert entitled_but_unauthenticated.status_code == 401

        now = datetime.now(UTC).replace(microsecond=0)
        expired = _signed(
            settings,
            private,
            issued_at=now - timedelta(days=2),
            expires_at=now - timedelta(seconds=1),
        )
        (tmp_path / "license.json").write_text(expired.model_dump_json(), encoding="utf-8")
        expired_request = licensed_client.get("/api/admin/reports/usage")
        assert expired_request.status_code == 402
        assert expired_request.json()["error"]["code"] == "license_required"


def test_company_setup_is_admin_only_and_profile_is_public_for_branding(client):
    public = client.get("/api/deployment/profile")
    assert public.status_code == 200
    assert public.json()["setup_complete"] is False
    assert client.get("/api/admin/deployment").status_code == 401

    login(client, ADMIN_EMAIL)
    response = client.put(
        "/api/admin/deployment/company",
        json={
            "company_name": "Northwind Traders",
            "industry": "Manufacturing",
            "brand_name": "Northwind Knowledge",
            "primary_color": "#245A72",
            "setup_complete": True,
        },
    )
    assert response.status_code == 200, response.text
    assert client.get("/api/deployment/profile").json()["brand_name"] == "Northwind Knowledge"
    overview = client.get("/api/admin/deployment").json()
    assert overview["company_name"] == "Northwind Traders"
    assert overview["license"]["status"] == "not_configured"


def test_regional_settings_are_optional_validated_and_published(client):
    base = {"company_name": "Northwind Traders", "industry": "Retail", "brand_name": "Northwind", "primary_color": "#245A72"}
    login(client, ADMIN_EMAIL)
    # A profile saved without regional settings keeps every viewer on their own browser formats.
    assert client.put("/api/admin/deployment/company", json=base).status_code == 200
    public = client.get("/api/deployment/profile").json()
    assert (public["currency"], public["time_zone"], public["locale"]) == (None, None, None)

    regional = {**base, "currency": "PKR", "time_zone": "Asia/Karachi", "locale": "ur-PK"}
    assert client.put("/api/admin/deployment/company", json=regional).status_code == 200
    public = client.get("/api/deployment/profile").json()
    assert (public["currency"], public["time_zone"], public["locale"]) == ("PKR", "Asia/Karachi", "ur-PK")

    for bad in ({"time_zone": "Mars/Olympus"}, {"time_zone": "../../etc/passwd"}, {"currency": "rupees"}, {"locale": "Urdu"}):
        assert client.put("/api/admin/deployment/company", json={**base, **bad}).status_code == 422, bad
    assert client.get("/api/deployment/profile").json()["time_zone"] == "Asia/Karachi"  # rejected saves change nothing


def test_profile_saved_before_regional_settings_still_loads(tmp_path):
    legacy = '{"company_name":"Acme","industry":"Retail","brand_name":"Acme","primary_color":"#123456","setup_complete":true,"template_id":null,"template_version":null}'
    profile = CompanyProfile.model_validate_json(legacy)
    assert (profile.currency, profile.time_zone, profile.locale) == (None, None, None)


def test_invitation_creation_enforces_signed_license_seat_limit(admin_client, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.api.routes.admin_users.license_claims",
        lambda _settings: SimpleNamespace(seat_limit=3),
    )
    response = admin_client.post(
        "/api/admin/users/invitations",
        json={"email": "new-seat@example.com", "role": "employee", "department_ids": []},
    )
    assert response.status_code == 409
    assert "seat limit" in response.json()["error"]["message"]
    assert admin_client.get("/api/admin/users/invitations").json()["invitations"] == []


def test_concurrent_invitations_cannot_overbook_the_last_license_seat(migrated_database):
    """Concurrent independent PostgreSQL sessions must reserve the final seat exactly once."""
    from tests.conftest import TEST_DATABASE_URL

    async def run_concurrent_invites():
        engine = create_async_engine(TEST_DATABASE_URL, pool_size=8, max_overflow=0)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db:
                admin = (await db.execute(select(User).where(User.email == ADMIN_EMAIL))).scalar_one()
                admin_id = admin.id
                admin_email = admin.email

            async def invite(index: int):
                async with sessions() as db:
                    admin = await db.get(User, admin_id)
                    ctx = AuditContext(admin.id, admin.email, None, f"seat-check-{index}")
                    return await UserAdminService(db, admin, ctx).create_invitation(
                        f"seat-race-{index}@example.com", "employee", [], seat_limit=4
                    )

            outcomes = await asyncio.gather(
                *(invite(index) for index in range(8)), return_exceptions=True
            )
            created = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
            rejected = [outcome for outcome in outcomes if isinstance(outcome, ConflictError)]
            unexpected = [
                outcome
                for outcome in outcomes
                if isinstance(outcome, Exception) and not isinstance(outcome, ConflictError)
            ]
            assert not unexpected, [type(error).__name__ for error in unexpected]
            assert len(created) == 1
            assert len(rejected) == 7

            async with sessions() as db:
                active_users = await db.scalar(
                    select(func.count()).select_from(User).where(User.is_active.is_(True))
                )
                pending = await db.scalar(
                    select(func.count()).select_from(UserInvitation).where(
                        UserInvitation.accepted_at.is_(None),
                        UserInvitation.revoked_at.is_(None),
                        UserInvitation.expires_at > datetime.now(UTC),
                    )
                )
            assert active_users == 3
            assert pending == 1
            assert active_users + pending == 4
        finally:
            await engine.dispose()

    asyncio.run(run_concurrent_invites())
