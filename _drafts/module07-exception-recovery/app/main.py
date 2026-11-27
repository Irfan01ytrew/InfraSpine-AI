"""Application factory and ASGI entry point (`uvicorn app.main:app`)."""

import asyncio
import contextlib
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import (
    request_forms,
    webhooks as webhook_routes,
    access_admin,
    agent_catalog,
    agent_actions,
    agents,
    automation,
    admin_embeddings,
    admin_media,
    admin_ocr,
    admin_parsers,
    admin_providers,
    admin_reports,
    admin_agent_runs,
    admin_traces,
    auth,
    chat,
    claims,
    conversations,
    departments,
    deployment as deployment_routes,
    expertise,
    exports,
    forecasts,
    folder_sync,
    cloud_connectors,
    sso,
    scim,
    collaboration,
    governance,
    handoffs,
    health,
    knowledge,
    knowledge_interviews,
    legacy_browser,
    memory,
    meetings,
    rag,
    admin_users,
    onboarding,
    org,
    personal_tasks,
    partner_admin,
    partner_portal,
    proposal_coverage,
    role_templates,
    service_requests,
)
from app.core.config import get_settings
from app.core.crypto import CredentialCipher
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.db.session import create_engine, create_sessionmaker
from app.rag import components as rag_components
from app.services.llm_runtime import LLMRuntime
from app.services.provider_admin import rebind_legacy_credentials
from app.services.agents import recover_abandoned_runs
from app.services.deployment import license_status

logger = logging.getLogger("app.request")

ADMIN_UI_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_UPLOAD_PATH = re.compile(r"^/api/rag/documents(/[^/]+/file)?/?$")
_PARTNER_UPLOAD_PATH = re.compile(r"^/api/admin/partner-workspaces/[^/]+/sources/upload/?$")
_MULTIPART_OVERHEAD = 64 * 1024
_ADMIN_UI_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def _reject_oversized_upload(request: Request, settings) -> Response | None:
    """FastAPI parses multipart bodies before authentication runs, so bound uploads up front:
    require Content-Length and reject bodies over the limit before any bytes are spooled."""
    if request.method not in ("POST", "PUT") or not (
        _UPLOAD_PATH.match(request.url.path) or _PARTNER_UPLOAD_PATH.match(request.url.path)
    ):
        return None
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        return JSONResponse(
            status_code=411, content={"error": {"code": "length_required", "message": "Content-Length is required."}}
        )
    if int(length) > settings.rag_max_upload_bytes + _MULTIPART_OVERHEAD:
        return JSONResponse(
            status_code=413,
            content={
                "error": {"code": "file_too_large", "message": f"The file exceeds the {settings.rag_max_upload_mb} MB limit."}
            },
        )
    return None


async def _warm_embedder(embedder) -> None:
    """Load the embedding model off the request path; a failure is logged and retried on first use."""
    started = time.perf_counter()
    try:
        await asyncio.to_thread(embedder.warmup)
        logger.info("Embedding model warmed up in %.1fs", time.perf_counter() - started)
    except Exception as exc:
        logger.warning("Embedding model warm-up failed: %s", type(exc).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        pool_recycle=settings.database_pool_recycle_seconds,
    )
    app.state.sessionmaker = create_sessionmaker(app.state.engine)
    try:
        recovered_agent_runs = await recover_abandoned_runs(app.state.sessionmaker)
        if recovered_agent_runs:
            logger.warning("Recovered %s stale agent runs after process startup", recovered_agent_runs)
    except Exception as exc:
        logger.error("Agent run startup recovery failed: %s", type(exc).__name__)
    app.state.cipher = CredentialCipher(settings.master_key_bytes, settings.master_encryption_key_id)
    from app.recovery.adapters import default_registry
    app.state.recovery_registry = default_registry(app.state.sessionmaker)
    try:  # one-time upgrade: bind custom-provider keys saved before origin binding (idempotent)
        await rebind_legacy_credentials(app.state.sessionmaker, app.state.cipher)
    except Exception as exc:
        logger.error("Binding stored provider credentials to their endpoints failed: %s", type(exc).__name__)
    # One runtime per process; it caches the client built from the active DB configuration.
    app.state.llm_runtime = LLMRuntime(settings, app.state.cipher)

    app.state.rag = rag_components.build_rag(settings, app.state.sessionmaker)
    # Background workers run in the index owner only; additional API processes (replicas) just serve requests.
    owner = app.state.rag.index_role == "owner"
    if not owner:
        logger.info("API process %s runs as a vector index replica; filling it from the database", os.getpid())
        await app.state.rag.indexer.sync_changes()  # serve no searches from an empty replica
    stop_worker = asyncio.Event()
    worker_tasks = []
    if settings.recovery_worker_enabled:
        from app.recovery.worker import run as run_recovery_worker
        worker_tasks.append(asyncio.create_task(run_recovery_worker(
            app.state.sessionmaker, app.state.recovery_registry, app.state.cipher,
            stop_worker, settings.recovery_poll_seconds), name="exception-recovery-worker"))
    if settings.rag_embedding_warmup:
        worker_tasks.append(asyncio.create_task(_warm_embedder(app.state.rag.embedder), name="embedding-warmup"))
    if settings.agent_queue_worker_enabled and owner:
        from app.services.agent_worker import run_agent_worker

        worker_tasks.append(asyncio.create_task(
            run_agent_worker(
                app.state.sessionmaker,
                settings,
                app.state.rag,
                app.state.cipher,
                app.state.llm_runtime,
                stop_worker,
                interval_seconds=settings.agent_queue_poll_seconds,
            ),
            name="agent-profile-run-worker",
        ))
    embedded = settings.rag_ingestion_mode == "embedded"
    if settings.rag_worker_enabled and owner:
        if embedded:  # single-process installations: document processing runs inside the API process
            worker_tasks.append(asyncio.create_task(
                app.state.rag.worker.run(stop_worker, concurrency=settings.rag_worker_concurrency, mode="embedded"),
                name="rag-ingestion-worker",
            ))
            worker_tasks.append(asyncio.create_task(app.state.rag.partner_worker.run(stop_worker), name="partner-ingestion-worker"))
            from app.services.training_reminders import run_training_reminders
            from app.services.personal_task_reminders import run_personal_task_reminders

            worker_tasks.append(asyncio.create_task(
                run_training_reminders(app.state.sessionmaker, stop_worker),
                name="training-reminder-worker",
            ))
            worker_tasks.append(asyncio.create_task(
                run_personal_task_reminders(app.state.sessionmaker, stop_worker),
                name="personal-task-reminder-worker",
            ))
            from app.services.automation_worker import run_automation_worker

            worker_tasks.append(asyncio.create_task(
                run_automation_worker(app.state.sessionmaker, stop_worker),
                name="automation-workflow-worker",
            ))
        worker_tasks.append(asyncio.create_task(app.state.rag.outbox.run(stop_worker), name="knowledge-outbox-worker"))
    # Apply chunks written by worker processes (and, for a replica, by any other API process) to this index.
    sync_seconds = settings.rag_index_sync_seconds or (0 if owner else 2.0)
    if sync_seconds > 0:
        worker_tasks.append(asyncio.create_task(
            app.state.rag.indexer.run_sync(stop_worker, sync_seconds), name="rag-index-sync"
        ))
    elif not embedded:
        logger.warning("RAG_INDEX_SYNC_SECONDS=0 with RAG_INGESTION_MODE=worker: new documents won't become searchable")
    try:
        yield
    finally:
        stop_worker.set()
        for task in worker_tasks:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=settings.rag_worker_shutdown_grace_seconds + 15)
        app.state.rag.close()
        await app.state.llm_runtime.close()
        await app.state.engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        # Interactive docs are handy in development but not exposed in production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next) -> Response:
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if _REQUEST_ID_PATTERN.match(incoming) else uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        rejection = _reject_oversized_upload(request, settings)
        if rejection is not None:
            response = rejection
        elif settings.license_enforcement_enabled and path_is_licensed(request.url.path):
            license = license_status(settings)
            if license.status != "valid":
                response = JSONResponse(
                    status_code=402,
                    content={
                        "error": {
                            "code": "license_required",
                            "message": "A valid deployment license is required. An administrator can install one in Company setup.",
                        }
                    },
                )
            elif (feature := required_license_feature(request.url.path)) and feature not in license.features:
                response = JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "license_feature_not_included",
                            "message": f"This deployment license does not include the {feature} feature.",
                        }
                    },
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        path = request.url.path
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith("/admin"):
            response.headers["Content-Security-Policy"] = _ADMIN_UI_CSP

        logger.info(
            "%s %s status=%s duration_ms=%.0f request_id=%s",
            request.method,
            path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            request_id,
        )
        return response

    register_exception_handlers(app)
    app.include_router(health.router)
    from app.recovery.routes import router as recovery_router
    app.include_router(recovery_router)
    app.include_router(auth.router)
    app.include_router(agent_catalog.router)
    app.include_router(agents.router)
    app.include_router(agent_actions.router)
    app.include_router(automation.router)
    app.include_router(personal_tasks.router)
    app.include_router(partner_admin.router)
    app.include_router(partner_portal.router)
    app.include_router(proposal_coverage.router)
    app.include_router(chat.router)
    app.include_router(admin_embeddings.router)
    app.include_router(admin_media.router)
    app.include_router(admin_ocr.router)
    app.include_router(admin_parsers.router)
    app.include_router(admin_providers.router)
    app.include_router(admin_traces.router)
    app.include_router(admin_reports.router)
    app.include_router(admin_agent_runs.router)
    app.include_router(admin_users.router)
    app.include_router(deployment_routes.public_router)
    app.include_router(deployment_routes.router)
    app.include_router(onboarding.router)
    app.include_router(access_admin.router)
    app.include_router(role_templates.router)
    app.include_router(org.router)
    app.include_router(org.chart_router)
    app.include_router(departments.router)
    app.include_router(governance.router)
    app.include_router(rag.router)
    app.include_router(conversations.router)
    app.include_router(memory.router)
    app.include_router(meetings.router)
    app.include_router(claims.router)
    app.include_router(knowledge.router)
    app.include_router(knowledge_interviews.router)
    app.include_router(legacy_browser.router)
    app.include_router(handoffs.router)
    app.include_router(service_requests.router)
    app.include_router(request_forms.router)
    app.include_router(webhook_routes.router)
    app.include_router(expertise.router)
    app.include_router(forecasts.router)
    app.include_router(exports.router)
    app.include_router(exports.image_router)
    app.include_router(folder_sync.router)
    app.include_router(cloud_connectors.router)
    app.include_router(sso.router)
    app.include_router(scim.router)
    app.include_router(collaboration.router)

    @app.get("/", include_in_schema=False)
    async def root() -> Response:
        if ADMIN_UI_DIR.is_dir():
            return RedirectResponse("/admin/")
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "admin_ui_not_built", "message": "Run 'npm run build' in frontend/."}},
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    # Serve the built admin UI from the same origin, so session cookies stay SameSite=Strict.
    if ADMIN_UI_DIR.is_dir():
        app.mount("/admin", StaticFiles(directory=ADMIN_UI_DIR, html=True), name="admin-ui")
    return app


def path_is_licensed(path: str) -> bool:
    """Whether an API request requires a valid license when enforcement is enabled.

    Sign-in, sign-out, health and the administrator deployment page remain reachable so an
    operator can diagnose and renew a license without disabling the enforcement setting.
    """
    if not path.startswith("/api/"):
        return False
    if path in {
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
        "/api/admin/deployment",
        "/api/deployment/profile",
    }:
        return False
    if path.startswith("/api/admin/deployment/"):
        return False
    return True


def required_license_feature(path: str) -> str | None:
    """Optional entitlements with a stable route-level product mapping."""
    if path == "/api/rag/research" or path == "/api/proposal-coverage" or path.startswith("/api/proposal-coverage/"):
        return "research"
    if path == "/api/rag/training" or path.startswith("/api/rag/training/"):
        return "training"
    if path == "/api/rag/market-research":
        return "market_research"
    if path.startswith("/api/admin/reports/"):
        return "reporting"
    return None


app = create_app()
