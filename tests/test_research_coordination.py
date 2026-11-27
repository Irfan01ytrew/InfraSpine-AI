"""Route-level checks for bounded, ACL-preserving specialist research without a database."""

import asyncio
import uuid
from types import SimpleNamespace

from app.api.routes import rag as rag_routes
from app.rag.research import SPECIALIST_PROMPTS
from app.schemas.chat import ChatResponse
from app.schemas.rag import RagResearchRequest


class FakeDatabase:
    def __init__(self):
        self.committed = False
        self.rolled_back = False

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class FakeSettings:
    rag_trace_enabled = False

    @staticmethod
    def model_copy(*, update):
        return SimpleNamespace(**update)


class FakePlanner:
    async def chat(self, message, *, system_prompt=None):
        assert message == "Compare access rules and exceptions."
        assert "bounded research coordinator" in system_prompt
        return ChatResponse(
            reply=(
                '{"assignments":['
                '{"specialist":"policy_analyst","question":"Which access rules apply?"},'
                '{"specialist":"exception_checker","question":"What exceptions are stated?"}]}'
            ),
            model="synthetic-planner",
            provider="test",
        )


def test_research_route_keeps_every_specialist_inside_the_requester_scope(monkeypatch):
    database = FakeDatabase()
    planner = FakePlanner()
    document_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4())
    rag = object()
    calls = []

    class Runtime:
        async def get_client(self, db, model_choice):
            assert db is database
            assert model_choice is None
            return planner

    async def fake_respond(db, caller, components, settings, get_llm, question, **kwargs):
        calls.append((db, caller, components, settings, question, kwargs))
        return SimpleNamespace(
            answer=f"Supported answer for {question}.",
            insufficient_evidence=False,
            citations=[],
            retrieved_chunks=2,
            model="synthetic-answerer",
            provider="test",
        )

    monkeypatch.setattr(rag_routes, "rag_respond", fake_respond)
    app = SimpleNamespace(
        state=SimpleNamespace(sessionmaker=object(), llm_runtime=Runtime())
    )
    request = SimpleNamespace(app=app, state=SimpleNamespace(request_id="synthetic-request"))
    payload = RagResearchRequest(
        question="Compare access rules and exceptions.",
        document_ids=[document_id],
    )

    report = asyncio.run(
        rag_routes.research_documents(
            payload=payload,
            request=request,
            db=database,
            user=user,
            settings=FakeSettings(),
            rag=rag,
        )
    )

    assert database.committed
    assert not database.rolled_back
    assert len(calls) == 2
    assert [finding.specialist for finding in report.findings] == ["policy_analyst", "exception_checker"]
    for call, finding in zip(calls, report.findings, strict=True):
        db_arg, caller, components, settings, question, kwargs = call
        assert db_arg is database
        assert caller is user
        assert components is rag
        assert settings.rag_general_answers is False
        assert question == finding.question
        assert kwargs["history"] == []
        assert kwargs["document_ids"] == [document_id]
        assert kwargs["focus_document_ids"] == [document_id]
        assert kwargs["system_prompt_addendum"] == SPECIALIST_PROMPTS[finding.specialist]
