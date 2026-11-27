"""Management reports expose aggregate usage metadata only; questions and answers stay private."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy import func, select

from app.api.deps import AppSettings, DbSession, require_admin, require_reports_viewer
from app.api.routes.admin_traces import _summary
from app.db.models import ConversationMessage, RagTrace
from app.schemas.deployment import CompanyProfile
from app.schemas.reports import (
    DailyUsage,
    DepartmentQuotaUpdate,
    DepartmentUsageQuota,
    OperationalHealthDashboard,
    ProviderCostDetail,
    ProviderUsage,
    QuotaAlert,
    UsageReport,
)
from app.services.deployment import get_company_profile
from app.services.operational_health import (
    get_department_quotas,
    get_operational_dashboard,
    get_provider_costs,
    set_department_quota,
)
from app.services.report_export import ReportFormat, render_usage_export

# Reading reports is delegable (reports.view); changing quotas stays administrator-only (see set_quota).
router = APIRouter(prefix="/api/admin/reports", tags=["admin-reports"], dependencies=[Depends(require_reports_viewer)])


@router.get("/usage", response_model=UsageReport)
async def usage_report(
    db: DbSession,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> UsageReport:
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    window = RagTrace.created_at >= start
    daily_rows = (
        await db.execute(
            select(
                func.date(RagTrace.created_at),
                func.count(),
                func.count().filter(RagTrace.status == "error"),
                func.count().filter(RagTrace.insufficient_evidence.is_(True)),
                func.count().filter(ConversationMessage.feedback == 1),
                func.count().filter(ConversationMessage.feedback == -1),
            )
            .select_from(RagTrace)
            .outerjoin(ConversationMessage, ConversationMessage.id == RagTrace.message_id)
            .where(window)
            .group_by(func.date(RagTrace.created_at))
            .order_by(func.date(RagTrace.created_at))
        )
    ).all()
    provider_rows = (
        await db.execute(
            select(
                RagTrace.provider,
                RagTrace.model,
                func.coalesce(func.sum(RagTrace.llm_calls), 0),
                func.percentile_cont(0.5).within_group(RagTrace.latency_ms),
                func.percentile_cont(0.95).within_group(RagTrace.latency_ms),
            )
            .where(window, RagTrace.provider.is_not(None))
            .group_by(RagTrace.provider, RagTrace.model)
            .order_by(func.count().desc(), RagTrace.provider, RagTrace.model)
        )
    ).all()
    return UsageReport(
        generated_at=end,
        period_start=start,
        period_end=end,
        summary=await _summary(db, window, days),
        daily=[
            DailyUsage(day=day, questions=count, errors=errors, refused=refused, helpful=helpful, not_helpful=not_helpful)
            for day, count, errors, refused, helpful, not_helpful in daily_rows
        ],
        providers=[
            ProviderUsage(
                provider=provider,
                model=model,
                calls=calls,
                latency_p50_ms=round(p50, 1) if p50 is not None else None,
                latency_p95_ms=round(p95, 1) if p95 is not None else None,
            )
            for provider, model, calls, p50, p95 in provider_rows
        ],
    )


@router.get("/usage/export")
async def export_usage_report(
    db: DbSession,
    settings: AppSettings,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    format: Annotated[ReportFormat, Query()] = "pdf",
) -> Response:
    """Download an editable aggregate report without conversation or source-document content."""
    report = await usage_report(db, days)
    profile: CompanyProfile = get_company_profile(settings)
    body, media_type, filename = render_usage_export(report, profile, format)
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/operational-health", response_model=OperationalHealthDashboard)
async def operational_health(
    db: DbSession,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> OperationalHealthDashboard:
    """Enterprise operational health dashboard with spend attribution, quotas, and active alerts."""
    return await get_operational_dashboard(db, days=days)


@router.get("/quotas", response_model=list[DepartmentUsageQuota])
async def department_quotas(
    db: DbSession,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> list[DepartmentUsageQuota]:
    """Retrieve all department quotas, utilization rates, and threshold alerts."""
    quotas, _ = await get_department_quotas(db, days=days)
    return quotas


@router.put("/quotas/{department_id}", dependencies=[Depends(require_admin)])
async def update_department_quota(
    department_id: Annotated[uuid.UUID, Path()],
    update: DepartmentQuotaUpdate,
) -> dict[str, str]:
    """Configure or update the monthly token quota and alert threshold for a department."""
    set_department_quota(
        department_id=department_id,
        monthly_token_quota=update.monthly_token_quota,
        alert_threshold_percent=update.alert_threshold_percent,
    )
    return {"status": "ok", "message": f"Quota updated for department {department_id}"}

