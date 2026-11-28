"""Per-user language preference API (Step Z slice 3).

Both endpoints only ever touch the caller's own row (``CurrentUser``):
there is no user id in the path, so one user can neither see nor change
another user's preference. A preference never changes authorization,
permissions, approvals, or what a user may read.
"""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession, get_current_user
from app.language.preference import (
    DEFAULT_LANGUAGE,
    get_language_preference,
    set_language_preference,
)

router = APIRouter(prefix="/api/language", tags=["language"], dependencies=[Depends(get_current_user)])


class LanguagePreferenceIn(BaseModel):
    language: Literal["en", "roman_urdu", "urdu"]


class LanguagePreferenceOut(BaseModel):
    language: str
    explicit: bool  # False when nothing is stored and the default applies


@router.get("/preference", response_model=LanguagePreferenceOut)
async def get_preference(db: DbSession, user: CurrentUser) -> LanguagePreferenceOut:
    stored = await get_language_preference(db, user.id)
    return LanguagePreferenceOut(
        language=stored if stored is not None else DEFAULT_LANGUAGE,
        explicit=stored is not None,
    )


@router.put("/preference", response_model=LanguagePreferenceOut)
async def update_preference(
    payload: LanguagePreferenceIn, db: DbSession, user: CurrentUser
) -> LanguagePreferenceOut:
    language = await set_language_preference(db, user.id, payload.language)
    return LanguagePreferenceOut(language=language, explicit=True)
