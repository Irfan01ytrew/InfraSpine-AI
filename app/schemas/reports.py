from datetime import date, datetime

from pydantic import BaseModel

from app.schemas.traces import TraceSummary


class DailyUsage(BaseModel):
    day: date
    questions: int
    errors: int
    refused: int
    helpful: int
    not_helpful: int


class ProviderUsage(BaseModel):
    provider: str
    model: str | None
    calls: int
    latency_p50_ms: float | None
    latency_p95_ms: float | None


class UsageReport(BaseModel):
    generated_at: datetime
    period_start: datetime
    period_end: datetime
    summary: TraceSummary
    daily: list[DailyUsage]
    providers: list[ProviderUsage]


class ProviderCostDetail(BaseModel):
    provider: str
    model: str | None
    calls: int
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float


class DepartmentUsageQuota(BaseModel):
    department_id: str
    department_name: str
    questions_count: int
    token_count: int
    estimated_cost_usd: float
    monthly_token_quota: int
    utilization_percent: float
    alert_threshold_percent: float
    is_alert_triggered: bool
    alert_severity: str | None = None


class QuotaAlert(BaseModel):
    department_id: str
    department_name: str
    message: str
    severity: str
    utilization_percent: float


class OperationalHealthDashboard(BaseModel):
    system_status: str
    total_spend_usd: float
    total_tokens_used: int
    active_alerts: list[QuotaAlert]
    department_quotas: list[DepartmentUsageQuota]
    provider_costs: list[ProviderCostDetail]


class DepartmentQuotaUpdate(BaseModel):
    monthly_token_quota: int
    alert_threshold_percent: float = 80.0
