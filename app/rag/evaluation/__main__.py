"""Run a retrieval or grounded-answer evaluation inside this deployment.

    python -m app.rag.evaluation --dataset billing.json --personas local.json --k 1 --k 5 --k 10
    python -m app.rag.evaluation --dataset answers.json --personas local.json --answers

`--dataset` and `--personas` are read from `data/evaluation` (or a path that stays inside it).
Reports are written only to `data/evaluation/reports`. `--answers` calls production `run_chat`
with the configured provider. If that provider is not available, no scores are invented.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.core.config import get_settings
from app.db.session import create_engine, create_sessionmaker
from app.rag.components import build_rag
from app.rag.evaluation.dataset import EvaluationError, load_dataset, load_personas
from app.rag.evaluation.paths import evaluation_root, resolve_inside
from app.rag.evaluation.report import write_report
from app.rag.evaluation.runner import evaluate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.rag.evaluation")
    parser.add_argument("--dataset", required=True, help="Dataset JSON inside data/evaluation")
    parser.add_argument("--personas", required=True, help="Persona-to-email JSON inside data/evaluation")
    parser.add_argument("--k", type=int, action="append", dest="ks", help="Cutoff K (repeatable, 1–20)")
    parser.add_argument(
        "--answers",
        action="store_true",
        help="Score production answers instead of ranking the retrieved chunks",
    )
    args = parser.parse_args(argv)
    try:
        runner = _run_answers if args.answers else _run
        json_path, _text_path = asyncio.run(runner(args.dataset, args.personas, tuple(args.ks or (1, 5, 10))))
    except EvaluationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Wrote {json_path}")
    return 0


async def _run(dataset_arg: str, personas_arg: str, ks: tuple[int, ...]) -> tuple[Path, Path]:
    settings = get_settings()
    root = evaluation_root(settings)
    dataset_path = _locate(root, dataset_arg, "datasets")
    persona_path = _locate(root, personas_arg, "personas")
    dataset = load_dataset(dataset_path)
    personas = load_personas(persona_path)
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        sessionmaker = create_sessionmaker(engine)
        rag = build_rag(settings, sessionmaker)
        report = await evaluate(
            sessionmaker,
            rag.embedder,
            rag.vector_store,
            dataset,
            personas,
            ks=ks,
            min_similarity=settings.rag_min_similarity,
            max_context_chars=settings.rag_max_context_chars,
        )
    finally:
        await engine.dispose()
    json_path, text_path = write_report(root, dataset.name, report)
    print(text_path.read_text(encoding="utf-8"), end="")
    return json_path, text_path


async def _run_answers(dataset_arg: str, personas_arg: str, ks: tuple[int, ...]) -> tuple[Path, Path]:
    """Same deployment files as retrieval evaluation. `ks` is unused; chat uses the configured top-k."""
    del ks
    from app.core.crypto import rotating_cipher_from_settings
    from app.core.errors import LLMNotConfiguredError, LLMUnavailableError
    from app.services.llm_runtime import LLMRuntime

    settings = get_settings()
    root = evaluation_root(settings)
    dataset = load_dataset(_locate(root, dataset_arg, "datasets"))
    personas = load_personas(_locate(root, personas_arg, "personas"))
    engine = create_engine(settings.database_url.get_secret_value())
    runtime = LLMRuntime(settings, rotating_cipher_from_settings(settings))
    try:
        sessionmaker = create_sessionmaker(engine)
        try:
            async with sessionmaker() as db:
                llm = await runtime.get_client(db)
        except (LLMNotConfiguredError, LLMUnavailableError):
            raise EvaluationError(
                "No configured LLM provider is available. Answer results were not produced."
            ) from None
        rag = build_rag(settings, sessionmaker)
        try:
            from app.rag.evaluation.answers import evaluate_answers

            report = await evaluate_answers(sessionmaker, rag, settings, dataset, personas, llm)
        finally:
            rag.close()
    finally:
        await engine.dispose()
        await runtime.close()
    json_path, text_path = write_report(root, dataset.name, report)
    print(text_path.read_text(encoding="utf-8"), end="")
    return json_path, text_path


def _locate(root: Path, value: str, folder: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute() and candidate.parent == Path("."):
        candidate = Path(folder) / candidate
    return resolve_inside(root, candidate)


if __name__ == "__main__":
    sys.exit(main())
