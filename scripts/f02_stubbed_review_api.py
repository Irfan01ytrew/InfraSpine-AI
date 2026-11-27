"""Loopback-only F02 review API using a deterministic, source-copying LLM test double."""

from __future__ import annotations

import json
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import app
from app.services.llm import ChatResponse


class MeetingDraftTestClient:
    provider = "local-test-double"

    async def chat(self, message: str, *, system_prompt: str | None = None) -> ChatResponse:
        if not system_prompt or "draft minutes" not in system_prompt.casefold():
            raise RuntimeError("The F02 review API accepts only the meeting-draft test request.")
        try:
            transcript = json.loads(message.rsplit("\n\n", 1)[1])
        except (IndexError, json.JSONDecodeError):
            raise RuntimeError("The F02 test double received an invalid transcript envelope.") from None
        if not isinstance(transcript, list) or not transcript:
            raise RuntimeError("The F02 test double requires transcript evidence.")

        summary: list[dict] = []
        decisions: list[dict] = []
        for segment in transcript:
            if not isinstance(segment, dict) or not isinstance(segment.get("chunk_id"), int):
                continue
            sentences = [
                item.strip()
                for item in re.split(r"(?<=[.!?])\s+", str(segment.get("text", "")))
                if item.strip()
            ]
            if sentences and not summary:
                summary.append({"text": sentences[0], "evidence_chunk_ids": [segment["chunk_id"]]})
            if len(sentences) > 1 and not decisions:
                decisions.append({"text": sentences[1], "evidence_chunk_ids": [segment["chunk_id"]]})
        if not summary:
            raise RuntimeError("The F02 test double could not find a source sentence to copy.")
        return ChatResponse(
            reply=json.dumps({"summary": summary, "decisions": decisions}, ensure_ascii=False),
            model="f02-deterministic-test-double",
            provider=self.provider,
            usage=None,
        )

    async def verify(self, timeout_seconds: float) -> None:
        return None

    async def close(self) -> None:
        return None


original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def review_lifespan(application: FastAPI):
    async with original_lifespan(application):
        async def get_test_client(_db, choice=None):
            if choice is not None:
                raise RuntimeError("Model selection is not part of this F02 acceptance.")
            return MeetingDraftTestClient()

        application.state.llm_runtime.get_client = get_test_client
        yield


app.router.lifespan_context = review_lifespan


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=63007)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
