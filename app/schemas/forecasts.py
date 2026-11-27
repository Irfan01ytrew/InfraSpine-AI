"""Schemas for reproducible, source-backed what-if scenarios."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ForecastObservationIn(BaseModel):
    period: str = Field(min_length=1, max_length=48)
    value: Decimal
    document_id: UUID
    excerpt: str = Field(min_length=8, max_length=800)

    @field_validator("period", "excerpt")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank.")
        return value

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or abs(value) > Decimal("1000000000000000000"):
            raise ValueError("The numeric value is outside the supported range.")
        return value


class ForecastScenarioCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    metric: str = Field(min_length=1, max_length=100)
    unit: str = Field(min_length=1, max_length=32)
    period_unit: Literal["period", "month", "quarter", "year"] = "period"
    observations: list[ForecastObservationIn] = Field(min_length=2, max_length=24)
    growth_rate_pct: Decimal
    additive_adjustment: Decimal = Decimal("0")
    horizon_periods: int = Field(ge=1, le=36)
    assumption_notes: str = Field(default="", max_length=1000)

    @field_validator("title", "metric", "unit")
    @classmethod
    def required_text(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("This field cannot be blank.")
        return value

    @field_validator("assumption_notes")
    @classmethod
    def normalize_notes(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("growth_rate_pct")
    @classmethod
    def valid_growth_rate(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or not Decimal("-100") <= value <= Decimal("500"):
            raise ValueError("Growth assumption must be a finite value from -100% to 500% per period.")
        return value

    @field_validator("additive_adjustment")
    @classmethod
    def valid_adjustment(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or abs(value) > Decimal("1000000000000000000"):
            raise ValueError("The adjustment is outside the supported numeric range.")
        return value


class ForecastCitationOut(BaseModel):
    document_id: UUID
    filename: str
    document_version: int
    chunk_id: int
    page_number: int
    locator: dict | None
    excerpt: str


class ForecastObservationOut(BaseModel):
    period: str
    value: Decimal
    citation: ForecastCitationOut


class ForecastProjectionOut(BaseModel):
    period: str
    value: Decimal


class ForecastAssumptionsOut(BaseModel):
    growth_rate_pct: Decimal
    additive_adjustment: Decimal
    horizon_periods: int
    period_unit: Literal["period", "month", "quarter", "year"]
    notes: str


class ForecastScenarioOut(BaseModel):
    id: UUID
    title: str
    metric: str
    unit: str
    model_version: str
    input_digest: str
    observations: list[ForecastObservationOut]
    assumptions: ForecastAssumptionsOut
    projections: list[ForecastProjectionOut]
    created_at: datetime

    model_config = {"from_attributes": True}


class ForecastScenarioSummaryOut(BaseModel):
    id: UUID
    title: str
    metric: str
    unit: str
    model_version: str
    input_digest: str
    observation_count: int
    citation_count: int
    assumptions: ForecastAssumptionsOut
    projections: list[ForecastProjectionOut]
    created_at: datetime


class ForecastScenarioListOut(BaseModel):
    items: list[ForecastScenarioSummaryOut]
