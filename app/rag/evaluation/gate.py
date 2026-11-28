"""Evaluation gate: fail the build when retrieval or answer quality drops below `evals/gate.json`.

    python -m app.rag.evaluation.gate                      # retrieval + permissions (no LLM, deterministic)
    python -m app.rag.evaluation.gate --answers            # also production answers, with the active provider
    python -m app.rag.evaluation.gate --answers --provider env   # provider from EVAL_LLM_* variables (CI)

Each run starts from nothing: the schema of a dedicated `*_evalgate` database is rebuilt, a throwaway data
directory gets a fresh index, and the synthetic golden corpus (`app.rag.evaluation.golden`) is uploaded and
processed with the production parsers, embedder and permission rules. The cases in `evals/golden.json` then
go through production `retrieve` and, with `--answers`, production `run_chat`. The live database and data
directory are only read, for the embedding model cache and (with `--provider active`) the provider key.

The gate database must exist once (the application role cannot create databases):

    psql -U postgres -c "CREATE DATABASE infraspine_ai_evalgate OWNER infraspine_app;"

Exit status: 0 passed, 1 a threshold failed, 2 the gate could not run (no scores are invented).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy.engine import make_url

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.rag.evaluation.dataset import Dataset, EvaluationError, load_dataset

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "evals" / "golden.json"
THRESHOLDS = ROOT / "evals" / "gate.json"
REPORTS = ROOT / "evals" / "reports"
_SUFFIX = "_evalgate"
_ANSWER_CATEGORIES = {"answerable", "conflicting", "longer"}
_KS = (1, 5, 10)


class GateError(RuntimeError):
    """The gate could not run. Nothing is scored."""


@dataclass(frozen=True)
class Check:
    metric: str
    value: float | None
    op: str  # "min" or "max"
    limit: float

    @property
    def passed(self) -> bool:
        if self.value is None:
            return False
        return self.value >= self.limit if self.op == "min" else self.value <= self.limit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.rag.evaluation.gate")
    parser.add_argument("--answers", action="store_true", help="Also score production answers (calls the LLM)")
    parser.add_argument(
        "--provider",
        choices=("active", "env"),
        default="active",
        help="active: this deployment's active provider; env: EVAL_LLM_PROVIDER/MODEL/API_KEY[/BASE_URL]",
    )
    parser.add_argument(
        "--rpm",
        type=int,
        default=int(os.environ.get("EVAL_LLM_RPM", "0")),
        help="At most this many LLM requests per minute (0 = no limit; free Gemini keys allow 15)",
    )
    parser.add_argument("--keep", action="store_true", help="Keep the throwaway data directory for inspection")
    args = parser.parse_args(argv)
    try:
        report = run(answers=args.answers, provider=args.provider, keep=args.keep, rpm=args.rpm)
    except (GateError, EvaluationError) as exc:
        print(f"Gate could not run: {exc}", file=sys.stderr)
        return 2
    print(render(report))
    path = write_report(report)
    print(f"Report: {path}")
    return 0 if report["passed"] else 1


def run(*, answers: bool, provider: str, keep: bool = False, rpm: int = 0) -> dict:
    live = get_settings()
    gate_url = gate_database_url(live)
    dataset = load_dataset(GOLDEN)
    thresholds = load_thresholds(THRESHOLDS)
    reset_schema(gate_url)
    data_dir = Path(tempfile.mkdtemp(prefix="infraspine-gate-"))
    models = Path(live.rag_models_dir) if live.rag_models_dir else live.rag_data_dir / "models"
    settings = live.model_copy(
        update={
            "database_url": SecretStr(gate_url),
            "rag_data_dir": data_dir,
            "rag_models_dir": str(models.resolve()),
            "rag_trace_enabled": False,
            # The corpus has a document a regular user keeps private, to prove nobody else retrieves it.
            "rag_admin_only_uploads": False,
        }
    )
    try:
        return asyncio.run(_score(live, settings, dataset, thresholds, answers=answers, provider=provider, rpm=rpm))
    finally:
        if keep:
            print(f"Data directory kept: {data_dir}")
        else:
            shutil.rmtree(data_dir, ignore_errors=True)


def gate_database_url(live: Settings) -> str:
    explicit = os.environ.get("EVAL_DATABASE_URL")
    url = make_url(explicit) if explicit else None
    if url is None:
        source = make_url(live.database_url.get_secret_value())
        url = source.set(database=f"{source.database}{_SUFFIX}")
    if not (url.database or "").endswith(_SUFFIX):
        raise GateError(f"The gate database name must end in {_SUFFIX!r}; its schema is rebuilt on every run.")
    return url.render_as_string(hide_password=False)


def reset_schema(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    try:
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
    except Exception as exc:
        name = make_url(url).database
        raise GateError(
            f"could not rebuild the schema of {name} ({type(exc).__name__}). Create the database once with: "
            f'psql -U postgres -c "CREATE DATABASE {name} OWNER <application role>;"'
        ) from None


def load_thresholds(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError(f"cannot read {path.name}: {exc}") from None
    for section in ("retrieval", "answers"):
        for metric, rule in raw.get(section, {}).items():
            if metric == "by_language":
                continue
            if not isinstance(rule, dict) or set(rule) not in ({"min"}, {"max"}):
                raise GateError(f"{section}.{metric} in {path.name} needs exactly one of min or max.")
    return raw


async def _score(
    live: Settings, settings: Settings, dataset: Dataset, thresholds: dict, *, answers: bool, provider: str, rpm: int
) -> dict:
    from app.db.session import create_engine, create_sessionmaker
    from app.rag.components import build_rag
    from app.rag.evaluation import golden

    llm = Paced(await _llm(live, provider), rpm) if answers else None  # fail before minutes of processing
    engine = create_engine(settings.database_url.get_secret_value())
    sessions = create_sessionmaker(engine)
    rag = build_rag(settings, sessions)
    try:
        documents = await golden.seed(sessions, settings, rag)
        from app.rag.retrieval import search_options

        # Gated: the retrieval users get (production options). Reported beside it, not gated: semantic search alone,
        # the configuration this gate measured before it was aligned with production, for comparison.
        retrieval = await _retrieval(sessions, settings, rag, dataset, search_options(settings))
        semantic_only = await _retrieval(sessions, settings, rag, dataset, None)
        checks = _checks(retrieval["metrics"], thresholds.get("retrieval", {}))
        for language, rules in thresholds.get("retrieval", {}).get("by_language", {}).items():
            values = retrieval["by_language"].get(language, {})
            checks += [replace(check, metric=f"{language} {check.metric}") for check in _checks(values, rules)]
        report: dict = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "dataset": {"name": dataset.name, "sha256": dataset.sha256, "cases": len(dataset.cases)},
            "documents": documents,
            "embedding_model": rag.embedder.model_name,
            "retrieval": retrieval,
            "retrieval_semantic_only": semantic_only,
        }
        if llm is not None:
            from app.rag.evaluation.answers import evaluate_answers

            try:
                scored = await evaluate_answers(sessions, rag, settings, dataset, golden.PERSONAS, llm)
            except AppError as exc:  # the provider failed; a partial run is not a score
                raise GateError(f"answers were not scored: {type(exc).__name__}: {exc.message}") from None
            finally:
                await llm.close()
            metrics = dict(scored["metrics"])
            metrics["false_refusals"] = sum(
                1
                for row in scored["cases"]
                if not row["expect_refusal"] and row["evidence_sufficient"] and row["automated_judgment"] == "refused"
            )
            checks += _checks(metrics, thresholds.get("answers", {}))
            report["answers"] = {
                "provider": scored["provider"],
                "metrics": metrics,
                "failures": [
                    {"id": row["id"], "judgment": row["automated_judgment"], "stage": row["failure_stage"]}
                    for row in scored["cases"]
                    if row["failure_stage"] != "none" and not row["human_review"]
                ],
                "unevaluated": [row["id"] for row in scored["cases"] if row["human_review"]],
            }
        report["checks"] = [
            {"metric": c.metric, "value": c.value, c.op: c.limit, "passed": c.passed} for c in checks
        ]
        report["passed"] = all(c.passed for c in checks)
        return report
    finally:
        rag.close()
        await engine.dispose()


async def _retrieval(sessions, settings: Settings, rag, dataset: Dataset, options: dict | None) -> dict:
    """Ranking of the answerable cases, plus a leak check: a persona never retrieves a document it cannot read.
    `options` are extra `retrieve()` options (production's `search_options`), or None for semantic search alone."""
    from app.rag.evaluation import golden
    from app.rag.evaluation.dataset import EvalCase
    from app.rag.evaluation.runner import _users, evaluate
    from app.rag.retrieval import retrieve

    ranked = Dataset(
        name=dataset.name,
        sha256=dataset.sha256,
        cases=tuple(
            EvalCase(
                id=case.id,
                question=case.question,
                language=case.language,
                persona=case.persona,
                expect_abstain=False,
                expected_document=case.expected_document,
                expected_passage=case.expected_passage,
            )
            for case in dataset.cases
            if case.category in _ANSWER_CATEGORIES and not case.history  # "?" has no topic to rank on its own
        ),
    )
    report = await evaluate(
        sessions,
        rag.embedder,
        rag.vector_store,
        ranked,
        golden.PERSONAS,
        ks=_KS,
        min_similarity=settings.rag_min_similarity,
        max_context_chars=settings.rag_max_context_chars,
        retrieval_options=options,
    )
    restricted = [case for case in dataset.cases if case.category == "unauthorized"]
    leaks = []
    async with sessions() as db:
        users = await _users(db, dataset, golden.PERSONAS)
        for case in restricted:
            chunks = await retrieve(
                db,
                users[case.persona],
                rag.embedder,
                rag.vector_store,
                case.question,
                top_k=settings.rag_top_k,
                min_similarity=settings.rag_min_similarity,
                max_context_chars=settings.rag_max_context_chars,
                **(options or {}),
            )
            if any(
                chunk.filename == case.expected_document or any(fact in chunk.content for fact in case.forbidden_facts)
                for chunk in chunks
            ):
                leaks.append(case.id)
    metrics = {key: value for key, value in report["metrics"].items() if key != "latency_ms"}
    metrics["permission_leaks"] = len(leaks)
    metrics["retrieval_p95_ms"] = report["metrics"]["latency_ms"]["p95"]
    misses = [row["id"] for row in report["cases"] if row["first_relevant_rank"] is None]
    return {
        "metrics": metrics,
        "by_language": report["by_language"],
        "missed": misses,
        "leaks": leaks,
        "permission_cases": len(restricted),
    }


class Paced:
    """Keeps LLM calls under a per-minute quota and waits out a rate limit instead of failing the run."""

    _RETRIES = 4

    def __init__(self, inner, rpm: int) -> None:
        self._inner = inner
        self._rpm = rpm
        self._sent: list[float] = []
        self._lock = asyncio.Lock()

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def chat(self, message: str, *, system_prompt: str | None = None):
        from app.core.errors import LLMRateLimitError

        for attempt in range(self._RETRIES + 1):
            await self._slot()
            try:
                return await self._inner.chat(message, system_prompt=system_prompt)
            except LLMRateLimitError:
                if attempt == self._RETRIES:
                    raise
                await asyncio.sleep(65)  # free-tier quotas reset per minute
        raise AssertionError("unreachable")

    async def _slot(self) -> None:
        if self._rpm <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            while True:
                now = loop.time()
                self._sent = [sent for sent in self._sent if now - sent < 60]
                if len(self._sent) < self._rpm:
                    self._sent.append(now)
                    return
                await asyncio.sleep(60 - (now - self._sent[0]) + 0.1)


def _checks(values: dict, rules: dict) -> list[Check]:
    checks = []
    for metric, rule in rules.items():
        if metric == "by_language":
            continue
        (op, limit), = rule.items()
        value = values.get(metric)
        checks.append(Check(metric, float(value) if isinstance(value, (int, float)) else None, op, float(limit)))
    return checks


async def _llm(live: Settings, provider: str):
    from app.core.crypto import rotating_cipher_from_settings
    from app.core.errors import LLMNotConfiguredError, LLMUnavailableError
    from app.db.session import create_engine, create_sessionmaker
    from app.services.llm_runtime import LLMRuntime, build_client
    from app.services.processing_policy import model_allowed_for_content

    if provider == "env":
        missing = [name for name in ("EVAL_LLM_PROVIDER", "EVAL_LLM_MODEL", "EVAL_LLM_API_KEY") if not os.environ.get(name)]
        if missing:
            raise GateError(f"--provider env needs {', '.join(missing)}.")
        provider_id = os.environ["EVAL_LLM_PROVIDER"]
        base_url = os.environ.get("EVAL_LLM_BASE_URL") or None
        if not model_allowed_for_content(provider_id, base_url, live):
            raise GateError("The EVAL_LLM provider is blocked by the local-only processing policy; answers were not scored.")
        return build_client(
            live,
            provider_id,
            os.environ["EVAL_LLM_API_KEY"],
            os.environ["EVAL_LLM_MODEL"],
            {},
            base_url,
        )
    runtime = LLMRuntime(live, rotating_cipher_from_settings(live))
    engine = create_engine(live.database_url.get_secret_value())
    try:
        async with create_sessionmaker(engine)() as db:
            return await runtime.get_client(db)  # the client outlives the runtime's cache; closed after scoring
    except (LLMNotConfiguredError, LLMUnavailableError):
        raise GateError("no usable active provider in this deployment; answers were not scored.") from None
    finally:
        await engine.dispose()


def render(report: dict) -> str:
    lines = [
        f"Evaluation gate - {report['dataset']['name']} ({report['dataset']['cases']} cases, "
        f"{report['documents']} documents, {report['embedding_model']})",
        "",
    ]
    width = max(len(check["metric"]) for check in report["checks"]) if report["checks"] else 10
    for check in report["checks"]:
        op = "min" if "min" in check else "max"
        value = "missing" if check["value"] is None else f"{check['value']:.4g}"
        mark = "PASS" if check["passed"] else "FAIL"
        lines.append(f"  {mark}  {check['metric']:<{width}}  {value:>8}   {op} {check[op]:g}")
    retrieval = report["retrieval"]
    semantic = report.get("retrieval_semantic_only")
    if semantic:
        compared = ("recall@1", "recall@5", "ndcg@5", "mrr")
        lines.append("\n  Semantic search alone (pre-alignment configuration, not gated): " + ", ".join(
            f"{key} {semantic['metrics'][key]:.4g}" for key in compared if semantic["metrics"].get(key) is not None
        ))
    if retrieval["missed"]:
        lines.append(f"\n  Not retrieved: {', '.join(retrieval['missed'])}")
    if retrieval["leaks"]:
        lines.append(f"  Permission leaks: {', '.join(retrieval['leaks'])}")
    if report.get("answers", {}).get("unevaluated"):
        lines.append(f"  Needs human review: {', '.join(report['answers']['unevaluated'])}")
    for failure in report.get("answers", {}).get("failures", []):
        lines.append(f"  Answer {failure['id']}: {failure['judgment']} ({failure['stage']})")
    lines.append("\nPASSED" if report["passed"] else "\nFAILED")
    return "\n".join(lines)


def write_report(report: dict) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS / f"gate-{stamp}{'-answers' if 'answers' in report else ''}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    sys.exit(main())
