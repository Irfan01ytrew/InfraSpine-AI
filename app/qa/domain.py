"""Pure fail-closed scoring. No provider, database, subprocess or network side effects."""
from __future__ import annotations
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone

STATUSES = ("passed", "failed", "blocked", "unverified")

def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()

def redact(text: str) -> str:
    text = re.sub(r"(?i)(postgres(?:ql)?(?:\+\w+)?|https?)://[^\s/@]+:[^\s/@]+@", r"\1://[REDACTED]@", text)
    text = re.sub(r"(?i)((?:api[_-]?key|password|token|secret|authorization|cookie)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", text)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)[:2000]

def junit_results(payload: bytes) -> list[dict]:
    if len(payload) > 8_000_000 or b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("Unsafe or oversized JUnit evidence")
    root = ET.fromstring(payload)
    if root.tag not in {"testsuites", "testsuite"}:
        raise ValueError("Invalid JUnit root")
    result = []
    for case in root.iter("testcase"):
        if len(result) >= 20000:
            raise ValueError("Too many JUnit cases")
        error, failure, skipped = case.find("error"), case.find("failure"), case.find("skipped")
        status = "blocked" if error is not None else "failed" if failure is not None else "blocked" if skipped is not None else "passed"
        detail = error if error is not None else failure if failure is not None else skipped
        duration = float(case.get("time", "0"))
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("Invalid test duration")
        # Never ingest stdout/stderr or assertion locals: they may contain credentials or company data.
        result.append({"test_id": (case.get("classname", "") + "::" + case.get("name", ""))[:500],
                       "status": status, "duration_ms": round(duration * 1000, 3),
                       "classification": "setup_error" if error is not None else "assertion" if failure is not None else "skipped" if skipped is not None else "success",
                       "message": redact(detail.get("message", "")) if detail is not None else ""})
    if not result:
        return [{"test_id": "collection", "status": "blocked", "duration_ms": 0,
                 "classification": "no_tests", "message": "No tests were collected; no acceptance credit."}]
    return result

def outcome(results: list[dict], exit_code: int) -> str:
    if any(x["status"] == "failed" for x in results):
        return "failed"
    if exit_code != 0 or not results or any(x["status"] == "blocked" for x in results):
        return "blocked"
    return "passed" if all(x["status"] == "passed" for x in results) else "unverified"

def performance_comparison(baseline: dict, candidate: dict, max_regression: float = .2) -> dict:
    if not math.isfinite(max_regression) or not 0 <= max_regression <= 1:
        raise ValueError("Invalid performance threshold")
    fields = ("dataset_sha256", "model", "tools_sha256", "environment", "scorer_version", "metric", "unit")
    if any(not baseline.get(k) or baseline.get(k) != candidate.get(k) for k in fields):
        return {"status": "blocked", "reason": "Comparison conditions differ or are missing"}
    a, b = baseline.get("value"), candidate.get("value")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (a, b)) or a <= 0 or b < 0:
        return {"status": "blocked", "reason": "Missing or invalid measured values"}
    regression = (b - a) / a
    return {"status": "failed" if regression > max_regression else "passed", "regression": regression,
            "baseline": a, "candidate": b, "threshold": max_regression}

def release_report(requirements: list[dict], runs: list[dict], *, source_digest: str, policy: dict,
                   now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    matrix = []
    for requirement in requirements:
        row = dict(requirement)
        row.update(status="Implemented but Unverified" if row.pop("implemented") else "Not Implemented",
                   evidence_run_id=None, reason="No current acceptance evidence")
        candidates = [r for r in runs if row["id"] in r.get("verifies", [])]
        if candidates:
            run = max(candidates, key=lambda r: r["created_at"])
            row["evidence_run_id"] = run["id"]
            stamp = datetime.fromisoformat(run["created_at"])
            age = (now - stamp).total_seconds()
            if run["source_digest"] != source_digest or age < 0 or age > policy["max_age_hours"] * 3600:
                row.update(status="Blocked", reason="Evidence is stale or belongs to another source snapshot")
            elif not run.get("evidence_sha256") or not run.get("results"):
                row.update(status="Blocked", reason="Missing test evidence")
            else:
                status = run["status"]
                row.update(status={"passed": "Verified", "failed": "Failed", "blocked": "Blocked"}.get(status, "Implemented but Unverified"),
                           reason=f"{run['suite_id']}: {status}")
        matrix.append(row)
    blockers = [r["id"] for r in matrix if r["mandatory"] and r["status"] != "Verified"]
    # Any current failed suite is release blocking even if it does not certify a whole requirement.
    latest = {}
    for run in sorted(runs, key=lambda r: r["created_at"]):
        if run["source_digest"] == source_digest:
            latest[run["suite_id"]] = run
    failed_suites = [r["id"] for r in latest.values() if r["status"] in {"failed", "blocked", "queued", "running", "cancelled"}]
    residual = [r["id"] for r in matrix if not r["mandatory"] and r["status"] != "Verified"]
    decision = "NO-GO" if blockers or failed_suites else "CONDITIONAL GO" if residual else "GO"
    report = {"decision": decision, "source_digest": source_digest, "policy": policy,
              "matrix": matrix, "counts": dict(Counter(r["status"] for r in matrix)),
              "blockers": blockers, "failed_suites": failed_suites, "residual_risks": residual,
              "evaluated_at": now.isoformat()}
    report["snapshot_sha256"] = digest({k: v for k, v in report.items() if k != "evaluated_at"})
    return report
