"""Per-installation company setup and vendor-signed offline licensing.

Each customer installation has a separate data directory, database and deployment ID. License
verification uses only a customer-deployed public key; the private signing key must stay offline
with the software publisher.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.core.config import Settings
from app.schemas.deployment import CompanyProfile, LicenseClaims, LicenseStatus, SignedLicense

_MAX_LICENSE_BYTES = 32 * 1024
_ATOMIC_JSON_REPLACE_LOCK = threading.Lock()


def _data_dir(settings: Settings) -> Path:
    return settings.rag_data_dir.resolve()


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    """Write private JSON using restrictive file mode where supported and an atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        # Windows can briefly reject a second concurrent replacement of the same
        # destination. Serialize in-process writers and retry only transient sharing
        # violations; the temporary file remains complete until the atomic swap.
        with _ATOMIC_JSON_REPLACE_LOCK:
            attempts = 8 if os.name == "nt" else 1
            for attempt in range(attempts):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError as exc:
                    if (
                        os.name != "nt"
                        or getattr(exc, "winerror", None) not in {5, 32}
                        or attempt == attempts - 1
                    ):
                        raise
                    time.sleep(0.01 * (attempt + 1))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def deployment_id(settings: Settings) -> str:
    path = _data_dir(settings) / "deployment-id"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("Deployment identity path must not be a symbolic link.")
    try:
        if path.stat().st_size > 64:
            raise RuntimeError("Deployment identity file is invalid; restore it from the deployment backup.")
        value = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        value = str(uuid.uuid4())
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:  # simultaneous first requests: use the winner's identity
            value = path.read_text(encoding="ascii").strip()
        else:
            with os.fdopen(descriptor, "w", encoding="ascii") as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise RuntimeError("Deployment identity file is invalid; restore it from the deployment backup.") from None


def get_company_profile(settings: Settings) -> CompanyProfile:
    path = _data_dir(settings) / "company-profile.json"
    if path.is_symlink():
        raise RuntimeError("Company profile path must not be a symbolic link.")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return CompanyProfile(
            company_name=settings.app_name,
            industry="General",
            brand_name=settings.app_name,
            primary_color="#146B61",
            setup_complete=False,
        )
    if len(raw) > 8192:
        raise RuntimeError("Company profile is larger than the supported limit.")
    return CompanyProfile.model_validate_json(raw)


def save_company_profile(settings: Settings, profile: CompanyProfile) -> None:
    _atomic_json(_data_dir(settings) / "company-profile.json", profile.model_dump(mode="json"))


def _canonical_payload(claims: LicenseClaims) -> bytes:
    return json.dumps(
        claims.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _public_key(settings: Settings) -> Ed25519PublicKey | None:
    if not settings.license_public_key:
        return None
    try:
        raw = base64.b64decode(settings.license_public_key, validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (binascii.Error, ValueError):
        return None


def _verified_claims(settings: Settings, license: SignedLicense, *, now: datetime | None = None) -> LicenseClaims:
    public_key = _public_key(settings)
    if public_key is None:
        raise ValueError("Offline license verification is not configured.")
    try:
        signature = base64.b64decode(license.signature, validate=True)
        public_key.verify(signature, _canonical_payload(license.payload))
    except (binascii.Error, ValueError, InvalidSignature):
        raise ValueError("The license signature is invalid.") from None
    if license.payload.deployment_id != deployment_id(settings):
        raise ValueError("The license was issued for a different deployment.")
    current = now or datetime.now(UTC)
    if license.payload.issued_at > current + timedelta(minutes=5):
        raise ValueError("The license issue date is in the future.")
    return license.payload


def validate_license(settings: Settings, license: SignedLicense, *, now: datetime | None = None) -> LicenseClaims:
    claims = _verified_claims(settings, license, now=now)
    current = now or datetime.now(UTC)
    if claims.expires_at <= current:
        raise ValueError("The license has expired.")
    return claims


def save_license(settings: Settings, license: SignedLicense) -> LicenseClaims:
    claims = validate_license(settings, license)
    _atomic_json(_data_dir(settings) / "license.json", license.model_dump(mode="json"))
    return claims


def license_status(settings: Settings) -> LicenseStatus:
    identity = deployment_id(settings)
    if not settings.license_public_key:
        return LicenseStatus(status="not_configured", enforcement_enabled=settings.license_enforcement_enabled,
                             deployment_id=identity)
    path = _data_dir(settings) / "license.json"
    if path.is_symlink():
        return LicenseStatus(status="invalid", enforcement_enabled=settings.license_enforcement_enabled,
                             deployment_id=identity)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return LicenseStatus(status="missing", enforcement_enabled=settings.license_enforcement_enabled,
                             deployment_id=identity)
    if len(raw) > _MAX_LICENSE_BYTES:
        return LicenseStatus(status="invalid", enforcement_enabled=settings.license_enforcement_enabled,
                             deployment_id=identity)
    try:
        license = SignedLicense.model_validate_json(raw)
        claims = _verified_claims(settings, license)
    except (ValueError, OSError):
        return LicenseStatus(status="invalid", enforcement_enabled=settings.license_enforcement_enabled,
                             deployment_id=identity)
    if claims.expires_at <= datetime.now(UTC):
        return LicenseStatus(status="expired", enforcement_enabled=settings.license_enforcement_enabled,
                             deployment_id=identity)
    return LicenseStatus(
        status="valid",
        enforcement_enabled=settings.license_enforcement_enabled,
        deployment_id=identity,
        license_id=claims.license_id,
        customer=claims.customer,
        expires_at=claims.expires_at,
        seat_limit=claims.seat_limit,
        features=claims.features,
    )


def license_claims(settings: Settings) -> LicenseClaims | None:
    """Return verified active claims for server-side entitlements such as seat limits."""
    if not settings.license_public_key:
        return None
    try:
        path = _data_dir(settings) / "license.json"
        if path.is_symlink():
            return None
        raw = path.read_bytes()
        if len(raw) > _MAX_LICENSE_BYTES:
            return None
        return validate_license(settings, SignedLicense.model_validate_json(raw))
    except (OSError, ValueError):
        return None
