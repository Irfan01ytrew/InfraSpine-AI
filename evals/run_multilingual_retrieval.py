"""Compare English-query baseline and offline Urdu query expansion on the synthetic corpus.

Run from the project root with `.venv/Scripts/python evals/run_multilingual_retrieval.py`.
The existing evaluation gate database (name ending in `_evalgate`) is rebuilt each run; the app
database is never used. The golden corpus is synthetic and the embedding model/index are shared
between baseline and improved measurements.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import User
from app.db.session import create_engine, create_sessionmaker
from app.rag.components import build_rag
from app.rag.evaluation.dataset import load_dataset
from app.rag.evaluation.gate import gate_database_url, reset_schema
from app.rag.evaluation.golden import PERSONAS, seed
from app.rag.evaluation.report import render_json
from app.rag.evaluation.runner import evaluate
from app.rag.query_expansion import retrieval_queries
from app.rag.retrieval import retrieve

DATASET = ROOT / "evals" / "multilingual_retrieval.json"
REPORT = ROOT / "evals" / "reports" / "multilingual-comparison.json"
ACL_QUESTION = "ترجیحی سپورٹ کے ریٹ کارڈ کی شرح کتنی ہے؟"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    live = get_settings()
    url = gate_database_url(live)
    reset_schema(url)
    asyncio.run(run(live, url))


async def run(live, url: str) -> None:
    data_dir = Path(tempfile.mkdtemp(prefix="infraspine-multilingual-eval-"))
    model_dir = Path(live.rag_models_dir) if live.rag_models_dir else live.rag_data_dir / "models"
    settings = live.model_copy(
        update={
            "database_url": SecretStr(url),
            "rag_data_dir": data_dir,
            "rag_models_dir": str(model_dir.resolve()),
            "rag_trace_enabled": False,
            "rag_vector_store": "faiss",
            "rag_index_role": "owner",
            "rag_embedding_allow_download": False,
            "rag_admin_only_uploads": False,
            # The focused corpus uses the built-in parsers and can run from this script without
            # spawning parser subprocesses or downloading Docling assets.
            "rag_use_docling": False,
            "rag_parse_isolation": False,
        }
    )
    engine = create_engine(url)
    sessions = create_sessionmaker(engine)
    rag = build_rag(settings, sessions)
    try:
        document_count = await seed(sessions, settings, rag)
        rag.embedder.warmup()
        dataset = load_dataset(DATASET)
        evaluate_args = {
            "ks": (1, 5),
            "min_similarity": settings.rag_min_similarity,
            "max_context_chars": settings.rag_max_context_chars,
        }
        baseline = await evaluate(sessions, rag.embedder, rag.vector_store, dataset, PERSONAS, **evaluate_args)
        improved = await evaluate(
            sessions,
            rag.embedder,
            rag.vector_store,
            dataset,
            PERSONAS,
            query_transform=retrieval_queries,
            **evaluate_args,
        )
        acl = await _acl_probe(sessions, rag, settings)
        report = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "dataset": {
                "name": dataset.name,
                "sha256": dataset.sha256,
                "cases": len(dataset.cases),
                "documents_in_synthetic_corpus": document_count,
            },
            "vector_index": "faiss",
            "embedding_model": rag.embedder.model_name,
            "embedding_dimension": rag.embedder.dimension,
            "query_expansion": "offline Urdu/Roman Urdu vocabulary mapped to English; English queries unchanged",
            "baseline": {"metrics": baseline["metrics"], "by_language": baseline["by_language"]},
            "improved": {"metrics": improved["metrics"], "by_language": improved["by_language"]},
            "delta": {
                metric: round(
                    improved["metrics"][metric] - baseline["metrics"][metric], 6
                )
                for metric in ("recall@5", "mrr")
            }
            | {
                f"latency_{percentile}_ms": round(
                    improved["metrics"]["latency_ms"][percentile]
                    - baseline["metrics"]["latency_ms"][percentile],
                    4,
                )
                for percentile in ("mean", "p50", "p95")
            },
            "acl_probe": acl,
            "cases": [
                {
                    "id": base["id"],
                    "language": base["language"],
                    "baseline_rank": base["first_relevant_rank"],
                    "improved_rank": after["first_relevant_rank"],
                }
                for base, after in zip(baseline["cases"], improved["cases"], strict=True)
            ],
        }
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(render_json(report), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"Report: {REPORT}")
    finally:
        rag.close()
        await engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)


async def _acl_probe(sessions, rag, settings) -> dict:
    async with sessions() as db:
        member = await db.scalar(select(User).where(User.email == PERSONAS["member"]))
        outsider = await db.scalar(select(User).where(User.email == PERSONAS["outsider"]))
        found = {}
        for name, user in (("member", member), ("outsider", outsider)):
            chunks = await retrieve(
                db,
                user,
                rag.embedder,
                rag.vector_store,
                ACL_QUESTION,
                top_k=5,
                min_similarity=settings.rag_min_similarity,
                max_context_chars=settings.rag_max_context_chars,
                query_transform=retrieval_queries,
            )
            found[name] = sorted({chunk.filename for chunk in chunks})
    passed = "rate-card.txt" in found["member"] and "rate-card.txt" not in found["outsider"]
    return {"question": ACL_QUESTION, "retrieved_documents": found, "passed": passed}


if __name__ == "__main__":
    main()
