"""End-to-end web-research path for the user's exact question, with the network stubbed.

"pak a ka next match kb hai?" must produce a sourced web answer: the routing fix
sends it to public web search, and the query fix turns it into the search query
"pak next match" (not "pak next kb"). Live providers are bot-blocked from this VM
(DuckDuckGo answers 202 with a captcha page), so the providers and page fetch are
stubbed here; the query construction, candidate matching, excerpting and synthesis
path are exercised for real.
"""

import asyncio
import base64
import os

import pytest

from app.core.config import Settings
from app.rag.chat import _web_context
from app.services import public_web
from app.services.public_web import _public_search_queries, public_web_research


@pytest.fixture()
def settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:5432/infraspine_ai")
    monkeypatch.setenv("MASTER_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    return Settings()


class StubLLM:
    async def chat(self, message, system_prompt=None):
        class Resp:
            reply = "Pakistan's next match is against India on 15 February 2027 [S1]."
            model = "stub"
            provider = "stub"

        return Resp()


def test_next_match_question_builds_a_topical_search_query(settings):
    prompt = _web_context("pak a ka next match kb hai?", [], [])
    assert _public_search_queries(prompt) == ["pak next match"]


def test_next_match_question_returns_sourced_web_answer(monkeypatch, settings):
    async def fake_search(_settings, query):
        assert query == "pak next match"
        return [{
            "title": "Pakistan next match schedule - PCB",
            "url": "https://www.pcb.com.pk/pakistan-next-match",
            "snippet": "Pakistan next match against India on 15 February 2027 at Gaddafi Stadium Lahore.",
        }]

    async def fake_page(_settings, url, max_bytes=None):
        body = (
            b"<html><head><title>Pakistan next match schedule - PCB</title></head><body>"
            b"<p>Pakistan next match is against India on 15 February 2027 at Gaddafi Stadium Lahore. "
            b"The Pakistan cricket team will play its next match in the series on that date, with tickets "
            b"available from the PCB box office and live coverage on national television.</p>"
            b"</body></html>"
        )
        return ("text/html", body)

    monkeypatch.setattr(public_web, "_search_duckduckgo", fake_search)
    monkeypatch.setattr(public_web, "_search_bing", lambda s, q: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(public_web, "_search_yahoo", lambda s, q: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(public_web, "_get_public_page", fake_page)

    prompt = _web_context("pak a ka next match kb hai?", [], [])
    result = asyncio.run(public_web_research(StubLLM(), settings, prompt))

    assert result["sources"], "expected at least one sourced page"
    assert result["sources"][0]["url"] == "https://www.pcb.com.pk/pakistan-next-match"
    assert "Pakistan" in result["answer"]
    assert "[S1]" in result["answer"]


def test_schedule_question_with_cricket_word_skips_live_scores_query(settings):
    """Integration regression (Step P + knowledge-fix): a schedule question that
    mentions cricket still ROUTES as a cricket lookup, but its topical query stands
    alone -- no "international cricket live scores today" prepend."""
    from app.services.public_web import is_current_cricket_request

    prompt = _web_context("pakistan cricket team ka next match kab hai?", [], [])
    assert is_current_cricket_request(prompt) is True
    assert _public_search_queries(prompt) == ["pakistan cricket team next match"]
