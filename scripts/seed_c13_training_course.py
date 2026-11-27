"""Seed one explicitly synthetic, exact-source C13 course for current browser acceptance."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select

from app.db.models import AccessGroup, AccessGroupMember, Document, DocumentChunk, ROLE_ADMIN, ROLE_EMPLOYEE, TrainingCourse, User
from app.db.session import create_engine, create_sessionmaker
from app.schemas.rag import Citation, TrainingModule, TrainingPlan, TrainingQuizQuestion


ROOT = Path(os.environ["INFRASPINE_C13_ROOT"]).resolve(strict=True)
DATABASE = "postgresql+asyncpg://postgres@127.0.0.1:55929/infraspine_c11dept_20261029_test"
GROUP_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SOURCE = "Recovery tokens must be rotated every 90 days. Verify the recovery owner before issuing a replacement token. Record the renewal date after rotation."
TOPIC = "Recovery token handling"


async def main() -> None:
    if os.environ.get("DATABASE_URL") != DATABASE:
        raise RuntimeError("Refusing to seed a non-disposable C11/C13 QA database.")
    worker = json.loads((ROOT / "evidence" / "c13-source-worker.json").read_text(encoding="utf-8"))
    if worker.get("result") != "PASS" or worker.get("chunkCount") != 1:
        raise RuntimeError("The exact synthetic source has not passed the one-document worker check.")
    engine = create_engine(DATABASE, pool_size=1, max_overflow=0)
    sessionmaker = create_sessionmaker(engine)
    try:
        async with sessionmaker() as db:
            document = await db.get(Document, UUID(worker["documentId"]))
            chunk = await db.scalar(select(DocumentChunk).where(DocumentChunk.document_id == UUID(worker["documentId"])))
            admin = await db.scalar(select(User).where(User.email == "c11-dept-admin@synthetic.test"))
            employee = await db.scalar(select(User).where(User.email == "c11-finance-employee@synthetic.test"))
            group = await db.get(AccessGroup, GROUP_ID)
            if not document or not chunk or not admin or not employee or not group:
                raise RuntimeError("The C13 isolated synthetic source or account fixture is incomplete.")
            if admin.role != ROLE_ADMIN or employee.role != ROLE_EMPLOYEE or document.status != "ready":
                raise RuntimeError("The C13 source or synthetic roles have unexpected state.")
            if document.sha256 != worker["sourceSha256"] or document.indexed_version != document.version:
                raise RuntimeError("The C13 source metadata no longer matches the worker evidence.")
            if any(sentence not in chunk.content for sentence in SOURCE.split(". ") if sentence):
                raise RuntimeError("C13 lesson wording is not present in the indexed source chunk.")
            existing = await db.scalar(select(func.count()).select_from(TrainingCourse).where(TrainingCourse.topic == TOPIC))
            if existing:
                raise RuntimeError("A C13 training course with this synthetic topic already exists; preserving it.")
            membership = await db.get(AccessGroupMember, (GROUP_ID, employee.id))
            if membership is None:
                db.add(AccessGroupMember(group_id=GROUP_ID, user_id=employee.id, added_by=admin.id))
                await db.flush()

            citation = Citation(
                source_id="S1",
                document_id=document.id,
                filename=document.filename,
                page_number=1,
                locator=chunk.locator,
                location="Text source · line 1",
                chunk_index=chunk.chunk_index,
                score=1.0,
                snippet=chunk.content,
                available=True,
            )
            modules = [
                TrainingModule(
                    title=title,
                    question=question,
                    answer=f"{sentence} [S1]",
                    insufficient_evidence=False,
                    citations=[citation],
                    retrieved_chunks=1,
                )
                for title, question, sentence in (
                    ("Rotation schedule", "How often should recovery tokens be rotated?", "Recovery tokens must be rotated every 90 days."),
                    ("Recovery owner check", "What should be verified before issuing a replacement token?", "Verify the recovery owner before issuing a replacement token."),
                    ("Renewal record", "What should be recorded after rotation?", "Record the renewal date after rotation."),
                )
            ]
            questions = [
                TrainingQuizQuestion(
                    question="How often should recovery tokens be rotated?",
                    choices=["Every 90 days", "Every 30 days", "Only after an incident", "Never"],
                    citations=[citation],
                ),
                TrainingQuizQuestion(
                    question="What should be verified before issuing a replacement token?",
                    choices=["The storage location", "The recovery owner", "The new token length", "The next audit date"],
                    citations=[citation],
                ),
                TrainingQuizQuestion(
                    question="What should be recorded after rotation?",
                    choices=["The employee's password", "The token value", "The renewal date", "The storage key"],
                    citations=[citation],
                ),
            ]
            plan = TrainingPlan(
                topic=TOPIC,
                modules=modules,
                quiz=questions,
                quiz_required=True,
                model="synthetic-source-fixture",
                provider="synthetic-qa",
                grounded_only=True,
            )
            plan_json = plan.model_dump(mode="json", exclude={"id", "status", "created_at", "completed_at"})
            snapshot = {
                "document_id": str(document.id),
                "version": document.version,
                "sha256": document.sha256,
                "indexed_version": document.indexed_version,
            }
            snapshots = [[snapshot] for _ in range(len(modules) + 1)]
            course = TrainingCourse(
                id=uuid4(),
                owner_user_id=admin.id,
                topic=TOPIC,
                plan=plan_json,
                source_snapshots=snapshots,
                quiz_answer_key=[
                    {"correct_index": 0, "explanation": "The source states that recovery tokens must be rotated every 90 days."},
                    {"correct_index": 1, "explanation": "The source requires verifying the recovery owner before issuing a replacement token."},
                    {"correct_index": 2, "explanation": "The source says to record the renewal date after rotation."},
                ],
            )
            db.add(course)
            await db.commit()
            evidence = {
                "result": "PASS",
                "courseId": str(course.id),
                "topic": TOPIC,
                "documentId": str(document.id),
                "filename": document.filename,
                "sourceSha256": document.sha256,
                "chunkIndex": chunk.chunk_index,
                "sourceSentenceCount": 3,
                "quizQuestionCount": len(questions),
                "correctIndexes": [0, 1, 2],
                "providerMode": "synthetic QA fixture; no live provider used",
                "membershipRestoredForAssignment": membership is None,
            }
        (ROOT / "evidence" / "c13-training-fixture.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print("C13_SOURCE_CITED_COURSE=READY; exact indexed source; 3 modules; 3 scored questions; Finance Employee scoped")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
