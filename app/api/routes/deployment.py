"""Per-installation company setup and signed license administration."""

from fastapi import APIRouter, Depends, Request

from app.api.deps import AdminUser, AppSettings, DbSession, audit_context, require_admin
from app.core.errors import InvalidInputError
from app.schemas.deployment import (
    CompanyProfile,
    IndustryTemplateApplyOut,
    IndustryTemplateOut,
    LicenseStatus,
    SignedLicense,
)
from app.services import audit
from app.services.deployment import get_company_profile, license_status, save_company_profile, save_license
from app.services.deployment_templates import (
    apply_industry_template,
    list_industry_templates,
    validate_profile_template,
)

public_router = APIRouter(prefix="/api/deployment", tags=["deployment"])


@public_router.get("/profile", response_model=CompanyProfile)
async def public_company_profile(settings: AppSettings) -> CompanyProfile:
    """Branding shown before sign-in; contains no customer records, license data or secrets."""
    return get_company_profile(settings)


router = APIRouter(
    prefix="/api/admin/deployment",
    tags=["deployment"],
    dependencies=[Depends(require_admin)],
)


class DeploymentOverview(CompanyProfile):
    license: LicenseStatus


@router.get("", response_model=DeploymentOverview)
async def get_overview(settings: AppSettings) -> DeploymentOverview:
    profile = get_company_profile(settings)
    return DeploymentOverview(**profile.model_dump(), license=license_status(settings))


@router.get("/templates", response_model=list[IndustryTemplateOut])
async def industry_templates() -> list[IndustryTemplateOut]:
    """Return versioned, customer-neutral starter templates for Admin setup."""
    return list_industry_templates()


@router.post("/templates/{template_id}/apply", response_model=IndustryTemplateApplyOut)
async def apply_template(
    template_id: str,
    db: DbSession,
    admin: AdminUser,
    request: Request,
) -> IndustryTemplateApplyOut:
    """Create missing empty local departments; no membership or ACL is assigned."""
    return await apply_industry_template(db, admin, audit_context(request, admin), template_id)


@router.put("/company", response_model=CompanyProfile)
async def update_company(
    payload: CompanyProfile,
    settings: AppSettings,
    db: DbSession,
    admin: AdminUser,
    request: Request,
) -> CompanyProfile:
    profile = payload.model_copy(update={"setup_complete": True})
    try:
        validate_profile_template(profile.template_id, profile.template_version)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from None
    save_company_profile(settings, profile)
    audit.record(
        db,
        audit_context(request, admin),
        "deployment.company_profile_updated",
        target=profile.company_name,
        details={
            "industry": profile.industry,
            "brand_name": profile.brand_name,
            "currency": profile.currency,
            "time_zone": profile.time_zone,
            "locale": profile.locale,
        },
    )
    await db.commit()
    return profile


@router.put("/license", response_model=LicenseStatus)
async def activate_license(
    payload: SignedLicense,
    settings: AppSettings,
    db: DbSession,
    admin: AdminUser,
    request: Request,
) -> LicenseStatus:
    try:
        claims = save_license(settings, payload)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from None
    audit.record(
        db,
        audit_context(request, admin),
        "deployment.license_installed",
        target=claims.license_id,
        details={"customer": claims.customer, "expires_at": claims.expires_at.isoformat()},
    )
    await db.commit()
    return license_status(settings)
