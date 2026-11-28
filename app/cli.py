"""Operational commands.

    python -m app.cli generate-master-key
    python -m app.cli create-user --email admin@example.com --role admin
    python -m app.cli set-password --email admin@example.com
    python -m app.cli rag-rebuild-index      (stop the server first; or use POST /api/rag/index/rebuild)
    python -m app.cli forecast-scan --tenant-id <uuid> --snapshot twin.json [--loop]
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
from app.core.crypto import rotating_cipher_from_settings
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
    cipher = rotating_cipher_from_settings(settings)
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


async def db_backup_cycle(output_dir: Path | None = None, drill_database: str | None = None) -> None:
    """Step AO slice 2: one scheduled backup + restore drill; exit 2 when the cycle does not pass."""
    from app.ops.backup_schedule import run_backup_cycle

    url = DatabaseSettings().database_url.get_secret_value()
    database = make_url(url).database or "postgres"
    admin_url = str(make_url(url).set(database="postgres"))
    if output_dir is None:
        output_dir = Path(".backups")
    engine = create_engine(url, pool_size=1, max_overflow=0)
    try:
        summary = await run_backup_cycle(
            source_url=url,
            backup_dir=output_dir,
            admin_url=admin_url,
            receipt_engine=engine,
            drill_db_name=drill_database,
        )
    finally:
        await engine.dispose()
    print(json.dumps(summary, sort_keys=True))
    if not summary["passed"]:
        sys.exit(2)


async def db_migrate_safe(
    check_only: bool, last_migration_at: datetime | None, rpo_hours: float
) -> None:
    """Step AO slice 6: backup-before-migrate gate; exit 2 when the gate refuses."""
    from app.ops.migration_safety import run_migrate_safe

    url = DatabaseSettings().database_url.get_secret_value()
    database = make_url(url).database or "postgres"
    engine = create_engine(url, pool_size=1, max_overflow=0)
    try:
        decision = await run_migrate_safe(
            engine,
            database,
            last_migration_at,
            datetime.now(UTC),
            rpo_hours=rpo_hours,
            check_only=check_only,
        )
    finally:
        await engine.dispose()
    print(json.dumps(decision, sort_keys=True))
    if not decision["allowed"] or (not check_only and not decision["migrated"]):
        sys.exit(2)


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


async def forecast_scan(args) -> int:
    """Standing forecast scan (Step L): scan a twin snapshot, record it, watch it.

    One pass (``--once`` behaviour is the default): load the snapshot
    JSON, run the fingerprint-gated scan, persist new forecasts with
    ``record_scan`` (idempotent per fingerprint), evaluate the watch
    rules and print the advisory alert lines. ``--dry-run`` scans and
    evaluates without touching the database or the state file.
    ``--loop`` repeats passes every ``--interval-seconds`` until
    stopped (SIGINT/SIGTERM); the snapshot file is re-read each
    pass so a refreshed twin export is picked up. Exit 0 on a
    completed pass (including an unchanged twin), 2 on a usage or
    validation error.
    """
    import signal
    from uuid import UUID as _UUID

    from app.predictive_continuity.scheduler import (
        build_scan_report,
        format_scan_summary,
        load_snapshot,
        load_state,
        run_forecast_scan,
        save_state,
    )

    try:
        tenant_id = _UUID(args.tenant_id)
    except ValueError:
        print(f"Invalid --tenant-id (must be a UUID): {args.tenant_id}", file=sys.stderr)
        return 2
    critical_ids = [part for part in (args.critical_ids or "").split(",") if part] or None
    horizons = None
    if args.horizons:
        try:
            horizons = [int(part) for part in args.horizons.split(",") if part]
        except ValueError:
            print("--horizons must be comma-separated integers.", file=sys.stderr)
            return 2
    created_by = None
    if args.created_by:
        try:
            created_by = _UUID(args.created_by)
        except ValueError:
            print(f"Invalid --created-by (must be a UUID): {args.created_by}", file=sys.stderr)
            return 2
    if args.dry_run and args.loop:
        print("--dry-run and --loop cannot be combined.", file=sys.stderr)
        return 2
    if args.interval_seconds < 60:
        print("--interval-seconds must be at least 60.", file=sys.stderr)
        return 2
    try:
        snapshot = load_snapshot(args.snapshot)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.dry_run:
        try:
            report = build_scan_report(
                snapshot, critical_ids=critical_ids, horizons=horizons)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps({
            "dry_run": True,
            "fingerprint": report["fingerprint"],
            "refreshed": report["refreshed"],
            "scanned_at": report["scanned_at"],
            "counts": report["counts"],
            "alerts": report["alerts"],
            "suppressed": len(report["suppressed"]),
            "deferred": len(report["deferred"]),
        }, sort_keys=True))
        return 0

    settings = get_settings()
    engine = create_engine(settings.database_url.get_secret_value())
    sessionmaker = create_sessionmaker(engine)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(*_):
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK: Ctrl+Break on Windows
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_stop)
    try:
        while True:
            if args.snapshot != "-":
                try:
                    snapshot = load_snapshot(args.snapshot)
                except ValueError as exc:
                    # A transient snapshot problem must not kill a
                    # looped scheduler; skip this pass and retry.
                    print(str(exc), file=sys.stderr)
                    snapshot = None
            if snapshot is not None:
                state = load_state(args.state_file)
                previous = None if args.force else state.get("fingerprint")
                try:
                    async with sessionmaker() as db:
                        summary = await run_forecast_scan(
                            db, tenant_id, snapshot,
                            critical_ids=critical_ids, horizons=horizons,
                            previous_fingerprint=previous,
                            sent_at=state.get("sent_at") or {},
                            created_by=created_by, prune=args.prune,
                        )
                except ValueError as exc:
                    print(str(exc), file=sys.stderr)
                    return 2
                if args.state_file:
                    save_state(args.state_file, {
                        "fingerprint": summary["fingerprint"],
                        "sent_at": summary["sent_at"],
                    })
                print(format_scan_summary(summary))
            if not args.loop or stop.is_set():
                return 0
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.interval_seconds)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                return 0
    finally:
        await engine.dispose()


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
    cycle = sub.add_parser("db-backup-cycle", help="Step AO: run one scheduled backup + restore drill and record the receipt")
    cycle.add_argument("--output-dir", type=Path, help="Directory for the backup file (default .backups)")
    cycle.add_argument("--drill-database", help="Throwaway drill database name (default ao_drill_<db>_<stamp>)")
    verify = sub.add_parser("db-verify-backup", help="Verify a PostgreSQL backup catalog")
    verify.add_argument("--path", type=Path, required=True)
    restore = sub.add_parser("db-restore-empty", help="Restore into the configured database only if it is empty")
    restore.add_argument("--path", type=Path, required=True)
    migrate = sub.add_parser("db-migrate-safe", help="Step AO: migrate only when a fresh drill-verified backup exists")
    migrate.add_argument("--check-only", action="store_true", help="Evaluate the gate without migrating")
    migrate.add_argument("--last-migration-at", help="ISO timestamp the last migration was applied (deploy record)")
    migrate.add_argument("--rpo-hours", type=float, default=24.0, help="Recovery-point objective in hours (default 24)")
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
    forecast = sub.add_parser(
        "forecast-scan",
        help="Standing forecast scan (Step L): scan a twin snapshot, record new forecasts, evaluate the watch",
    )
    forecast.add_argument("--tenant-id", required=True, help="Graph tenant UUID the forecasts belong to")
    forecast.add_argument("--snapshot", required=True, help="Twin snapshot JSON file ('-' reads stdin)")
    forecast.add_argument("--critical-ids", help="Comma-separated entity ids; default: critical set from the snapshot")
    forecast.add_argument("--horizons", help="Comma-separated day counts; default: 1,7,14,30")
    forecast.add_argument("--created-by", help="UUID of the user the scan is attributed to")
    forecast.add_argument("--state-file", help="JSON state file for fingerprint gating + alert cooldowns")
    forecast.add_argument("--dry-run", action="store_true", help="Scan and evaluate the watch without writing anything")
    forecast.add_argument("--prune", action="store_true", help="Prune superseded records past retention after recording")
    forecast.add_argument("--force", action="store_true", help="Ignore the stored fingerprint; always re-scan")
    forecast.add_argument("--loop", action="store_true", help="Repeat passes until stopped (SIGINT/SIGTERM)")
    forecast.add_argument("--interval-seconds", type=int, default=3600, help="Seconds between loop passes (min 60)")
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
    elif args.command == "db-backup-cycle":
        asyncio.run(db_backup_cycle(args.output_dir, args.drill_database))
    elif args.command == "db-verify-backup":
        db_verify_backup(args.path)
    elif args.command == "db-restore-empty":
        asyncio.run(db_restore_empty(args.path))
    elif args.command == "db-migrate-safe":
        last_migration = datetime.fromisoformat(args.last_migration_at) if args.last_migration_at else None
        asyncio.run(db_migrate_safe(args.check_only, last_migration, args.rpo_hours))
    elif args.command == "deployment-snapshot-create":
        deployment_snapshot_create(args.database_backup, args.output, args.application_data, args.qdrant_data)
    elif args.command == "deployment-snapshot-verify":
        deployment_snapshot_verify(args.path)
    elif args.command == "deployment-snapshot-restore-files":
        deployment_snapshot_restore_files(args.path, args.application_data_target, args.qdrant_data_target)
    elif args.command == "forecast-scan":
        sys.exit(asyncio.run(forecast_scan(args)))


if __name__ == "__main__":
    main()
