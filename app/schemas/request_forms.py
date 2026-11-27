import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db.form_models import RequestForm

FieldType = Literal["text", "long_text", "number", "date", "choice", "yes_no", "file"]
Category = Literal["it", "hr", "access", "facilities", "other", "purchase", "expense"]


class ShowIf(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # The key (or, for a field not saved yet, the label) of an earlier choice or yes/no field.
    field: str = Field(..., min_length=1, max_length=120)
    equals: str = Field(..., min_length=1, max_length=100)


class FormField(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str | None = Field(default=None, max_length=40)
    label: str = Field(..., min_length=1, max_length=120)
    type: FieldType
    required: bool = False
    options: list[str] = Field(default_factory=list, max_length=50)
    help: str | None = Field(default=None, max_length=200)
    show_if: ShowIf | None = None


class RequestFormIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    category: Category
    department_group_id: uuid.UUID | None = None
    fields: list[FormField] = Field(..., min_length=1, max_length=30)
    # Who may use the form; both empty means everyone.
    audience_department_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    audience_position_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)

    def as_kwargs(self) -> dict:
        return {"name": self.name, "description": self.description, "category": self.category,
                "department_group_id": self.department_group_id, "fields": [f.model_dump() for f in self.fields],
                "audience_department_ids": self.audience_department_ids, "audience_position_ids": self.audience_position_ids}


class AudienceEntry(BaseModel):
    id: uuid.UUID
    name: str


class RequestFormOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    category: Category
    department_group_id: uuid.UUID | None
    department_name: str | None
    fields: list[FormField]
    version: int
    updated_at: datetime
    audience_departments: list[AudienceEntry] = Field(default_factory=list)
    audience_positions: list[AudienceEntry] = Field(default_factory=list)


class RequestFormListResponse(BaseModel):
    forms: list[RequestFormOut]


def form_out(form: RequestForm, department_name: str | None, names: dict[str, str] | None = None) -> RequestFormOut:
    names = names or {}
    audience = form.audience or {}

    def entries(kind: str) -> list[AudienceEntry]:
        return [AudienceEntry(id=i, name=names.get(i, "Unavailable")) for i in audience.get(kind, [])]

    return RequestFormOut(
        id=form.id, name=form.name, description=form.description, category=form.category,
        department_group_id=form.department_group_id, department_name=department_name,
        fields=[FormField(**f) for f in form.fields], version=form.version, updated_at=form.updated_at,
        audience_departments=entries("departments"), audience_positions=entries("positions"),
    )
