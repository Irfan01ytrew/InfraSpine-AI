"""Module 08 QA: disposable PostgreSQL runner, adapted from irfanix_postgres_tests.py."""

import argparse
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", type=Path, required=True)
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=180,
        help="Timeout for disposable PostgreSQL setup/teardown commands (default: 180).",
    )
    parser.add_argument("--retrieval-eval", action="store_true", help="Run the synthetic multilingual retrieval benchmark.")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    port = args.port if args.port is not None else available_port()
    if not 1024 <= port <= 65535:
        parser.error("Test port must be between 1024 and 65535.")
    if not 10 <= args.timeout_seconds <= 1800:
        parser.error("--timeout-seconds must be between 10 and 1800.")
    pytest_args = args.pytest_args
    if pytest_args and pytest_args[0] == "--":
        pytest_args = pytest_args[1:]
    if not pytest_args and not args.retrieval_eval:
        parser.error("Provide explicit pytest targets after --.")
    if pytest_args and args.retrieval_eval:
        parser.error("Choose the retrieval benchmark or pytest targets.")
    database = "irfanix_retrieval_evalgate" if args.retrieval_eval else "infraspine_ai_test"
    tools = {name: args.pg_bin.resolve() / (name + (".exe" if os.name == "nt" else ""))
             for name in ("initdb", "pg_ctl", "psql")}
    if any(not path.is_file() for path in tools.values()):
        parser.error("The PostgreSQL bin directory must contain initdb, pg_ctl and psql.")
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PG")}
    password = secrets.token_hex(32)
    bootstrap_password = secrets.token_hex(32)
    root = Path(tempfile.mkdtemp(prefix="irfanix-pg-test-"))
    cluster = root / "cluster"
    bootstrap = root / "bootstrap-password"
    bootstrap.write_text(bootstrap_password, encoding="ascii")

    def postgres(tool, *values, **kwargs):
        return subprocess.run([str(tools[tool]), *map(str, values)], cwd=ROOT,
                              check=True, timeout=args.timeout_seconds, env=env, **kwargs)

    try:
        try:
            postgres("initdb", "-D", cluster, "-U", "test_bootstrap", "-A", "scram-sha-256",
                     "--locale=C", "--encoding=UTF8", "--pwfile", bootstrap,
                     stdout=subprocess.DEVNULL)
        finally:
            bootstrap.unlink(missing_ok=True)
        postgres("pg_ctl", "-D", cluster, "-l", root / "postgres.log",
                 "-o", f"-p {port} -h 127.0.0.1", "-w", "start")
        env["PGPASSWORD"] = bootstrap_password
        sql = f"""
        CREATE ROLE irfanix_test LOGIN PASSWORD '{password}';
        CREATE DATABASE {database} OWNER irfanix_test;
        """
        postgres("psql", "-X", "-q", "-h", "127.0.0.1", "-p", port,
                 "-U", "test_bootstrap", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                 input=sql, text=True, stderr=subprocess.DEVNULL)
        env.pop("PGPASSWORD")
        env.pop("INFASPINE_TEST_SCHEMA", None)
        env.pop("TEST_DATABASE_ALLOW_SHARED_SCHEMA", None)
        url = f"postgresql+asyncpg://irfanix_test:{password}@127.0.0.1:{port}/{database}"
        if args.retrieval_eval:
            env["EVAL_DATABASE_URL"] = url
            command = [sys.executable, "evals/run_multilingual_retrieval.py"]
        else:
            env["TEST_DATABASE_URL"] = url
            env["DATABASE_URL"] = "postgresql+asyncpg://unused@127.0.0.1/continuity_application"
            command = [sys.executable, "-m", "pytest", "--tb=line", *pytest_args]
        return subprocess.run(command, cwd=ROOT, env=env).returncode
    finally:
        if (cluster / "postmaster.pid").exists():
            postgres("pg_ctl", "-D", cluster, "-m", "fast", "-w", "stop")
        # The path is generated for this run only. Retain it if PostgreSQL failed to stop.
        if root.parent == Path(tempfile.gettempdir()).resolve() and root.name.startswith("irfanix-pg-test-"):
            shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())
