"""Owner-scoped reproducible forecasts with live source ACL and version checks."""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError, NotFoundError
from app.db.models import Document, DocumentChunk, ForecastScenario, User
from app.rag.access import readable_by
from app.rag.forecast import MODEL_VERSION, calculate_projection, numeric_tokens, parse_finite_decimal
from app.schemas.forecasts import ForecastScenarioCreate
from app.services import audit
from app.services.audit import AuditContext


def _normalize_number(value: Decimal) -> str:
    return format(value, "f")


async def create_scenario(
    db: AsyncSession,
    user: User,
    payload: ForecastScenarioCreate,
    *,
    ctx: AuditContext,
) -> ForecastScenario:
    if not user.is_active:
        raise NotFoundError("Forecast scenario not found.")
    if len({item.period for item in payload.observations}) != len(payload.observations):
        raise InvalidInputError("Historical periods must be unique.")

    observations: list[dict] = []
    calculation_inputs: list[tuple[str, Decimal]] = []
    for item in payload.observations:
        value = parse_finite_decimal(item.value)
        if item.period not in item.excerpt:
            raise InvalidInputError("Each cited passage must contain its historical period.")
        if value not in numeric_tokens(item.excerpt):
            raise InvalidInputError("Each historical value must appear in its cited passage.")
        document = await db.scalar(
            select(Document).where(
                Document.id == item.document_id,
                readable_by(user),
                Document.status == "ready",
                Document.indexed_version == Document.version,
            )
        )
        if document is None:
            raise NotFoundError("Forecast source not found.")
        chunk = await db.scalar(
            select(DocumentChunk)
            .where(
                DocumentChunk.document_id == document.id,
                DocumentChunk.content.contains(item.excerpt, autoescape=True),
            )
            .order_by(DocumentChunk.chunk_index)
            .limit(1)
        )
        if chunk is None:
            raise InvalidInputError("The cited passage must exactly match text in the selected source chunk.")
        if item.excerpt not in chunk.content:
            raise InvalidInputError("The cited passage must exactly match text in the selected source chunk.")
        citation = {
            "document_id": str(document.id),
            "filename": document.filename,
            "document_version": document.version,
            "document_sha256": document.sha256,
            "chunk_id": chunk.id,
            "page_number": chunk.page_number,
            "locator": chunk.locator,
            "excerpt": item.excerpt,
        }
        observations.append({"period": item.period, "value": _normalize_number(value), "citation": citation})
        calculation_inputs.append((item.period, value))

    growth = parse_finite_decimal(payload.growth_rate_pct, maximum_abs=Decimal("500"))
    adjustment = parse_finite_decimal(payload.additive_adjustment)
    try:
        projections = calculate_projection(
            calculation_inputs,
            growth_rate_pct=growth,
            additive_adjustment=adjustment,
            horizon_periods=payload.horizon_periods,
            period_unit=payload.period_unit,
        )
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from None

    assumptions = {
        "growth_rate_pct": _normalize_number(growth),
        "additive_adjustment": _normalize_number(adjustment),
        "horizon_periods": payload.horizon_periods,
        "period_unit": payload.period_unit,
        "notes": payload.assumption_notes,
    }
    canonical = {
        "title": payload.title,
        "metric": payload.metric,
        "unit": payload.unit,
        "period_unit": payload.period_unit,
        "observations": observations,
        "assumptions": assumptions,
        "model_version": MODEL_VERSION,
    }
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    scenario = ForecastScenario(
        owner_user_id=user.id,
        title=payload.title,
        metric=payload.metric,
        unit=payload.unit,
        period_unit=payload.period_unit,
        horizon_periods=payload.horizon_periods,
        model_version=MODEL_VERSION,
        input_digest=digest,
        observations=observations,
        assumptions=assumptions,
        projections=projections,
    )
    db.add(scenario)
    await db.flush()
    audit.record(
        db,
        ctx,
        "forecast.scenario_created",
        target=str(scenario.id),
        details={
            "model_version": MODEL_VERSION,
            "history_points": len(observations),
            "source_count": len({item["citation"]["document_id"] for item in observations}),
            "horizon_periods": payload.horizon_periods,
        },
    )
    await db.commit()
    await db.refresh(scenario)
    return scenario


async def _current_sources(db: AsyncSession, user: User, scenarios: list[ForecastScenario]) -> set[tuple[str, int, str]]:
    references = {
        (item["citation"]["document_id"], item["citation"]["document_version"], item["citation"]["document_sha256"])
        for scenario in scenarios
        for item in scenario.observations
    }
    if not references:
        return set()
    ids = {uuid.UUID(document_id) for document_id, _, _ in references}
    docs = list(
        (
            await db.scalars(
                select(Document).where(
                    Document.id.in_(ids),
                    readable_by(user),
                    Document.status == "ready",
                    Document.indexed_version == Document.version,
                )
            )
        ).all()
    )
    valid = {
        (str(document.id), document.version, document.sha256)
        for document in docs
    }
    return references.intersection(valid)


def _scenario_is_current(scenario: ForecastScenario, current_sources: set[tuple[str, int, str]]) -> bool:
    citations = {
        (
            item["citation"]["document_id"],
            item["citation"]["document_version"],
            item["citation"]["document_sha256"],
        )
        for item in scenario.observations
    }
    return bool(citations) and citations.issubset(current_sources)


async def list_scenarios(db: AsyncSession, user: User) -> list[ForecastScenario]:
    if not user.is_active:
        return []
    rows = list(
        (
            await db.scalars(
                select(ForecastScenario)
                .where(ForecastScenario.owner_user_id == user.id)
                .order_by(ForecastScenario.created_at.desc())
                .limit(100)
            )
        ).all()
    )
    current_sources = await _current_sources(db, user, rows)
    return [scenario for scenario in rows if _scenario_is_current(scenario, current_sources)]


async def get_scenario(db: AsyncSession, user: User, scenario_id: uuid.UUID) -> ForecastScenario:
    scenario = await db.scalar(
        select(ForecastScenario).where(ForecastScenario.id == scenario_id, ForecastScenario.owner_user_id == user.id)
    )
    if scenario is None or not user.is_active:
        raise NotFoundError("Forecast scenario not found.")
    current_sources = await _current_sources(db, user, [scenario])
    if not _scenario_is_current(scenario, current_sources):
        raise NotFoundError("Forecast scenario not found.")
    return scenario
