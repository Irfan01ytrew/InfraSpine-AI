"""JSON and text reports. The fingerprint covers ranking metrics, not wall-clock latency."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from app.rag.evaluation.dataset import EvaluationError
from app.rag.evaluation.paths import resolve_inside

_ROUND = 6


def rounding(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), _ROUND)


def fingerprint(stable: dict) -> str:
    payload = json.dumps(stable, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def render_json(report: dict) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def render_text(report: dict) -> str:
    if report.get("schema_version") == 2:
        return _render_answers(report)
    metrics = report["metrics"]
    baseline = report["baseline"]
    dataset = report["dataset"]
    lines = [
        "InfraSpine retrieval evaluation",
        f"Baseline: FAISS + {baseline['embedding_model']} (dim {baseline['embedding_dimension']})",
        f"Dataset: {dataset['name']} ({dataset['cases']} cases, sha256 {dataset['sha256'][:12]})",
        f"Fingerprint: {report['fingerprint']}",
    ]
    if report.get("generated_at"):
        lines.append(f"Generated: {report['generated_at']}")
    lines.extend(
        [
            (
                "Path: PostgreSQL readable_by, then FAISS search restricted to those chunks "
                f"(min similarity {report['parameters']['min_similarity']}, "
                f"context budget {report['parameters']['max_context_chars']} chars)."
            ),
            "",
            _metric_line("Recall", metrics, "recall@"),
            _metric_line("nDCG", metrics, "ndcg@"),
            f"MRR                 {_fmt(metrics.get('mrr'))}",
            f"Citation precision  {_fmt(metrics.get('citation_precision'))}",
            f"Abstention accuracy {_fmt(metrics.get('abstention_accuracy'))}  "
            f"({metrics['abstention_cases']} cases)",
            _latency_line(metrics.get("latency_ms") or {}),
            "",
            "By language",
        ]
    )
    for language, row in report["by_language"].items():
        lines.append(
            f"  {language:<10} cases={row['cases']}  recall={_fmt(row.get('recall'))}  "
            f"mrr={_fmt(row.get('mrr'))}  citation_precision={_fmt(row.get('citation_precision'))}  "
            f"abstention={_fmt(row.get('abstention_accuracy'))}"
        )
    lines.extend(["", "Cases"])
    for case in report["cases"]:
        if case["expect_abstain"]:
            mark = "abstained" if case["abstention_correct"] else "leaked"
        elif case["first_relevant_rank"]:
            mark = f"hit rank {case['first_relevant_rank']}"
        else:
            mark = "miss"
        lines.append(
            f"  [{mark}] {case['id']}  {case['language']}  persona={case['persona']}  "
            f"retrieved={case['retrieved']}  gold={case['gold_chunks']}"
        )
    lines.append("")
    return "\n".join(lines)


def write_report(root: Path, dataset_name: str, report: dict, *, generated_at: datetime | None = None) -> tuple[Path, Path]:
    """Write JSON and text under `root/reports`. Both paths are checked to stay inside `root`."""
    stamp = generated_at or datetime.now(UTC)
    directory = resolve_inside(root, Path("reports"))
    stem = f"{dataset_name}-{stamp.strftime('%Y%m%dT%H%M%SZ')}-{report['fingerprint'][:16]}"
    json_path = resolve_inside(root, directory / f"{stem}.json")
    text_path = resolve_inside(root, directory / f"{stem}.txt")
    body = dict(report)
    body["generated_at"] = stamp.isoformat()
    json_path.write_text(render_json(body), encoding="utf-8")
    text_path.write_text(render_text(body), encoding="utf-8")
    if not json_path.is_relative_to(root) or not text_path.is_relative_to(root):
        raise EvaluationError("Refusing to write an evaluation report outside this deployment.")
    return json_path, text_path


def _render_answers(report: dict) -> str:
    metrics = report["metrics"]
    baseline = report["baseline"]
    dataset = report["dataset"]
    provider = report["provider"]
    lines = [
        "InfraSpine grounded-answer evaluation",
        f"Path: {baseline['path']}  FAISS + {baseline['embedding_model']} (dim {baseline['embedding_dimension']})",
        (
            f"Settings: general_answers={baseline['general_answers']}  top_k={baseline['top_k']}  "
            f"min_similarity={baseline['min_similarity']}"
        ),
        f"Rubric: {report['rubric']} (automated; uncertain cases need a person)",
        f"Dataset: {dataset['name']} ({dataset['cases']} cases, sha256 {dataset['sha256'][:12]})",
        f"Provider: {provider['name']}  calls={provider['calls']}  "
        f"prompt_tokens={provider['prompt_tokens']}  completion_tokens={provider['completion_tokens']}",
        f"Fingerprint: {report['fingerprint']}",
    ]
    if report.get("generated_at"):
        lines.append(f"Generated: {report['generated_at']}")
    lines.extend(
        [
            "",
            f"Answer correctness  {_fmt(metrics.get('answer_correctness'))}  "
            f"({metrics['answerable_cases']} answerable, {metrics['unevaluated_cases']} unevaluated)",
            f"Fact presence       {_fmt(metrics.get('fact_presence'))}",
            f"Groundedness        {_fmt(metrics.get('groundedness'))}",
            f"Citation accuracy   {_fmt(metrics.get('citation_accuracy'))}  "
            f"(answerable {_fmt(metrics.get('answerable_citation_accuracy'))})",
            f"Citation support    {_fmt(metrics.get('citation_support'))}",
            f"Abstention accuracy {_fmt(metrics.get('abstention_accuracy'))}  ({metrics['refusal_cases']} refusal cases)",
            f"Unsupported claims  {_fmt(metrics.get('unsupported_claim_rate'))}",
            f"General on refusal  {_fmt(metrics.get('general_knowledge_on_refusal'))}",
            f"Permission isolation {_fmt(metrics.get('permission_isolation'))}",
            _latency_line(metrics.get("latency_ms") or {}),
            "",
            "By language",
        ]
    )
    for language, row in report["by_language"].items():
        lines.append(
            f"  {language:<10} cases={row['cases']}  unevaluated={row['unevaluated']}  "
            f"correct={_fmt(row.get('answer_correctness'))}  "
            f"abstention={_fmt(row.get('abstention_accuracy'))}  "
            f"grounded={_fmt(row.get('groundedness'))}"
        )
    lines.extend(["", "By category"])
    for category, row in report["by_category"].items():
        lines.append(
            f"  {category:<22} cases={row['cases']}  unevaluated={row['unevaluated']}  "
            f"correct={_fmt(row.get('answer_correctness'))}  "
            f"abstention={_fmt(row.get('abstention_accuracy'))}"
        )
    lines.extend(["", "Cases"])
    for case in report["cases"]:
        lines.append(
            f"  [{case['automated_judgment']}] {case['id']}  {case['language']}  {case['category']}  "
            f"stage={case['failure_stage']}  evidence={case['evidence_sufficient']}  "
            f"cited={case['cited']}  retrieved={case['retrieved']}"
        )
    lines.append("")
    return "\n".join(lines)


def _metric_line(label: str, metrics: dict, prefix: str) -> str:
    parts = [f"{key}={_fmt(metrics[key])}" for key in sorted(metrics) if key.startswith(prefix)]
    return f"{label:<20}{', '.join(parts) if parts else 'n/a'}"


def _latency_line(latency: dict) -> str:
    return (
        "Latency ms          "
        f"mean={_fmt(latency.get('mean'))}  p50={_fmt(latency.get('p50'))}  p95={_fmt(latency.get('p95'))}"
    )


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"
