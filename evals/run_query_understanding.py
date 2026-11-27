"""Query understanding and hybrid retrieval: baseline vs improved, retrieval and answers measured separately.

    .venv/Scripts/python evals/run_query_understanding.py                 # retrieval only (no LLM, deterministic)
    .venv/Scripts/python evals/run_query_understanding.py --answers       # also answers, with the active provider

The corpus is synthetic: the golden corpus (`app.rag.evaluation.golden`) plus five documents written here (an IT
policy, an inspection note, a payment memo, a CRM table and a 3,000-row door-access log that repeats the words
"policy" and "issue"). Every case of `evals/query_understanding.json` goes through production `run_chat`, so
routing, follow-up handling, retrieval, ACLs and the context budget are the real ones:

* baseline: `rag_hybrid_retrieval` and `rag_query_analysis` off (semantic search only, as before);
* improved: both on.

Retrieval is judged on the passages `run_chat` hands to the model (gold passage present, its rank, how many come
from the repetitive log, ACL leaks). Answers (`--answers`) use the existing rubric (`app.rag.evaluation.answers`).
The gate database (`EVAL_DATABASE_URL`, name ending in `_evalgate`) is rebuilt each run; the app database is only
read for the embedding model cache and, with `--answers`, the active provider's key.
"""

from __future__ import annotations

import argparse
import asyncio
import io
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
from app.rag.answer import NO_ANSWER_NOTE, Turn  # noqa: E402
from app.rag.chat import run_chat  # noqa: E402
from app.rag.components import build_rag  # noqa: E402
from app.rag.evaluation.dataset import load_dataset  # noqa: E402
from app.rag.evaluation.gate import gate_database_url, reset_schema  # noqa: E402
from app.rag.evaluation.golden import PERSONAS, _pdf, seed  # noqa: E402
from app.rag.evaluation.metrics import chunk_is_relevant  # noqa: E402
from app.rag.evaluation.report import render_json  # noqa: E402
from app.schemas.chat import ChatResponse  # noqa: E402

DATASET = ROOT / "evals" / "query_understanding.json"
REPORTS = ROOT / "evals" / "reports"
REPETITIVE = "door-access-log.csv"


# ------------------------------------------------------------------ corpus


def _docx(*blocks: tuple[str, str]) -> bytes:
    import docx

    document = docx.Document()
    for kind, text in blocks:
        if kind == "h":
            document.add_heading(text, level=1)
        else:
            document.add_paragraph(text)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def extra_corpus() -> dict[str, bytes]:
    from tests.rag_helpers import LEAD_COLUMNS, make_pdf_with_table, synthetic_leads

    log = ["Log date,User,Door,Event,Remark"]
    doors = ("Pier 4 main door", "Dockside office", "Loading bay 2", "Server room")
    for n in range(3000):
        event = "Access denied" if n % 97 == 0 else "Access granted"
        door = "Pier 4 main door" if event == "Access denied" else doors[n % 4]
        remark = "Badge read per access policy; no issue" if event == "Access granted" else "Badge expired; reported per policy"
        log.append(f"2026-09-{1 + n % 28:02d} {8 + n % 10:02d}:{n % 60:02d},staff-{n % 40:03d},{door},{event},{remark}")
    return {
        "it-acceptable-use-policy.pdf": _pdf(
            "Harborline Facilities IT Acceptable Use Policy. This IT policy applies to every employee and contractor. "
            "Passwords must be changed every 90 days and must be at least 14 characters long. Multi-factor "
            "authentication is required for email and for VPN access.",
            "Removable media: USB storage devices are blocked on company laptops; approved encrypted drives are issued "
            "by the service desk. Personal cloud storage must not be used for company files. Door access badges are "
            "personal and must not be shared. Violations are reported to the information security officer.",
        ),
        "site-inspection-findings.docx": _docx(
            ("h", "Site inspection summary, September 2026"),
            ("h", "Key findings"),
            ("p", "Overall, 111 of 120 door sensors (92.5%) are working across the four Harborline sites. Several issues "
                  "require attention: 9 sensors are offline, 6 of them at the Pier 4 warehouse. Alarm audio is enabled on "
                  "only 58 of the 111 working sensors."),
            ("p", "Backup retention varies between 5 and 14 days at the Dockside office, which suggests inconsistent "
                  "recording settings."),
            ("h", "Recommendations"),
            ("p", "Replace the offline sensors at Pier 4 within 48 hours and enable alarm audio at every loading bay."),
        ),
        "internet-payment-memo.docx": _docx(
            ("p", "Inter office memo. From: IT Department. To: CFO. Subject: Payments for approval."),
            ("p", "The following internet link requires approval: Fibre link 100 Mbps, Rs. 60,000 per month. Total Rs. 60,000."),
            ("p", "Approved by the CFO on 8 August 2026."),
        ),
        "quarterly-risk-register.txt": (
            "Quarterly risk register, Q3 2026\n"
            "Key challenges this quarter: delayed sensor deliveries from the vendor and staff turnover at the Dockside office.\n"
            "Mitigation: a second vendor was contacted and two new technicians start in October.\n"
        ).encode(),
        "crm-leads-export.pdf": make_pdf_with_table("CRM Leads Export", LEAD_COLUMNS, synthetic_leads(20), trailing_pages=2),
        REPETITIVE: ("\n".join(log) + "\n").encode(),
    }


async def seed_extra(sessions, settings, rag) -> int:
    from io import BytesIO

    from starlette.datastructures import UploadFile

    from app.rag.documents import DocumentService
    from app.services.audit import AuditContext

    ctx = AuditContext(None, "query-understanding-eval", None, None)
    files = extra_corpus()
    async with sessions() as db:
        admin = (await db.execute(select(User).where(User.email == PERSONAS["admin"]))).scalar_one()
        for name, data in files.items():
            service = DocumentService(db, settings, rag.storage, rag.indexer, admin, ctx)
            await service.upload(UploadFile(filename=name, file=BytesIO(data)), "shared")
    while await rag.worker.process_next():
        pass
    async with sessions() as db:
        rows = (await db.execute(select(Document.filename, Document.status, Document.chunk_count))).all()
    failed = [row.filename for row in rows if row.status != "ready"]
    if failed:
        raise RuntimeError(f"Documents did not become ready: {failed}")
    return len(files)


# ------------------------------------------------------------------ retrieval evaluation


class _OfflineLLM:
    """Retrieval mode: no provider. The table-query step gets NONE (normal retrieval answers), the answer step a stub."""

    async def chat(self, message: str, *, system_prompt: str | None = None) -> ChatResponse:
        reply = "NONE" if "TABLES in" in message else "Stub answer [S1]."
        return ChatResponse(reply=reply, model="offline", provider="offline")

    async def close(self) -> None:
        return None


def _kind(case_id: str) -> str:
    return case_id.split("-", 1)[0]


async def retrieval_run(sessions, rag, settings, dataset) -> dict:
    llm = _OfflineLLM()
    rows = []
    async with sessions() as db:
        users = {name: (await db.execute(select(User).where(User.email == email))).scalar_one() for name, email in PERSONAS.items()}
        for case in dataset.cases:
            started = time.perf_counter()
            result = await run_chat(
                db, users[case.persona], rag, settings, lambda: _ready(llm), case.question,
                history=[Turn(q, a if a is not None else NO_ANSWER_NOTE) for q, a in case.history],
                document_ids=None, focus_document_ids=None,
            )
            latency = (time.perf_counter() - started) * 1000
            gold = [chunk_is_relevant(c.filename, c.content, case) for c in result.passages]
            first = next((i for i, g in enumerate(gold, 1) if g), None)
            files = [c.filename for c in result.passages]
            leak = case.category == "unauthorized" and case.expected_document in files
            rows.append({
                "id": case.id, "kind": _kind(case.id), "language": case.language, "strategy": result.strategy,
                "passages": len(files), "documents": len(set(files)),
                "hit": first is not None and case.category != "unauthorized",
                "rank": first, "rr": (1.0 / first) if first and case.category != "unauthorized" else 0.0,
                "repetitive_share": (files.count(REPETITIVE) / len(files)) if files and case.expected_document != REPETITIVE else 0.0,
                "leak": leak, "latency_ms": round(latency, 1), "retrieved": files,
            })
    return {"metrics": _retrieval_metrics(rows), "by_kind": _by_kind(rows), "cases": rows}


async def _ready(llm):
    return llm


def _retrieval_metrics(rows: list[dict]) -> dict:
    scored = [r for r in rows if r["kind"] != "acl" or r["id"] == "acl-member-typo"]
    latencies = sorted(r["latency_ms"] for r in rows)
    return {
        "cases": len(rows),
        "hit_rate": round(sum(r["hit"] for r in scored) / len(scored), 4),
        "mrr": round(sum(r["rr"] for r in scored) / len(scored), 4),
        "repetitive_share": round(sum(r["repetitive_share"] for r in rows) / len(rows), 4),
        "acl_leaks": sum(r["leak"] for r in rows),
        "latency_p50_ms": latencies[len(latencies) // 2],
        "latency_p95_ms": latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))],
    }


def _by_kind(rows: list[dict]) -> dict:
    out = {}
    for kind in sorted({r["kind"] for r in rows}):
        group = [r for r in rows if r["kind"] == kind and (kind != "acl" or r["id"] == "acl-member-typo")]
        if group:
            out[kind] = {"cases": len(group), "hit_rate": round(sum(r["hit"] for r in group) / len(group), 4),
                         "mrr": round(sum(r["rr"] for r in group) / len(group), 4)}
    return out


# ------------------------------------------------------------------ main


def configured(settings, improved: bool):
    return settings.model_copy(update={"rag_hybrid_retrieval": improved, "rag_query_analysis": improved})


async def _provider(live, choice: str):
    """The active provider, or a configured one by id ("deepseek"), with its stored key."""
    from app.core.crypto import CredentialCipher
    from app.db.models import ProviderConfig
    from app.services.llm_runtime import LLMRuntime

    if choice == "active":
        from app.rag.evaluation.gate import _llm

        return await _llm(live, "active")
    runtime = LLMRuntime(live, CredentialCipher(live.master_key_bytes, live.master_encryption_key_id))
    engine = create_engine(live.database_url.get_secret_value())
    try:
        async with create_sessionmaker(engine)() as db:
            model = (await db.execute(select(ProviderConfig.model).where(ProviderConfig.provider == choice))).scalar_one()
            return await runtime.get_client(db, (choice, model))
    finally:
        await engine.dispose()


async def run(live, url: str, answers: bool, provider: str = "active", rpm: int = 0) -> dict:
    data_dir = Path(tempfile.mkdtemp(prefix="infraspine-qu-eval-"))
    model_dir = Path(live.rag_models_dir) if live.rag_models_dir else live.rag_data_dir / "models"
    settings = live.model_copy(update={
        "database_url": SecretStr(url), "rag_data_dir": data_dir, "rag_models_dir": str(model_dir.resolve()),
        "rag_trace_enabled": False, "rag_admin_only_uploads": False, "rag_use_docling": False,
        "rag_parse_isolation": False, "rag_general_answers": False,
    })
    engine = create_engine(url)
    sessions = create_sessionmaker(engine)
    rag = build_rag(settings, sessions)
    llm = None
    try:
        golden = await seed(sessions, settings, rag)
        extra = await seed_extra(sessions, settings, rag)
        rag.embedder.warmup()
        dataset = load_dataset(DATASET)
        report: dict = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "dataset": {"name": dataset.name, "sha256": dataset.sha256, "cases": len(dataset.cases)},
            "corpus_documents": golden + extra,
            "embedding_model": rag.embedder.model_name,
        }
        for name, improved in (("baseline", False), ("improved", True)):
            report[name] = {"retrieval": await retrieval_run(sessions, rag, configured(settings, improved), dataset)}
        if answers:
            from app.rag.evaluation.answers import evaluate_answers
            from app.rag.evaluation.gate import Paced

            llm = Paced(await _provider(live, provider), rpm)
            for name, improved in (("baseline", False), ("improved", True)):
                result = await evaluate_answers(sessions, rag, configured(settings, improved), dataset, PERSONAS, llm)
                report[name]["answers"] = {"metrics": result["metrics"], "cases": result["cases"]}
        base, new = report["baseline"]["retrieval"]["metrics"], report["improved"]["retrieval"]["metrics"]
        report["delta"] = {key: round(new[key] - base[key], 4) for key in ("hit_rate", "mrr", "repetitive_share")}
        return report
    finally:
        if llm is not None:
            await llm.close()
        rag.close()
        await engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--answers", action="store_true", help="also score answers with the active provider")
    parser.add_argument("--label", default="", help="suffix for the report file name")
    parser.add_argument("--provider", default="active", help="'active' or a configured provider id, e.g. deepseek")
    parser.add_argument("--rpm", type=int, default=0, help="at most this many provider requests per minute")
    args = parser.parse_args()
    live = get_settings()
    url = gate_database_url(live)
    reset_schema(url)  # Alembic runs its own event loop: before ours
    report = asyncio.run(run(live, url, args.answers, args.provider, args.rpm))
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"query-understanding{('-' + args.label) if args.label else ''}.json"
    path.write_text(render_json(report), encoding="utf-8")
    for name in ("baseline", "improved"):
        print(name, json.dumps(report[name]["retrieval"]["metrics"]), json.dumps(report[name]["retrieval"]["by_kind"]))
        if "answers" in report[name]:
            print(name, "answers", json.dumps(report[name]["answers"]["metrics"], default=str))
    print("delta", report["delta"])
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
