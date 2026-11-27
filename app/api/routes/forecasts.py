"""Source-cited, owner-scoped deterministic forecast scenarios."""

import uuid

from fastapi import APIRouter, Depends, Request

from app.api.deps import CurrentUser, DbSession, audit_context, get_current_user
from app.db.models import ForecastScenario
from app.schemas.forecasts import ForecastScenarioCreate, ForecastScenarioListOut, ForecastScenarioOut, ForecastScenarioSummaryOut
from app.services import forecasts

router = APIRouter(prefix="/api/forecasts", tags=["forecasts"], dependencies=[Depends(get_current_user)])


def _summary(scenario: ForecastScenario) -> ForecastScenarioSummaryOut:
    return ForecastScenarioSummaryOut(
        id=scenario.id,
        title=scenario.title,
        metric=scenario.metric,
        unit=scenario.unit,
        model_version=scenario.model_version,
        input_digest=scenario.input_digest,
        observation_count=len(scenario.observations),
        citation_count=len(scenario.observations),
        assumptions=scenario.assumptions,
        projections=scenario.projections,
        created_at=scenario.created_at,
    )


@router.post("", response_model=ForecastScenarioOut, status_code=201)
async def create_scenario(
    payload: ForecastScenarioCreate,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> ForecastScenarioOut:
    scenario = await forecasts.create_scenario(db, user, payload, ctx=audit_context(request, user))
    return ForecastScenarioOut.model_validate(scenario)


@router.get("", response_model=ForecastScenarioListOut)
async def list_scenarios(db: DbSession, user: CurrentUser) -> ForecastScenarioListOut:
    return ForecastScenarioListOut(items=[_summary(row) for row in await forecasts.list_scenarios(db, user)])


@router.get("/{scenario_id}", response_model=ForecastScenarioOut)
async def get_scenario(scenario_id: uuid.UUID, db: DbSession, user: CurrentUser) -> ForecastScenarioOut:
    scenario = await forecasts.get_scenario(db, user, scenario_id)
    return ForecastScenarioOut.model_validate(scenario)
