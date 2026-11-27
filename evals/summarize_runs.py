"""Median and range across repeated answer runs, so a one-case change is not mistaken for a regression.

    .venv/Scripts/python evals/summarize_runs.py --workloads <report> <report> <report> --gate <report> <report> <report>

Answer metrics vary between runs of the same model on the same passages. This reads several reports of the enterprise
workload suite (`evals/run_enterprise_workloads.py --answers`) and of the golden gate (`gate --answers`) and prints,
per metric, the median and the range, with false refusals, unsupported claims and security failures in their own
sections, and how often each case failed (a case failing in every run is a defect; in one run, variance).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

WORKLOAD_QUALITY = ("pass_rate", "answer_correctness", "refusal_accuracy", "evidence_hit_rate", "document_hit_rate")
WORKLOAD_REFUSALS = ("false_refusals", "general_on_document_question", "cited_non_answers")
WORKLOAD_UNSUPPORTED = ("unsupported_claims",)
WORKLOAD_SECURITY = ("acl_leaks", "history_leaks")
GATE_QUALITY = ("answer_correctness", "groundedness", "citation_accuracy", "abstention_accuracy",
                "recall@1", "recall@5", "ndcg@5", "mrr")
GATE_REFUSALS = ("false_refusals",)
GATE_UNSUPPORTED = ("unsupported_claim_rate",)
GATE_SECURITY = ("permission_leaks", "permission_isolation")


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _band(values: list) -> str:
    known = [value for value in values if isinstance(value, (int, float))]
    if not known:
        return "n/a"
    return f"median {statistics.median(known):.4g}  range {min(known):.4g}-{max(known):.4g}  runs {', '.join(f'{v:.4g}' for v in known)}"


def workloads(paths: list[str]) -> list[str]:
    reports = [_load(path) for path in paths]
    summaries = [report["answers"]["summary"]["overall"] for report in reports]
    lines = [f"Enterprise workloads, {len(reports)} runs"]
    for title, keys in (("quality", WORKLOAD_QUALITY), ("false refusals", WORKLOAD_REFUSALS),
                        ("unsupported claims", WORKLOAD_UNSUPPORTED), ("security", WORKLOAD_SECURITY)):
        lines.append(f"  [{title}]")
        lines += [f"    {key:30} {_band([summary.get(key) for summary in summaries])}" for key in keys]
    lines.append("  [cases that failed]")
    failed: dict[str, list[str]] = {}
    for report in reports:
        for row in report["answers"]["cases"]:
            if not row["answer"]["passed"]:
                failed.setdefault(row["id"], []).append(f"{row['answer']['judgment']}/{row['answer']['failure_stage']}")
    for case_id, outcomes in sorted(failed.items(), key=lambda item: -len(item[1])):
        lines.append(f"    {case_id:34} failed {len(outcomes)}/{len(reports)}: {', '.join(outcomes)}")
    return lines


def gate(paths: list[str]) -> list[str]:
    reports = [_load(path) for path in paths]
    metrics = [{**report["retrieval"]["metrics"], **report.get("answers", {}).get("metrics", {})} for report in reports]
    lines = [f"Golden gate, {len(reports)} runs"]
    for title, keys in (("quality", GATE_QUALITY), ("false refusals", GATE_REFUSALS),
                        ("unsupported claims", GATE_UNSUPPORTED), ("security", GATE_SECURITY)):
        lines.append(f"  [{title}]")
        lines += [f"    {key:30} {_band([metric.get(key) for metric in metrics])}" for key in keys]
    lines.append("  [cases that failed]")
    failed: dict[str, list[str]] = {}
    for report in reports:
        for failure in report.get("answers", {}).get("failures", []):
            failed.setdefault(failure["id"], []).append(f"{failure['judgment']}/{failure['stage']}")
    for case_id, outcomes in sorted(failed.items(), key=lambda item: -len(item[1])):
        lines.append(f"    {case_id:34} failed {len(outcomes)}/{len(reports)}: {', '.join(outcomes)}")
    return lines


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workloads", nargs="*", default=[], help="enterprise workload reports (with --answers)")
    parser.add_argument("--gate", nargs="*", default=[], help="golden gate reports (with --answers)")
    args = parser.parse_args()
    lines: list[str] = []
    if args.workloads:
        lines += workloads(args.workloads) + [""]
    if args.gate:
        lines += gate(args.gate)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
