"""Per-user language preference (Step Z slice 3).

Storage + resolution only. The preference is read through the normal
session user (``CurrentUser``) — routes below only ever touch the caller's
own row, so one user can neither see nor change another user's preference.
A preference never changes authorization, permissions, approvals, or what
a user may read; it only changes the language replies are written in.

Resolution rule (``resolve_reply_language``): an explicit preference wins;
when nothing is stored, the detected language wins; when detection finds
no language, the default is English. ``prefer_roman_urdu`` applies the
same rule to the Roman-Urdu yes/no decisions in ``app/rag/chat.py``:
``roman_urdu`` forces Roman Urdu, ``en`` forces English, ``urdu`` and
unset fall back to per-question detection (no Urdu-script reply templates
exist yet — slice 4).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserLanguagePreference

LANGUAGE_CHOICES = ("en", "roman_urdu", "urdu")
DEFAULT_LANGUAGE = "en"


async def get_language_preference(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    """Stored preference for *user_id*, or ``None`` when the user never set one."""
    preference = await db.get(UserLanguagePreference, user_id)
    return preference.language if preference else None


async def set_language_preference(db: AsyncSession, user_id: uuid.UUID, language: str) -> str:
    """Store *language* for *user_id*. Raises ``ValueError`` for anything outside ``LANGUAGE_CHOICES``."""
    if language not in LANGUAGE_CHOICES:
        raise ValueError(f"Unknown language: {language!r}")
    await db.execute(
        insert(UserLanguagePreference)
        .values(user_id=user_id, language=language)
        .on_conflict_do_update(
            index_elements=[UserLanguagePreference.user_id],
            set_={"language": language, "updated_at": datetime.now(UTC)},
        )
    )
    await db.commit()
    return language


def resolve_reply_language(preference: str | None, detected: str) -> str:
    """Preference first, detected language second, English when neither says."""
    if preference in LANGUAGE_CHOICES:
        return preference
    if detected and detected != "unknown":
        return detected
    return DEFAULT_LANGUAGE


def prefer_roman_urdu(preference: str | None, detected: bool) -> bool:
    """Apply *preference* to a Roman-Urdu yes/no reply decision.

    ``roman_urdu`` forces Roman Urdu, ``en`` forces English, ``urdu`` and
    unset keep the per-question *detected* value.
    """
    if preference == "roman_urdu":
        return True
    if preference == "en":
        return False
    return detected
