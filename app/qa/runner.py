"""Trusted CLI worker. A fixed suite executes inside a clean disposable QA environment."""
import argparse
import asyncio
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from collections import Counter
from .catalog import ROOT, SUITES
from .domain import digest, junit_results, outcome
from .sandbox import clean_environment, disposable_postgres
from .source import source_identity

def blocked(reason, identity=None):
    return {"status": "blocked", "results": [{"test_id": "runner", "status": "blocked", "duration_ms": 0,
            "classification": reason, "message": reason}], "metrics": {}, "provenance": identity or {}}

def execute(suite_id, *, pg_bin=None):
    if suite_id not in SUITES:
        raise ValueError("Unknown reviewed suite")
    suite, before = SUITES[suite_id], source_identity()
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="qa12-run-") as directory:
        root = Path(directory)
        report = root / "junit.xml"
        command = [sys.executable, "-m", "pytest", "-q", "--tb=no", f"--junitxml={report}", "-o", "junit_family=xunit2"]
        if suite["kind"] == "unit":
            command.append("--noconftest")
        command.extend(suite["targets"])
        def run(env):
            with (root / "process.log").open("wb") as log:
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
                try:
                    return process.wait(timeout=suite["timeout"])
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=30)
                    raise
        try:
            if suite["kind"] == "unit":
                code = run(clean_environment(root))
            elif pg_bin:
                with disposable_postgres(Path(pg_bin)) as env:
                    code = run(env)
            else:
                return blocked("PostgreSQL binaries not configured", before)
            if not report.exists():
                return blocked(f"No JUnit report (process exit {code})", before)
            results = junit_results(report.read_bytes())
        except subprocess.TimeoutExpired:
            return blocked("Suite timeout; disposable environment cleaned up", before)
        except (OSError, RuntimeError, ValueError) as exc:
            return blocked(f"Environment or report failure: {type(exc).__name__}", before)
        after = source_identity()
        status = outcome(results, code)
        if before != after:
            status = "blocked"
            results.append({"test_id": "source", "status": "blocked", "duration_ms": 0,
                            "classification": "source_changed", "message": "Source changed during execution; rerun this snapshot"})
        counts = dict(Counter(x["status"] for x in results))
        return {"status": status, "results": results,
                "metrics": {"duration_ms": round((time.perf_counter() - start) * 1000, 3), "counts": counts,
                            "case_count": len(results), "execution_pass_rate": counts.get("passed", 0) / len(results)},
                "provenance": {**before, "python": platform.python_version(), "platform": platform.system(),
                    "suite_id": suite_id, "targets": suite["targets"], "environment": "disposable-loopback-postgresql" if suite["kind"] != "unit" else "isolated-unit",
                    "provider": "deterministic test provider; no model quality claim", "exit_code": code,
                    "junit_sha256": hashlib.sha256(report.read_bytes()).hexdigest(), "scorer_version": "qa12-v1"}}

async def worker_once(url, tenant, owner, pg_bin):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy import select
    from app.db.models import User
    from . import service
    engine = create_async_engine(url, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            user = await db.scalar(select(User).where(User.id == owner, User.role == "admin", User.is_active.is_(True)))
            if not user:
                raise RuntimeError("Worker owner is not an active administrator")
            row = await service.claim(db, tenant, owner)
            if not row:
                return None
            if row.source_digest != source_identity()["source_digest"]:
                evidence = blocked("Queued source differs from worker checkout")
            else:
                evidence = await asyncio.to_thread(execute, row.suite_id, pg_bin=pg_bin)
            return await service.finish(db, tenant, owner, row.id, row.claim_token, evidence)
    finally:
        await engine.dispose()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=[*SUITES, "worker"])
    parser.add_argument("--pg-bin", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--owner", type=uuid.UUID)
    args = parser.parse_args()
    if args.suite == "worker":
        from sqlalchemy.engine import make_url
        from .service import tenant_id
        url = os.environ.get("QA_DATABASE_URL", "")
        parsed = make_url(url)
        if not args.owner or parsed.host not in {"localhost", "127.0.0.1"} or not (parsed.database or "").endswith("_test"):
            parser.error("Worker requires --owner and a loopback QA_DATABASE_URL ending in _test")
        result = asyncio.run(worker_once(url, tenant_id(), args.owner, args.pg_bin))
        print(json.dumps(result or {"status": "idle"}))
        return 0
    result = execute(args.suite, pg_bin=args.pg_bin)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"suite": args.suite, "status": result["status"], "metrics": result["metrics"]}))
    return 0 if result["status"] == "passed" else 1

if __name__ == "__main__":
    raise SystemExit(main())
