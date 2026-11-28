"""Step Z slice 3: per-user language preference.

Covers storage + API + per-user isolation (another user can neither see
nor change your preference) + the reply-language resolution rule + the
run_chat wiring (an explicit preference overrides per-question detection
at the Roman Urdu reply decisions; unset keeps detection).
"""

import pytest

from app.language.preference import (
    DEFAULT_LANGUAGE,
    prefer_roman_urdu,
    resolve_reply_language,
)
from tests.conftest import USER2_EMAIL, USER_EMAIL, login


# --- pure resolution rule ---------------------------------------------------

@pytest.mark.parametrize(
    "preference, detected, expected",
    [
        ("roman_urdu", "en", "roman_urdu"),  # explicit preference wins
        ("en", "roman_urdu", "en"),
        ("urdu", "roman_urdu", "urdu"),
        (None, "roman_urdu", "roman_urdu"),  # unset: detection wins
        (None, "en", "en"),
        (None, "unknown", "en"),  # neither says: default English
        (None, "mixed", "mixed"),
        ("bogus", "roman_urdu", "roman_urdu"),  # invalid stored value ignored
    ],
)
def test_resolve_reply_language(preference, detected, expected):
    assert resolve_reply_language(preference, detected) == expected


@pytest.mark.parametrize(
    "preference, detected, expected",
    [
        ("roman_urdu", False, True),  # forces Roman Urdu even for English text
        ("en", True, False),  # forces English even for Roman Urdu text
        ("urdu", True, True),  # no Urdu-script templates yet: detection stands
        ("urdu", False, False),
        (None, True, True),  # unset: detection stands
        (None, False, False),
    ],
)
def test_prefer_roman_urdu(preference, detected, expected):
    assert prefer_roman_urdu(preference, detected) == expected


# --- API: storage + per-user isolation ---------------------------------------

def test_preference_defaults_to_english(client):
    login(client, USER_EMAIL)
    assert client.get("/api/language/preference").json() == {
        "language": DEFAULT_LANGUAGE,
        "explicit": False,
    }


def test_preference_round_trip(client):
    login(client, USER_EMAIL)
    assert client.put("/api/language/preference", json={"language": "roman_urdu"}).json() == {
        "language": "roman_urdu",
        "explicit": True,
    }
    assert client.get("/api/language/preference").json() == {
        "language": "roman_urdu",
        "explicit": True,
    }
    assert client.put("/api/language/preference", json={"language": "urdu"}).json() == {
        "language": "urdu",
        "explicit": True,
    }
    assert client.get("/api/language/preference").json()["language"] == "urdu"


def test_preference_is_per_user(client):
    login(client, USER_EMAIL)
    assert client.put("/api/language/preference", json={"language": "roman_urdu"}).status_code == 200

    # Another user sees only their own (unset) preference...
    client.cookies.clear()
    login(client, USER2_EMAIL)
    assert client.get("/api/language/preference").json() == {
        "language": DEFAULT_LANGUAGE,
        "explicit": False,
    }
    # ...and changing theirs does not touch the first user's.
    assert client.put("/api/language/preference", json={"language": "en"}).status_code == 200

    client.cookies.clear()
    login(client, USER_EMAIL)
    assert client.get("/api/language/preference").json() == {
        "language": "roman_urdu",
        "explicit": True,
    }


def test_preference_rejects_unknown_language(client):
    login(client, USER_EMAIL)
    assert client.put("/api/language/preference", json={"language": "punjabi"}).status_code == 422
    assert client.get("/api/language/preference").json()["explicit"] is False


def test_preference_requires_login(client):
    assert client.get("/api/language/preference").status_code == 401
    assert client.put("/api/language/preference", json={"language": "roman_urdu"}).status_code == 401


# --- run_chat wiring: preference overrides detection -------------------------

_PORTAL: list = []


@pytest.fixture(autouse=True)
def _portal(client):
    _PORTAL[:] = [client.portal]
    yield
    _PORTAL.clear()


def _chat(email: str, question: str):
    """Production run_chat with a scripted model; returns the ChatResult."""
    from sqlalchemy import select

    from app.core.config import get_settings
    from app.db.models import User
    from app.rag.chat import run_chat
    from app.schemas.chat import ChatResponse
    from app.main import app

    class Scripted:
        async def chat(self, message, *, system_prompt=None):
            return ChatResponse(reply="NONE", model="m", provider="p")

    async def get_llm():
        return Scripted()

    async def go():
        async with app.state.sessionmaker() as db:
            user = (await db.execute(select(User).where(User.email == email))).scalar_one()
            return await run_chat(
                db, user, app.state.rag, get_settings(), get_llm, question,
                history=[], document_ids=None, focus_document_ids=None,
            )

    return _PORTAL[0].call(go)


def test_chat_inventory_intro_follows_detection_when_unset(client):
    login(client, USER_EMAIL)
    assert "Aap ki permissions wali library" in _chat(USER_EMAIL, "kitne documents hain?").answer.answer
    assert "Your authorized library" in _chat(USER_EMAIL, "how many documents do I have?").answer.answer


def test_chat_inventory_intro_follows_preference(client):
    login(client, USER_EMAIL)
    # English question, Roman Urdu preference -> Roman Urdu intro.
    assert client.put("/api/language/preference", json={"language": "roman_urdu"}).status_code == 200
    assert "Aap ki permissions wali library" in _chat(
        USER_EMAIL, "how many documents do I have?"
    ).answer.answer
    # Roman Urdu question, English preference -> English intro.
    assert client.put("/api/language/preference", json={"language": "en"}).status_code == 200
    assert "Your authorized library" in _chat(USER_EMAIL, "kitne documents hain?").answer.answer
