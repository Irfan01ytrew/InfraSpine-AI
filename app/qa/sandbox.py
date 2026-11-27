"""Disposable loopback PostgreSQL only. Never derives a target from application configuration."""
import base64
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from .catalog import ROOT

def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]

def clean_environment(directory):
    # Inherit OS/runtime discovery only, never database, provider or production settings.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME", "LANG", "LD_LIBRARY_PATH"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(APP_ENV="testing", DATABASE_URL="postgresql+asyncpg://invalid@127.0.0.1:1/qa_no_application",
               MASTER_ENCRYPTION_KEY=base64.b64encode(secrets.token_bytes(32)).decode(),
               QA_TENANT_ID="12121212-1212-4212-8212-121212121212", SESSION_COOKIE_SECURE="false",
               RAG_DATA_DIR=str(directory / "rag"), RAG_MODELS_DIR=str(directory / "models"), RAG_WORKER_ENABLED="false",
               RAG_INDEX_SYNC_SECONDS="0", RAG_EMBEDDING_WARMUP="false", LOG_DIR="", PYTHONUTF8="1",
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONPATH=str(ROOT))
    return env

@contextmanager
def disposable_postgres(pg_bin: Path):
    pg_bin = pg_bin.resolve()
    tools = {n: pg_bin / (n + (".exe" if os.name == "nt" else "")) for n in ("initdb", "pg_ctl", "psql")}
    if not all(p.is_file() for p in tools.values()):
        raise RuntimeError("PostgreSQL initdb, pg_ctl and psql are required")
    root = Path(tempfile.mkdtemp(prefix="qa12-pg-")).resolve()
    cluster, port = root / "cluster", free_port()
    env = clean_environment(root)
    admin_password, test_password = secrets.token_hex(32), secrets.token_hex(32)
    password_file = root / "bootstrap-password"
    password_file.write_text(admin_password, encoding="ascii")
    def command(name, *args, **kwargs):
        result = subprocess.run([str(tools[name]), *map(str, args)], env=env, cwd=ROOT, timeout=90,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        if result.returncode:
            raise RuntimeError(f"Disposable PostgreSQL {name} failed (exit {result.returncode}); no live target used")
    stopped = False
    try:
        try:
            command("initdb", "-D", cluster, "-U", "qa_bootstrap", "-A", "scram-sha-256", "--locale=C", "--encoding=UTF8", "--pwfile", password_file)
        finally:
            password_file.unlink(missing_ok=True)
        command("pg_ctl", "-D", cluster, "-l", root / "postgres.log", "-o", f"-p {port} -h 127.0.0.1", "-w", "start")
        env["PGPASSWORD"] = admin_password
        command("psql", "-X", "-q", "-h", "127.0.0.1", "-p", port, "-U", "qa_bootstrap", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                input=f"CREATE ROLE qa_owner LOGIN PASSWORD '{test_password}';\nCREATE DATABASE qa12_test OWNER qa_owner;\n".encode())
        env.pop("PGPASSWORD")
        env["TEST_DATABASE_URL"] = f"postgresql+asyncpg://qa_owner:{test_password}@127.0.0.1:{port}/qa12_test"
        yield env
    finally:
        if (cluster / "postmaster.pid").exists():
            command("pg_ctl", "-D", cluster, "-m", "fast", "-w", "stop")
        stopped = not (cluster / "postmaster.pid").exists()
        if stopped and root.parent == Path(tempfile.gettempdir()).resolve() and root.name.startswith("qa12-pg-"):
            shutil.rmtree(root)
