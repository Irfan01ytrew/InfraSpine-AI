"""Document worker process: parses, chunks and embeds uploaded documents outside the API process.

    python -m app.worker                 # run until stopped (Ctrl+C / SIGTERM); finishes or requeues jobs
    python -m app.worker --concurrency 4 # process up to 4 documents in parallel (default RAG_WORKER_CONCURRENCY)
    python -m app.worker --once          # process everything that is due, then exit (scheduled tasks, scripts)
    python -m app.worker --check         # validate configuration and the database, then exit

It reads the same `.env` as the API (database, data directory, embedding model and RAG_* limits) and must
see the same RAG_DATA_DIR (the uploaded files). It writes results to PostgreSQL only; the API process
applies them to its vector index. Run as many workers as the hardware allows, on one or more machines
that share the database and data directory: each job is claimed by exactly one worker.
"""

# Keep module-level imports minimal: every parser child process re-imports this module (spawn start method).
import argparse
import sys


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.worker", description="InfraSpine AI document worker")
    parser.add_argument("--concurrency", type=int, help="documents processed in parallel (default: RAG_WORKER_CONCURRENCY)")
    parser.add_argument("--once", action="store_true", help="process the documents that are due, then exit")
    parser.add_argument("--check", action="store_true", help="validate configuration and the database, then exit")
    return parser.parse_args(argv)


async def _schema_ready(sessionmaker) -> str | None:
    """None when the database is reachable and migrated far enough for workers; otherwise the problem."""
    from sqlalchemy import text

    try:
        async with sessionmaker() as db:
            await db.execute(text("SELECT 1 FROM ingestion_workers LIMIT 1"))
            await db.execute(text("SELECT file_format FROM documents LIMIT 1"))
            await db.execute(text("SELECT reminder_type FROM training_reminders LIMIT 1"))
            await db.execute(text("SELECT reminder_type FROM personal_task_reminders LIMIT 1"))
            await db.execute(text("SELECT status FROM automation_runs LIMIT 1"))
            await db.execute(text("SELECT response_due_at FROM service_requests LIMIT 1"))
            await db.execute(text("SELECT notification_type FROM service_request_notifications LIMIT 1"))
            await db.execute(text("SELECT enabled FROM folder_sync_sources LIMIT 1"))
            await db.execute(text("SELECT index_status FROM partner_knowledge_sources LIMIT 1"))
    except Exception as exc:
        if "does not exist" in str(exc):
            return "The database schema is out of date. Run: alembic upgrade head"
        cause = getattr(exc, "orig", None)
        diagnostic = type(exc).__name__
        if cause is not None:
            diagnostic += f"/{type(cause).__name__}"
            sqlstate = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
            if isinstance(sqlstate, str) and len(sqlstate) == 5 and sqlstate.isalnum():
                diagnostic += f"/sqlstate:{sqlstate}"
        return f"The database is not reachable ({diagnostic}). Check DATABASE_URL."
    return None


async def _run(args: argparse.Namespace) -> int:
    import asyncio
    import logging
    import os
    import signal

    from app.core.config import get_settings
    from app.db.session import create_engine, create_sessionmaker

    settings = get_settings()
    log = logging.getLogger("app.worker")
    concurrency = args.concurrency or settings.rag_worker_concurrency
    if not 1 <= concurrency <= 32:
        log.error("--concurrency must be between 1 and 32")
        return 2
    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_max_overflow,
        pool_timeout=getattr(settings, "database_pool_timeout_seconds", 30),
        pool_recycle=getattr(settings, "database_pool_recycle_seconds", 1800),
    )
    try:
        sessionmaker = create_sessionmaker(engine)
        problem = await _schema_ready(sessionmaker)
        if problem:
            log.error(problem)
            return 1
        if args.check:
            data_dir = settings.rag_data_dir.resolve()
            if not data_dir.is_dir() or not os.access(data_dir, os.R_OK | os.W_OK):
                log.error("Worker data directory is unavailable or not readable and writable")
                return 1
            from pathlib import Path

            from app.rag.embeddings import SentenceTransformerEmbedder

            models_dir = Path(settings.rag_models_dir).resolve() if settings.rag_models_dir else data_dir / "models"
            embedder = SentenceTransformerEmbedder(
                settings.rag_embedding_model,
                settings.rag_embedding_dimension,
                models_dir,
                allow_download=settings.rag_embedding_allow_download,
            )
            if not embedder.available_locally and not settings.rag_embedding_allow_download:
                log.error(
                    "Embedding model files are not available locally; pre-stage the configured model under %s "
                    "or explicitly enable RAG_EMBEDDING_ALLOW_DOWNLOAD in a connected, trusted deployment.",
                    models_dir,
                )
                return 1
            embedding_status = (
                "staged locally" if embedder.available_locally else "download permitted (connectivity not checked)"
            )
            print(
                # getattr: lightweight/fake settings objects (worker healthcheck
                # tests) may not carry the property; real Settings always does.
                f"Worker configuration OK: workflow engine {'on' if getattr(settings, 'workflow_engine_on', False) else 'off'}, "
                f"data dir {data_dir}, concurrency {concurrency}, "
                f"isolation {'on' if settings.rag_parse_isolation else 'off'}, timeout "
                f"{settings.rag_processing_timeout_seconds}s, parser memory {settings.rag_parser_memory_mb} MB, "
                f"API ingestion mode '{settings.rag_ingestion_mode}', embedding model "
                f"{embedding_status}."
            )
            return 0
        from app.rag import components

        if settings.rag_ingestion_mode == "embedded" and settings.rag_worker_enabled:
            log.warning("RAG_INGESTION_MODE=embedded: the API also processes documents; this worker shares the queue")

        worker = components.build_worker(settings, sessionmaker)
        from app.services.partner_ingestion import PartnerIngestionWorker

        partner_worker = PartnerIngestionWorker(sessionmaker, settings, worker._embedder)
        if args.once:
            from app.services.training_reminders import process_training_reminders
            from app.services.personal_task_reminders import process_personal_task_reminders
            from app.services.automation_worker import process_workflows_once
            from app.services.service_request_worker import process_service_request_slas
            from app.services.approval_reminders import process_approval_reminders
            from app.services.request_attachments import cleanup_orphans
            from app.services.webhooks import deliver_due
            from app.services.folder_sync import process_due_folder_syncs

            await worker.startup()
            processed = 0
            while await worker.process_next():
                processed += 1
            partner_processed = 0
            while await partner_worker.process_next():
                partner_processed += 1
            reminders = await process_training_reminders(sessionmaker)
            personal_task_reminders = await process_personal_task_reminders(sessionmaker, settings=settings)
            workflows = await process_workflows_once(sessionmaker)
            sla_escalations = 0
            while completed_batch := await process_service_request_slas(sessionmaker):
                sla_escalations += completed_batch
            approval_reminders = await process_approval_reminders(sessionmaker)
            await cleanup_orphans(sessionmaker, settings=settings)
            await deliver_due(sessionmaker, settings=settings)
            folder_sync_runs = await process_due_folder_syncs(sessionmaker, settings, batch_size=10)
            if getattr(settings, "workflow_engine_on", False):
                from app.core.crypto import CredentialCipher
                from app.finance.approval_reminders import process_finance_approval_reminders
                from app.services.finance_autopilot import process_finance_autopilot
                from app.workflows.engine import tick

                cipher = CredentialCipher(settings.master_key_bytes, settings.master_encryption_key_id)
                ticks = 0
                while ticks < 1000 and await tick(sessionmaker, cipher):
                    ticks += 1
                log.info("Workflow engine: %s ticks", ticks)
                autopilot_decisions = await process_finance_autopilot(sessionmaker, settings=settings, cipher=cipher)
                log.info("Finance autopilot: %s sweep decisions", autopilot_decisions)
                finance_reminders = await process_finance_approval_reminders(sessionmaker)
                log.info("Finance approval reminders: %s approvals nudged", finance_reminders)
            log.info(
                "Processed %s documents, %s training reminders, %s personal task reminders, %s workflow events, %s service request SLA escalations and %s folder sync runs",
                processed,
                reminders,
                personal_task_reminders,
                workflows,
                sla_escalations,
                folder_sync_runs,
            )
            log.info("Processed %s partner sources and %s approval step reminders", partner_processed, approval_reminders)
            return 0

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def request_stop(*_):
            loop.call_soon_threadsafe(stop.set)

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK: Ctrl+Break / console close on Windows
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), request_stop)
        from app.services.training_reminders import run_training_reminders
        from app.services.personal_task_reminders import run_personal_task_reminders
        from app.services.automation_worker import run_automation_worker
        from app.services.service_request_worker import run_service_request_sla_worker
        from app.services.approval_reminders import run_approval_reminders
        from app.finance.approval_reminders import run_finance_approval_reminders
        from app.services.finance_autopilot import run_finance_autopilot
        from app.services.request_attachments import run_attachment_cleanup
        from app.services.webhooks import run_webhook_worker
        from app.services.folder_sync import run_folder_sync_worker

        background = []
        if getattr(settings, "workflow_engine_on", False):
            # Unified workflow runtime (step E): automation rules, the invoice flow and governance actions.
            # On by default; WORKFLOW_ENGINE_WORKER_ENABLED=false is the kill switch. Leased claims make it safe to
            # run here and in the API at the same time. Agent Studio steps wait for the API process (LLM runtime).
            from app.core.crypto import CredentialCipher
            from app.workflows.engine import run_worker as run_workflow_engine

            cipher = CredentialCipher(settings.master_key_bytes, settings.master_encryption_key_id)
            background.append(run_workflow_engine(sessionmaker, cipher, stop))
            # Finance autopilot (step R slice 2): same kill switch; the org-settings policy gates the sweep itself.
            background.append(run_finance_autopilot(sessionmaker, stop, settings=settings, cipher=cipher))
            # Finance approval reminders (step R slice 3): same kill switch; nudges only, never decides.
            background.append(run_finance_approval_reminders(sessionmaker, stop))
            log.info("Workflow engine worker started (WORKFLOW_ENGINE_WORKER_ENABLED=false turns it off)")
        else:
            log.warning("Workflow engine worker is OFF (WORKFLOW_ENGINE_WORKER_ENABLED=false)")

        await asyncio.gather(
            *background,
            worker.run(stop, concurrency=concurrency, mode="worker"),
            partner_worker.run(stop),
            run_training_reminders(sessionmaker, stop),
            run_personal_task_reminders(sessionmaker, stop, settings=settings),
            run_automation_worker(sessionmaker, stop),
            run_service_request_sla_worker(sessionmaker, stop),
            run_approval_reminders(sessionmaker, stop),
            run_attachment_cleanup(sessionmaker, stop),
            run_webhook_worker(sessionmaker, stop),
            run_folder_sync_worker(sessionmaker, settings, stop),
        )
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    import asyncio

    from app.core.config import get_settings
    from app.core.logging import configure_logging

    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir, "worker.log")  # its own file: one writer per file
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
