"""Schemas for employee-declared, department-scoped expertise discovery."""

from __future__ import annotations

import unicodedata
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


def normalize_skill(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


class ExpertiseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tags: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(default_factory=list, max_length=20)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            if any(unicodedata.category(character) == "Cc" for character in value):
                raise ValueError("Expertise tags cannot contain control characters.")
            tag = normalize_skill(value)
            if not tag:
                raise ValueError("Expertise tags cannot be empty.")
            if tag in seen:
                raise ValueError("Expertise tags must be unique, ignoring letter case.")
            seen.add(tag)
            normalized.append(tag)
        return normalized


class ExpertiseProfile(BaseModel):
    user_id: str
    tags: list[str]


class ExpertiseMatch(BaseModel):
    user_id: str
    email: str
    tags: list[str]
    shared_departments: list[str]


class ExpertiseSearchResponse(BaseModel):
    skill: str
    matches: list[ExpertiseMatch]
    next_cursor: str | None = None
