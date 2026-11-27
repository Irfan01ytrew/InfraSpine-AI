"""Enterprise workload baseline: discovery, follow-ups, multilingual, multi-document, grounding, ACLs.

    .venv/Scripts/python evals/run_enterprise_workloads.py                 # retrieval only (no LLM, deterministic)
    .venv/Scripts/python evals/run_enterprise_workloads.py --answers       # also answers, with the active provider

The corpus is synthetic: the Harborline golden corpus (`app.rag.evaluation.golden`) plus the Northwind Traders
workload corpus (`app.rag.evaluation.workloads`). Every case of `evals/enterprise_workloads.json` goes through
production `run_chat` with this deployment's settings unchanged (hybrid retrieval, query analysis, open-mode
general answers, Docling, parse isolation), and conversation cases get history and focus documents built the
way `ConversationService.context` builds them. Each answer's evidence decision is read from its trace.

* Retrieval mode answers with an offline stub, so routing and the passages handed to the model are measured
  without a provider (the table-query step gets NONE, as with a model that declines).
* `--answers` uses the active provider (synthetic data only) and scores replies deterministically.
* The report also measures the golden gate's own cases with the gate's retrieval call and with production's
  (hybrid, query analysis, Urdu expansion, per-document cap), because the gate calls `retrieve()` without them.

The gate database (`EVAL_DATABASE_URL`, name ending in `_evalgate`) is rebuilt each run; the application database
is only read for the embedding model cache and, with `--answers`, the active provider's key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import SecretStr  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.models import Document, User  # noqa: E402
from app.db.session import create_engine, create_sessionmaker  # noqa: E402
from app.rag import tracing  # noqa: E402
from app.rag.chat import run_chat  # noqa: E402
from app.rag.components import build_rag  # noqa: E402
from app.rag.evaluation import golden, workloads  # noqa: E402
from app.rag.evaluation.dataset import load_dataset  # noqa: E402
from app.rag.evaluation.gate import GOLDEN, gate_database_url, reset_schema  # noqa: E402
from app.rag.evaluation.metrics import chunk_is_relevant, mean  # noqa: E402
from app.rag.evaluation.report import render_json  # noqa: E402
from app.schemas.chat import ChatResponse  # noqa: E402

DATASET = ROOT / "evals" / "enterprise_workloads.json"
REPORTS = ROOT / "evals" / "reports"


class _OfflineLLM:
    """Retrieval mode: no provider. The table-query step gets NONE (normal retrieval answers), everything else a stub."""

    provider = "offline"

    async def chat(self, message: str, *, system_prompt: str | None = None) -> ChatResponse:
        reply = "NONE" if "TABLES in" in message else "Stub answer [S1]."
        return ChatResponse(reply=reply, model="offline", provider="offline")

    async def close(self) -> None:
        return None


async def run_cases(sessions, rag, settings, cases, llm, *, answers: bool) -> list[dict]:
    rows = []
    async with sessions() as db:
        users = {
            name: (await db.execute(select(User).where(User.email == email))).scalar_one()
            for name, email in golden.PERSONAS.items()
        }
        ids = dict((await db.execute(select(Document.filename, Document.id))).all())

    async def ready():
        return llm

    from app.rag.retrieval import named_documents

    for case in cases:
        # Each earlier turn's saved source state: what its question named, as the product resolved and stored it.
        async with sessions() as db:
            named = [
                await named_documents(db, users[case.persona], rag.embedder.model_name, turn.question,
                                      limit=settings.rag_discovery_max_documents)
                if settings.rag_hierarchical_retrieval else []
                for turn in case.history
            ]
        history, focus = workloads.history_and_focus(case, ids, settings.chat_context_turns, named)
        history_documents = [name for turn in case.history for name in turn.cited]
        async with sessions() as db:
            started = time.perf_counter()
            async with tracing.traced(
                sessions, settings, route="evaluation", question=case.question,
                user_id=users[case.persona].id, request_id=f"workload-{case.id}",
            ) as trace:
                result = await run_chat(
                    db, users[case.persona], rag, settings, tracing.traced_llm(ready), case.question,
                    history=history, document_ids=None, focus_document_ids=focus or None,
                )
            latency = (time.perf_counter() - started) * 1000
        retrieval = workloads.judge_retrieval(case, result.passages, history_documents)
        row = {
            "id": case.id, "scenario": case.scenario, "language": case.language, "persona": case.persona,
            "expect": case.expect, "history_turns": len(history), "focus_documents": len(focus),
            "strategy": result.strategy, "decision": trace.decision if trace else None,
            "llm_calls": trace.llm_calls if trace else None,
            "latency_ms": round(latency, 1), "retrieval": retrieval,
        }
        if answers:
            answer = result.answer
            row["answer"] = workloads.judge_answer(
                case, answer=answer.answer, insufficient=answer.insufficient_evidence, general=answer.general,
                cited_files=[chunk.filename for _, chunk in answer.cited], evidence_hit=retrieval["evidence_hit"],
                decision=row["decision"], strategy=result.strategy,
            )
            row["answer"]["reply"] = answer.answer  # synthetic corpus only: kept for review and `--rescore`
            row["answer"]["cited_files"] = sorted({chunk.filename for _, chunk in answer.cited})
            row["answer"]["tokens"] = (
                {"prompt": trace.prompt_tokens, "completion": trace.completion_tokens} if trace else None
            )
        rows.append(row)
    return rows


async def check_labels(db, suite) -> None:
    """Every labelled passage must exist in some chunk of its document as the production parsers produced it;
    otherwise a labelling mistake would be scored as a retrieval failure."""
    from app.db.models import DocumentChunk

    rows = (await db.execute(
        select(Document.filename, DocumentChunk.content).join(DocumentChunk, DocumentChunk.document_id == Document.id)
    )).all()
    missing = sorted({
        f"{case.id}: {entry.passage!r} in {entry.document}"
        for case in suite.cases for entry in case.evidence
        if not any(name == entry.document and workloads.contains(content, entry.passage) for name, content in rows)
    })
    if missing:
        raise RuntimeError("Evidence labels not found in the parsed documents:\n  " + "\n  ".join(missing))


async def gate_parity(sessions, rag, settings) -> dict:
    """The golden gate's ranked cases, retrieved by semantic search alone (the gate before alignment) and the production way."""
    from app.rag.retrieval import retrieve, search_options

    dataset = load_dataset(GOLDEN)
    cases = [c for c in dataset.cases if c.category in {"answerable", "conflicting", "longer"} and not c.history]
    configs = {"semantic_only": {}, "production": search_options(settings)}
    out = {}
    async with sessions() as db:
        users = {
            name: (await db.execute(select(User).where(User.email == email))).scalar_one()
            for name, email in golden.PERSONAS.items()
        }
        for name, extra in configs.items():
            ranks, misses = [], []
            for case in cases:
                chunks = await retrieve(
                    db, users[case.persona], rag.embedder, rag.vector_store, case.question, top_k=10,
                    min_similarity=settings.rag_min_similarity, max_context_chars=settings.rag_max_context_chars,
                    **extra,
                )
                rank = next(
                    (i for i, c in enumerate(chunks, 1) if chunk_is_relevant(c.filename, c.content, case)), None
                )
                ranks.append(rank)
                if rank is None or rank > settings.rag_top_k:
                    misses.append(case.id)
            out[name] = {
                "cases": len(cases),
                "hit@1": round(sum(1 for r in ranks if r == 1) / len(ranks), 4),
                f"hit@{settings.rag_top_k}": round(sum(1 for r in ranks if r and r <= settings.rag_top_k) / len(ranks), 4),
                "mrr": round(mean([1 / r if r else 0.0 for r in ranks]), 4),
                "missed_in_top_k": misses,
            }
    return out


async def run(live, url: str, answers: bool, provider: str, rpm: int) -> dict:
    data_dir = Path(tempfile.mkdtemp(prefix="infraspine-workloads-"))
    model_dir = Path(live.rag_models_dir) if live.rag_models_dir else live.rag_data_dir / "models"
    settings = live.model_copy(update={
        "database_url": SecretStr(url), "rag_data_dir": data_dir, "rag_models_dir": str(model_dir.resolve()),
        # Traces go to the throwaway gate database: they carry each answer's evidence decision.
        "rag_trace_enabled": True,
        # A regular user uploads the private note, as in the golden corpus.
        "rag_admin_only_uploads": False,
    })
    engine = create_engine(url)
    sessions = create_sessionmaker(engine)
    rag = build_rag(settings, sessions)
    llm = None
    try:
        started = time.perf_counter()
        base = await golden.seed(sessions, settings, rag)
        extra = await workloads.seed(sessions, settings, rag)
        seeded_s = round(time.perf_counter() - started, 1)
        rag.embedder.warmup()
        suite = workloads.load_workloads(DATASET)
        async with sessions() as db:
            chunk_counts = dict((await db.execute(select(Document.filename, Document.chunk_count))).all())
            await check_labels(db, suite)
        report: dict = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "dataset": {"name": suite.name, "sha256": suite.sha256, "cases": len(suite.cases)},
            "corpus": {
                "documents": base + extra, "workload_documents": extra, "seed_seconds": seeded_s,
                "chunks": sum(chunk_counts.values()),
                "workload_chunks": {name: chunk_counts.get(name) for name in workloads.corpus()},
            },
            "configuration": {
                "embedding_model": rag.embedder.model_name, "top_k": settings.rag_top_k,
                "min_similarity": settings.rag_min_similarity, "hybrid": settings.rag_hybrid_retrieval,
                "query_analysis": settings.rag_query_analysis, "general_answers": settings.rag_general_answers,
                "max_chunks_per_document": settings.rag_max_chunks_per_document,
                "chat_context_turns": settings.chat_context_turns, "docling": settings.rag_use_docling,
            },
        }
        rows = await run_cases(sessions, rag, settings, suite.cases, _OfflineLLM(), answers=False)
        report["retrieval"] = {"summary": workloads.summarize(rows, answers=False), "cases": rows}
        report["gate_parity"] = await gate_parity(sessions, rag, settings)
        if answers:
            from app.rag.evaluation.gate import Paced
            from evals.run_query_understanding import _provider

            llm = Paced(await _provider(live, provider), rpm)
            rows = await run_cases(sessions, rag, settings, suite.cases, llm, answers=True)
            report["answers"] = {
                "provider": getattr(llm, "provider", None),
                "summary": workloads.summarize(rows, answers=True),
                "cases": rows,
            }
        return report
    finally:
        if llm is not None:
            await llm.close()
        rag.close()
        await engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)


def render(report: dict) -> str:
    lines = [
        f"Enterprise workloads - {report['dataset']['cases']} cases, {report['corpus']['documents']} documents "
        f"({report['corpus']['chunks']} chunks), {report['configuration']['embedding_model']}",
    ]
    for mode in ("retrieval", "answers"):
        if mode not in report:
            continue
        summary = report[mode]["summary"]
        lines.append(f"\n{mode.upper()}  overall: {json.dumps(summary['overall'], ensure_ascii=False)}")
        for scenario, rates in summary["by_scenario"].items():
            lines.append(f"  {scenario:<14} {json.dumps(rates, ensure_ascii=False)}")
    lines.append(f"\nGATE PARITY  {json.dumps(report['gate_parity'])}")
    return "\n".join(lines)


def rescore(path: Path) -> str:
    """Re-judge the stored answers of a report with the current rubric and dataset labels. No provider call; the
    retrieval results are kept as measured."""
    report = json.loads(path.read_text(encoding="utf-8"))
    suite = workloads.load_workloads(DATASET)
    cases = {case.id: case for case in suite.cases}
    rows = report["answers"]["cases"]
    for row in rows:
        row["answer"] = workloads.rejudge(cases[row["id"]], row)
    report["answers"]["summary"] = workloads.summarize(rows, answers=True)
    report["answers"]["rescored"] = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"), "labels_sha256": suite.sha256,
    }
    path.write_text(render_json(report), encoding="utf-8")
    return render(report) + f"\nRescored: {path}"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--answers", action="store_true", help="also score answers with the active provider")
    parser.add_argument("--provider", default="active", help="'active' or a configured provider id")
    parser.add_argument("--rpm", type=int, default=0, help="at most this many provider requests per minute")
    parser.add_argument("--label", default="", help="suffix for the report file name")
    parser.add_argument("--rescore", type=Path, help="re-score a saved report's answers with the current rubric")
    args = parser.parse_args()
    if args.rescore:
        print(rescore(args.rescore))
        return
    live = get_settings()
    url = gate_database_url(live)
    reset_schema(url)  # Alembic runs its own event loop: before ours
    report = asyncio.run(run(live, url, args.answers, args.provider, args.rpm))
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"enterprise-workloads{('-' + args.label) if args.label else ''}.json"
    path.write_text(render_json(report), encoding="utf-8")
    print(render(report))
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
