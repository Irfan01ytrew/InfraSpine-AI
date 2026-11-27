import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class HandoffCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    department_id: uuid.UUID
    title: str = Field(..., min_length=1, max_length=180)
    body: str = Field(..., min_length=1, max_length=12000)


class HandoffOut(BaseModel):
    id: uuid.UUID
    author_email: str
    department_id: uuid.UUID
    department_name: str
    title: str
    body: str
    status: str
    created_at: datetime
    acknowledged_by: str | None
    acknowledged_at: datetime | None


class HandoffListResponse(BaseModel):
    items: list[HandoffOut]
