"""Request and response shapes for the bounded business database connector."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ColumnType = Literal["text", "integer", "numeric", "date", "timestamp", "boolean"]
AnalysisOperation = Literal["rows", "count", "sum", "mean", "min", "max", "count_distinct"]
FilterOperation = Literal["eq", "ne", "contains", "gt", "gte", "lt", "lte"]


class DatabaseAnalysisFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1, max_length=63)
    operation: FilterOperation
    value: str = Field(max_length=500)


class DatabaseAnalysisQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: str = Field(min_length=3, max_length=127)
    operation: AnalysisOperation
    value: str | None = Field(default=None, max_length=63)
    group_by: list[str] = Field(default_factory=list, max_length=2)
    filters: list[DatabaseAnalysisFilter] = Field(default_factory=list, max_length=5)
    limit: int = Field(default=50, ge=1, le=100)


class DatabaseAnalysisColumn(BaseModel):
    name: str
    type: ColumnType


class DatabaseAnalysisTable(BaseModel):
    name: str
    columns: list[DatabaseAnalysisColumn]


class DatabaseAnalysisCatalog(BaseModel):
    enabled: bool
    source_label: str | None = None
    tables: list[DatabaseAnalysisTable] = Field(default_factory=list)
    message: str | None = None


class DatabaseAnalysisResult(BaseModel):
    source_label: str
    table: str
    retrieved_at: str
    operation: AnalysisOperation
    columns: list[str]
    rows: list[list[str]]
    returned_rows: int
    row_limit: int
    truncated: bool
    provenance: str
