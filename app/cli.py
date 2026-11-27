"""Operational commands.

    python -m app.cli generate-master-key
    python -m app.cli create-user --email admin@example.com --role admin
    python -m app.cli set-password --email admin@example.com
    python -m app.cli rag-rebuild-index      (stop the server first; or use POST /api/rag/index/rebuild)
    echo "<password>" | python -m app.cli create-user --email ... --role admin --password-stdin
"""

import argparse
import asyncio
import base64
import getpass
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.core.config import DatabaseSettings, get_settings
from app.core.crypto import CredentialCipher
from app.core.security import MIN_PASSWORD_LENGTH, hash_password
from app.db.models import ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_USER, User
from app.db.session import create_engine, create_sessionmaker
from app.services import auth
from app.services.audit import AuditContext
from app.services.auth import normalize_email

CLI_AUDIT_CONTEXT = AuditContext(actor_user_id=None, actor_email="cli", ip_address=None, request_id=None)


def generate_master_key() -> None:
    print(base64.b64encode(secrets.token_bytes(32)).decode())


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        print("(Typing is hidden. On Windows, paste with right-click; Ctrl+V does not work here.)")
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm password: "):
            sys.exit("Passwords do not match.")
    if any(not ch.isprintable() for ch in password):
        # Typically a Ctrl+V keystroke captured as a control character instead of pasted text.
        sys.exit("Password contains invisible/control characters. Type it, or paste with right-click.")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return password


async def create_user(email: str, role: str, password: str) -> None:
    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    try:
        async with create_sessionmaker(engine)() as db:
            email = normalize_email(email)
            if (await db.execute(select(User).where(User.email == email))).scalar_one_or_none():
                sys.exit(f"User {email} already exists. Use set-password to change its password.")
            db.add(User(email=email, role=role, password_hash=hash_password(password)))
            await db.commit()
            print(f"Created {role} user {email}.")
    finally:
        await engine.dispose()


async def set_password(email: str, password: str) -> None:
    engine = create_engine(DatabaseSettings().database_url.get_secret_value())
    try:
        async with create_sessionmaker(engine)() as db:
            if not await auth.set_password(db, email, password, CLI_AUDIT_CONTEXT):
                sys.exit(f"No user with email {normalize_email(email)}.")
            print(f"Password updated for {normalize_email(email)}; lockout cleared and sessions signed out.")
    finally:
        await engine.dispose()


async def rag_rebuild_index() -> None:
    """Rebuild the vector index from embeddings stored in PostgreSQL (no PDF re-reading)."""
    from app.rag.components import build_rag

    settings = get_settings()
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        rag = build_rag(settings, create_sessionmaker(engine))  # refuses while the API process owns the index
        try:
            result = await rag.indexer.reconcile(full=True)
            print(f"Vector index rebuilt: {result['vectors']} vectors.")
        finally:
            rag.close()
    finally:
        await engine.dispose()


async def agent_queue_worker(once: bool = False) -> None:
    """Run only durable, internal read-only agent jobs; document and workflow queues are untouched."""
    from app.rag.components import build_rag
    from app.services.agent_worker import process_next_agent_run, recover_expired_agent_queue_runs, run_agent_worker
    from app.services.llm_runtime import LLMRuntime

    settings = get_settings()
    engine = create_engine(settings.database_url.get_secret_value())
    sessionmaker = create_sessionmaker(engine)
    cipher = CredentialCipher(settings.master_key_bytes, settings.master_encryption_key_id)
    rag = build_rag(settings, sessionmaker)
    runtime = LLMRuntime(settings, cipher)
    stop = asyncio.Event()
    try:
        if once:
            recovered = await recover_expired_agent_queue_runs(sessionmaker)
            processed = await process_next_agent_run(sessionmaker, settings, rag, cipher, runtime)
            print(f"Agent queue pass complete; recovered={recovered}; processed={str(processed).lower()}.")
        else:
            await run_agent_worker(
                sessionmaker,
                settings,
                rag,
                cipher,
                runtime,
                stop,
                interval_seconds=settings.agent_queue_poll_seconds,
            )
    finally:
        rag.close()
        await runtime.close()
        await engine.dispose()


async def rag_import_folder(
    folder: Path, admin_email: str, visibility: str, department_id: str | None,
    source_id: str | None, retry_failed: bool,
) -> None:
    """Import/synchronize a local folder. Re-run with the same source ID to resume safely."""
    from app.rag.bulk_import import import_folder

    settings = get_settings()
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        result = await import_folder(
            create_sessionmaker(engine), settings, folder, admin_email=admin_email,
            visibility=visibility, department_id=department_id, source_id=source_id,
            retry_failed=retry_failed,
        )
        print(json.dumps(result, sort_keys=True))
        if result["failed"]:
            sys.exit(2)
    finally:
        await engine.dispose()


def db_backup(destination: Path | None = None) -> None:
    from app.ops.postgres_backup import create_backup

    url = DatabaseSettings().database_url.get_secret_value()
    database = make_url(url).database or "postgres"
    if destination is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = Path(".backups") / f"{database}_{stamp}.dump"
    print(json.dumps(create_backup(url, destination), sort_keys=True))


def db_verify_backup(backup: Path) -> None:
    from app.ops.postgres_backup import verify_backup

    print(json.dumps({"path": str(backup.resolve()), "catalog_entries": verify_backup(backup), "verified": True}))


async def db_restore_empty(backup: Path) -> None:
    from app.ops.postgres_backup import restore_into_empty_database

    url = DatabaseSettings().database_url.get_secret_value()
    print(json.dumps(await restore_into_empty_database(url, backup), sort_keys=True))


def deployment_snapshot_create(database_backup: Path, output: Path, application_data: Path, qdrant_data: Path | None) -> None:
    from app.ops.deployment_snapshot import create_deployment_snapshot

    result = create_deployment_snapshot(output, database_backup, application_data, qdrant_data)
    print(json.dumps(result, sort_keys=True))


def deployment_snapshot_verify(path: Path) -> None:
    from app.ops.deployment_snapshot import verify_deployment_snapshot

    print(json.dumps(verify_deployment_snapshot(path), sort_keys=True))


def deployment_snapshot_restore_files(path: Path, application_data_target: Path, qdrant_data_target: Path | None) -> None:
    from app.ops.deployment_snapshot import restore_snapshot_files

    print(json.dumps(restore_snapshot_files(path, application_data_target, qdrant_data_target), sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-master-key", help="Print a new random 256-bit key (base64)")
    create = sub.add_parser("create-user", help="Create a user account")
    create.add_argument("--email", required=True)
    create.add_argument("--role", choices=[ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_USER], default=ROLE_EMPLOYEE)
    create.add_argument("--password-stdin", action="store_true", help="Read the password from stdin")
    reset = sub.add_parser("set-password", help="Reset a user's password and clear any lockout")
    reset.add_argument("--email", required=True)
    reset.add_argument("--password-stdin", action="store_true", help="Read the password from stdin")
    sub.add_parser("rag-rebuild-index", help="Rebuild the RAG vector index from the database")
    agent_worker = sub.add_parser(
        "agent-worker", help="Run the durable read-only agent queue worker (only the agent queue is processed)"
    )
    agent_worker.add_argument("--once", action="store_true", help="Process at most one due agent run and exit")
    folder = sub.add_parser(
        "rag-import-folder", help="Import or incrementally synchronize a local folder; repeat to resume"
    )
    folder.add_argument("--path", type=Path, required=True)
    folder.add_argument("--admin-email", required=True, help="Existing administrator account that owns new records")
    folder.add_argument("--visibility", choices=["private", "restricted", "shared"], default="restricted")
    folder.add_argument("--department-id", help="Required for restricted folder imports")
    folder.add_argument("--source-id", help="Stable import identity; defaults to a hash of the resolved folder path")
    folder.add_argument("--no-retry-failed", action="store_true", help="Leave failed unchanged files for manual reprocess")
    backup = sub.add_parser("db-backup", help="Create a verified custom-format PostgreSQL backup without overwriting files")
    backup.add_argument("--output", type=Path)
    verify = sub.add_parser("db-verify-backup", help="Verify a PostgreSQL backup catalog")
    verify.add_argument("--path", type=Path, required=True)
    restore = sub.add_parser("db-restore-empty", help="Restore into the configured database only if it is empty")
    restore.add_argument("--path", type=Path, required=True)
    snapshot = sub.add_parser("deployment-snapshot-create", help="Create an integrity-checked snapshot of quiesced local data and a verified PostgreSQL dump")
    snapshot.add_argument("--database-backup", type=Path, required=True)
    snapshot.add_argument("--application-data", type=Path, required=True)
    snapshot.add_argument("--qdrant-data", type=Path, help="Stopped Qdrant storage directory, when Qdrant is enabled")
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot_verify = sub.add_parser("deployment-snapshot-verify", help="Verify snapshot manifest, all file hashes and PostgreSQL archive")
    snapshot_verify.add_argument("--path", type=Path, required=True)
    snapshot_restore = sub.add_parser("deployment-snapshot-restore-files", help="Restore snapshot files to new paths only; restore the database separately")
    snapshot_restore.add_argument("--path", type=Path, required=True)
    snapshot_restore.add_argument("--application-data-target", type=Path, required=True)
    snapshot_restore.add_argument("--qdrant-data-target", type=Path)
    args = parser.parse_args()

    if args.command == "generate-master-key":
        generate_master_key()
    elif args.command == "create-user":
        asyncio.run(create_user(args.email, args.role, _read_password(args.password_stdin)))
    elif args.command == "set-password":
        asyncio.run(set_password(args.email, _read_password(args.password_stdin)))
    elif args.command == "rag-rebuild-index":
        asyncio.run(rag_rebuild_index())
    elif args.command == "agent-worker":
        asyncio.run(agent_queue_worker(once=args.once))
    elif args.command == "rag-import-folder":
        asyncio.run(
            rag_import_folder(
                args.path, args.admin_email, args.visibility, args.department_id,
                args.source_id, retry_failed=not args.no_retry_failed,
            )
        )
    elif args.command == "db-backup":
        db_backup(args.output)
    elif args.command == "db-verify-backup":
        db_verify_backup(args.path)
    elif args.command == "db-restore-empty":
        asyncio.run(db_restore_empty(args.path))
    elif args.command == "deployment-snapshot-create":
        deployment_snapshot_create(args.database_backup, args.output, args.application_data, args.qdrant_data)
    elif args.command == "deployment-snapshot-verify":
        deployment_snapshot_verify(args.path)
    elif args.command == "deployment-snapshot-restore-files":
        deployment_snapshot_restore_files(args.path, args.application_data_target, args.qdrant_data_target)


if __name__ == "__main__":
    main()
