"""Independent AI Agent Studio: custom agents, encrypted threads, approvals and run history."""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Path as ApiPath, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.agent_studio.service import (
    AgentStudioService,
    _encode_state,
    create_agent,
    create_thread,
    decode_run_state,
    decode_thread_history,
    delete_agent,
    get_run,
    list_agents,
    list_runs,
    list_threads,
    public_run,
    set_agent_enabled,
    set_agent_paused,
    update_agent,
)
from app.agent_studio.analytics import agent_studio_analytics
from app.agent_studio.tools import TOOL_DEFINITIONS
from app.api.deps import AppSettings, CurrentUser, DbSession, audit_context, get_current_user
from app.core.errors import ConflictError, NotFoundError
from app.db.agent_studio_models import (
    AgentStudioAgent,
    AgentStudioApproval,
    AgentStudioMemory,
    AgentStudioRun,
    AgentStudioThread,
)
from app.schemas.agent_studio import (
    AgentStudioAgentList,
    AgentStudioAgentOut,
    AgentStudioDecisionRequest,
    AgentStudioMemoryList,
    AgentStudioMemoryOut,
    AgentStudioRunList,
    AgentStudioRunOut,
    AgentStudioRunRequest,
    AgentStudioThreadCreate,
    AgentStudioThreadList,
    AgentStudioThreadOut,
    AgentStudioTools,
    AgentStudioToolOut,
    AgentStudioUpdate,
    AgentStudioCreate,
)
from app.schemas.agent_studio_whatsapp import (
    WhatsAppConnectionOut,
    WhatsAppConnectionResponse,
    WhatsAppConnectionSave,
    WhatsAppPathResponse,
    WhatsAppPathSave,
    WhatsAppVerifyResponse,
)
from app.integrations.whatsapp_failover import (
    get_path_setting,
    live_baileys_session,
    resolve_primary,
    set_path_setting,
)
from app.services import audit
from app.services.chat_exports import MEDIA_TYPES, resolve_chat_export
from app.services.chat_images import resolve_chat_image
from app.services.local_image_generation import local_image_model_ready
from app.services.agent_studio_whatsapp import (
    delete_connection as delete_whatsapp_connection,
    get_connection as get_whatsapp_connection,
    public_connection as public_whatsapp_connection,
    save_connection as save_whatsapp_connection,
    verify_and_subscribe as verify_whatsapp_and_subscribe,
)
from app.services.llm_runtime import LLMRuntime

router = APIRouter(prefix="/api/agent-studio", tags=["agent-studio"], dependencies=[Depends(get_current_user)])


@router.get("/whatsapp", response_model=WhatsAppConnectionResponse)
async def agent_studio_whatsapp_connection(
    request: Request, db: DbSession, user: CurrentUser, settings: AppSettings
) -> WhatsAppConnectionResponse:
    connection = await get_whatsapp_connection(db, user)
    agent = await db.get(AgentStudioAgent, connection.agent_id) if connection else None
    result = public_whatsapp_connection(
        connection,
        agent.name if agent and agent.owner_id == user.id else None,
        str(request.base_url),
        settings.whatsapp_webhook_base_url,
    )
    # Step F slice 3: the runtime path setting + which transport a reply
    # would actually use right now.
    path_setting = await get_path_setting(db, user.id)
    cloud_available = connection is not None and connection.enabled
    sessions = getattr(request.app.state, "baileys_sessions", None) or {}
    baileys_session = await live_baileys_session(db, sessions, user.id)
    active_path = resolve_primary(
        path_setting, own_channel="cloud",
        cloud_available=cloud_available, baileys_available=baileys_session is not None,
    )
    return WhatsAppConnectionResponse(
        connection=WhatsAppConnectionOut.model_validate(result) if result else None,
        path_setting=path_setting, active_path=active_path,
        cloud_available=cloud_available, baileys_available=baileys_session is not None,
    )


@router.put("/whatsapp/path", response_model=WhatsAppPathResponse)
async def set_agent_studio_whatsapp_path(
    payload: WhatsAppPathSave, request: Request, db: DbSession, user: CurrentUser
) -> WhatsAppPathResponse:
    """Step F slice 3: store the owner's WhatsApp transport path setting.

    ``cloud`` / ``baileys`` pick the primary transport; ``auto`` prefers the
    channel a message arrived on. The setting only picks the primary — a
    failed primary is still retried once on the other path.
    """
    path_setting = await set_path_setting(db, user.id, payload.path)
    await db.commit()
    connection = await get_whatsapp_connection(db, user)
    cloud_available = connection is not None and connection.enabled
    sessions = getattr(request.app.state, "baileys_sessions", None) or {}
    baileys_session = await live_baileys_session(db, sessions, user.id)
    active_path = resolve_primary(
        path_setting, own_channel="cloud",
        cloud_available=cloud_available, baileys_available=baileys_session is not None,
    )
    return WhatsAppPathResponse(
        path_setting=path_setting, active_path=active_path,
        cloud_available=cloud_available, baileys_available=baileys_session is not None,
    )


@router.put("/whatsapp", response_model=WhatsAppConnectionResponse)
async def save_agent_studio_whatsapp_connection(
    payload: WhatsAppConnectionSave,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> WhatsAppConnectionResponse:
    data = payload.model_dump()
    for name in ("access_token", "app_secret", "verify_token"):
        value = data.get(name)
        data[name] = value.get_secret_value() if value is not None else None
    row = await save_whatsapp_connection(db, user, request.app.state.cipher, data)
    agent = await db.get(AgentStudioAgent, row.agent_id)
    audit.record(
        db,
        audit_context(request, user),
        "agent_studio.whatsapp_connected",
        target=str(row.id),
        details={"agent_id": str(row.agent_id), "phone_number_id": row.phone_number_id},
    )
    await db.commit()
    result = public_whatsapp_connection(row, agent.name if agent else None, str(request.base_url), settings.whatsapp_webhook_base_url)
    return WhatsAppConnectionResponse(connection=WhatsAppConnectionOut.model_validate(result))


@router.post("/whatsapp/verify", response_model=WhatsAppVerifyResponse)
async def verify_agent_studio_whatsapp_connection(
    request: Request, db: DbSession, user: CurrentUser
) -> WhatsAppVerifyResponse:
    row = await get_whatsapp_connection(db, user)
    if row is None:
        raise NotFoundError("WhatsApp integration not found.")
    details = await verify_whatsapp_and_subscribe(db, row, request.app.state.cipher)
    audit.record(db, audit_context(request, user), "agent_studio.whatsapp_verified", target=str(row.id))
    await db.commit()
    return WhatsAppVerifyResponse(**details, subscribed=True)


@router.delete("/whatsapp", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_agent_studio_whatsapp(
    request: Request, db: DbSession, user: CurrentUser
) -> None:
    row = await get_whatsapp_connection(db, user)
    if row is not None:
        await delete_whatsapp_connection(db, user)
        audit.record(db, audit_context(request, user), "agent_studio.whatsapp_disconnected", target=str(row.id))
        await db.commit()


def _service(request: Request, db: DbSession, user: CurrentUser, settings: AppSettings) -> AgentStudioService:
    rag = getattr(request.app.state, "rag", None)
    return AgentStudioService(
        db,
        user,
        settings,
        request.app.state.cipher,
        request.app.state.llm_runtime,
        rag,
    )


async def _run_out(db: DbSession, request: Request, user: CurrentUser, run: AgentStudioRun) -> AgentStudioRunOut:
    state = decode_run_state(request.app.state.cipher, run)
    approval = None
    if run.status == "awaiting_approval":
        approval = await db.scalar(select(AgentStudioApproval).where(
            AgentStudioApproval.run_id == run.id,
            AgentStudioApproval.owner_id == user.id,
            AgentStudioApproval.status == "pending",
        ))
    return AgentStudioRunOut.model_validate(
        public_run(run, state, approval, request.app.state.cipher)
    )


def _record(request: Request, user: CurrentUser, event: str, target: str, details: dict | None = None) -> None:
    audit.record(
        request.state.db,
        audit_context(request, user),
        event,
        target=target,
        details=details or {},
    )


@router.get("/tools", response_model=AgentStudioTools)
async def list_agent_studio_tools(request: Request, db: DbSession, settings: AppSettings) -> AgentStudioTools:
    browser_executable = settings.rag_legacy_browser_executable_path
    browser_ready = bool(
        settings.rag_legacy_browser_enabled
        and browser_executable
        and browser_executable.is_absolute()
        and browser_executable.is_file()
        and settings.rag_legacy_browser_allowed_origins.strip()
    )
    provider_ready = False
    image_ready = False
    try:
        client = await request.app.state.llm_runtime.get_client(db)
        provider_ready = True
        image_ready = bool(client.supports_image_generation) or local_image_model_ready(settings)
    except Exception:
        # Keep the tools dashboard usable even before an administrator configures a provider.
        pass
    rag_ready = getattr(request.app.state, "rag", None) is not None
    return AgentStudioTools(tools=[
        AgentStudioToolOut(
            name=name,
            description=description,
            requires_approval_by_default=requires_approval,
            available=(
                browser_ready if name == "browser_capture" else
                image_ready if name == "generate_image" else
                rag_ready if name == "rag_search" else
                provider_ready if name in {"web_search", "browse_url"} else True
            ),
            availability_note=(
                "Enable isolated browser capture and configure allowed public origins in settings."
                if name == "browser_capture" and not browser_ready else
                "Configure an image-capable AI provider before enabling image generation."
                if name == "generate_image" and not image_ready else
                "Configure an active AI provider before enabling public web research."
                if name in {"web_search", "browse_url"} and not provider_ready else
                "The Knowledge service is unavailable in this process."
                if name == "rag_search" and not rag_ready else None
            ),
        )
        for name, description, requires_approval in TOOL_DEFINITIONS
    ])


@router.get("/agents", response_model=AgentStudioAgentList)
async def agent_studio_agents(db: DbSession, user: CurrentUser) -> AgentStudioAgentList:
    return AgentStudioAgentList(agents=[AgentStudioAgentOut.model_validate(row) for row in await list_agents(db, user)])


@router.get("/analytics")
async def agent_studio_analytics_report(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    days: int = Query(default=30, ge=7, le=90),
) -> dict:
    worker = getattr(request.app.state, "agent_studio_worker_task", None)
    return await agent_studio_analytics(
        db,
        user,
        days=days,
        worker_enabled=settings.agent_studio_worker_enabled,
        worker_running=bool(worker is not None and not worker.done()),
    )


@router.post("/agents", response_model=AgentStudioAgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent_studio_agent(
    payload: AgentStudioCreate,
    db: DbSession,
    user: CurrentUser,
    request: Request,
) -> AgentStudioAgentOut:
    row = await create_agent(db, user, payload.model_dump())
    audit.record(db, audit_context(request, user), "agent_studio.agent_created", target=str(row.id), details={"tools": row.tool_permissions})
    await db.commit()
    return AgentStudioAgentOut.model_validate(row)


@router.put("/agents/{agent_id}", response_model=AgentStudioAgentOut)
async def update_agent_studio_agent(
    agent_id: uuid.UUID,
    payload: AgentStudioUpdate,
    db: DbSession,
    user: CurrentUser,
    request: Request,
) -> AgentStudioAgentOut:
    data = payload.model_dump(exclude={"expected_version"})
    row = await update_agent(db, user, agent_id, payload.expected_version, data)
    audit.record(db, audit_context(request, user), "agent_studio.agent_updated", target=str(row.id), details={"version": row.version})
    await db.commit()
    return AgentStudioAgentOut.model_validate(row)


@router.post("/agents/{agent_id}/disable", response_model=AgentStudioAgentOut)
async def disable_agent_studio_agent(
    agent_id: uuid.UUID, db: DbSession, user: CurrentUser, request: Request
) -> AgentStudioAgentOut:
    row = await set_agent_enabled(db, user, agent_id, False)
    audit.record(db, audit_context(request, user), "agent_studio.agent_disabled", target=str(row.id))
    await db.commit()
    return AgentStudioAgentOut.model_validate(row)


@router.post("/agents/{agent_id}/enable", response_model=AgentStudioAgentOut)
async def enable_agent_studio_agent(
    agent_id: uuid.UUID, db: DbSession, user: CurrentUser, request: Request
) -> AgentStudioAgentOut:
    row = await set_agent_enabled(db, user, agent_id, True)
    audit.record(db, audit_context(request, user), "agent_studio.agent_enabled", target=str(row.id))
    await db.commit()
    return AgentStudioAgentOut.model_validate(row)


@router.post("/agents/{agent_id}/pause", response_model=AgentStudioAgentOut)
async def pause_agent_studio_agent(
    agent_id: uuid.UUID, db: DbSession, user: CurrentUser, request: Request
) -> AgentStudioAgentOut:
    row = await set_agent_paused(db, user, agent_id, True)
    audit.record(db, audit_context(request, user), "agent_studio.agent_paused", target=str(row.id))
    await db.commit()
    return AgentStudioAgentOut.model_validate(row)


@router.post("/agents/{agent_id}/resume", response_model=AgentStudioAgentOut)
async def resume_agent_studio_agent(
    agent_id: uuid.UUID, db: DbSession, user: CurrentUser, request: Request
) -> AgentStudioAgentOut:
    row = await set_agent_paused(db, user, agent_id, False)
    audit.record(db, audit_context(request, user), "agent_studio.agent_resumed", target=str(row.id))
    await db.commit()
    return AgentStudioAgentOut.model_validate(row)


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_studio_agent(
    agent_id: uuid.UUID, db: DbSession, user: CurrentUser, request: Request
) -> None:
    await delete_agent(db, user, agent_id)
    audit.record(db, audit_context(request, user), "agent_studio.agent_deleted", target=str(agent_id))
    await db.commit()


@router.post("/agents/{agent_id}/threads", response_model=AgentStudioThreadOut, status_code=status.HTTP_201_CREATED)
async def create_agent_studio_thread(
    agent_id: uuid.UUID,
    payload: AgentStudioThreadCreate,
    db: DbSession,
    user: CurrentUser,
    request: Request,
) -> AgentStudioThreadOut:
    row = await create_thread(db, user, request.app.state.cipher, agent_id, payload.title)
    return AgentStudioThreadOut(id=row.id, agent_id=row.agent_id, title=row.title, created_at=row.created_at, updated_at=row.updated_at)


@router.get("/agents/{agent_id}/threads", response_model=AgentStudioThreadList)
async def agent_studio_threads(
    agent_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> AgentStudioThreadList:
    rows = await list_threads(db, user, agent_id)
    return AgentStudioThreadList(threads=[
        AgentStudioThreadOut(
            id=thread.id,
            agent_id=thread.agent_id,
            title=thread.title,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
            last_run_status=last_status,
            last_run_at=last_run_at,
        )
        for thread, last_status, last_run_at in rows
    ])


@router.get("/agents/{agent_id}/runs", response_model=AgentStudioRunList)
async def agent_studio_runs(
    agent_id: uuid.UUID,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AgentStudioRunList:
    rows = await list_runs(db, user, agent_id, limit)
    return AgentStudioRunList(runs=[await _run_out(db, request, user, row) for row in rows])


@router.get("/threads/{thread_id}/runs", response_model=AgentStudioRunList)
async def agent_studio_thread_runs(
    thread_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser
) -> AgentStudioRunList:
    thread = await db.scalar(select(AgentStudioThread).where(
        AgentStudioThread.id == thread_id, AgentStudioThread.owner_id == user.id
    ))
    if thread is None:
        raise NotFoundError("Agent Studio resource not found.")
    rows = list((await db.execute(
        select(AgentStudioRun).where(
            AgentStudioRun.thread_id == thread.id, AgentStudioRun.owner_id == user.id
        ).order_by(AgentStudioRun.created_at.asc()).limit(100)
    )).scalars())
    return AgentStudioRunList(runs=[await _run_out(db, request, user, row) for row in rows])


@router.post(
    "/agents/{agent_id}/threads/{thread_id}/runs",
    response_model=AgentStudioRunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_agent_studio_agent(
    agent_id: uuid.UUID,
    thread_id: uuid.UUID,
    payload: AgentStudioRunRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    service: Annotated[AgentStudioService, Depends(_service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=256)] = None,
) -> AgentStudioRunOut:
    run, state, approval = await service.start_run(
        agent_id, thread_id, payload.prompt, idempotency_key=idempotency_key, scheduled_at=payload.scheduled_at
    )
    audit.record(
        db,
        audit_context(request, user),
        "agent_studio.run_queued" if run.status == "queued" else f"agent_studio.run_{run.status}",
        target=str(run.id),
        details={"agent_id": str(agent_id), "step_count": len(state.get("steps") or [])},
    )
    await db.commit()
    return AgentStudioRunOut.model_validate(public_run(run, state, approval, request.app.state.cipher))


@router.get("/runs/{run_id}", response_model=AgentStudioRunOut)
async def get_agent_studio_run(
    run_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser
) -> AgentStudioRunOut:
    run = await get_run(db, user, run_id)
    return await _run_out(db, request, user, run)


@router.post("/runs/{run_id}/cancel", response_model=AgentStudioRunOut)
async def cancel_agent_studio_run(
    run_id: uuid.UUID,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> AgentStudioRunOut:
    run = await db.scalar(select(AgentStudioRun).where(
        AgentStudioRun.id == run_id,
        AgentStudioRun.owner_id == user.id,
    ).with_for_update())
    if run is None:
        raise NotFoundError("Agent Studio resource not found.")
    if run.status == "cancelled":
        return await _run_out(db, request, user, run)
    if run.status != "queued":
        raise ConflictError("Only queued tasks can be cancelled. Running actions must finish or be reviewed.")
    run.status = "cancelled"
    run.finished_at = datetime.now(UTC)
    run.claimed_at = None
    state = decode_run_state(request.app.state.cipher, run)
    state["answer"] = "This queued task was cancelled before execution."
    sealed = _encode_state(request.app.state.cipher, run.id, state)
    run.state_ciphertext, run.state_nonce, run.state_key_id = sealed.ciphertext, sealed.nonce, sealed.key_id
    audit.record(db, audit_context(request, user), "agent_studio.run_cancelled", target=str(run.id))
    await db.commit()
    return AgentStudioRunOut.model_validate(public_run(run, state, None, request.app.state.cipher))


@router.post("/runs/{run_id}/decision", response_model=AgentStudioRunOut)
async def decide_agent_studio_run(
    run_id: uuid.UUID,
    payload: AgentStudioDecisionRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    service: Annotated[AgentStudioService, Depends(_service)],
) -> AgentStudioRunOut:
    run, state, approval = await service.decide(run_id, payload.decision, payload.note, user)
    audit.record(
        db,
        audit_context(request, user),
        f"agent_studio.action_{payload.decision}d",
        target=str(run.id),
        details={"status": run.status},
    )
    await db.commit()
    return AgentStudioRunOut.model_validate(public_run(run, state, approval, request.app.state.cipher))


@router.get("/agents/{agent_id}/memories", response_model=AgentStudioMemoryList)
async def agent_studio_memories(
    agent_id: uuid.UUID, db: DbSession, user: CurrentUser, request: Request
) -> AgentStudioMemoryList:
    row = await db.scalar(select(AgentStudioAgent).where(
        AgentStudioAgent.id == agent_id, AgentStudioAgent.owner_id == user.id
    ))
    if row is None:
        raise NotFoundError("Agent Studio resource not found.")
    memories = (await db.execute(
        select(AgentStudioMemory).where(
            AgentStudioMemory.agent_id == agent_id, AgentStudioMemory.owner_id == user.id
        ).order_by(AgentStudioMemory.updated_at.desc()).limit(100)
    )).scalars()
    from app.agent_studio.tools import _decrypt

    return AgentStudioMemoryList(memories=[
        AgentStudioMemoryOut(id=item.id, memory_key=item.memory_key, value=_decrypt(request.app.state.cipher, item), updated_at=item.updated_at)
        for item in memories
    ])


@router.delete("/agents/{agent_id}/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_studio_memory(
    agent_id: uuid.UUID, memory_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> None:
    row = await db.scalar(select(AgentStudioMemory).where(
        AgentStudioMemory.id == memory_id,
        AgentStudioMemory.agent_id == agent_id,
        AgentStudioMemory.owner_id == user.id,
    ))
    if row is None:
        raise NotFoundError("Agent Studio memory not found.")
    await db.delete(row)
    await db.commit()


@router.get("/download/{file_id}")
async def download_agent_studio_file(
    file_id: Annotated[str, ApiPath(min_length=1, max_length=150)],
    user: CurrentUser,
    settings: AppSettings,
    file_format: Annotated[Literal["pptx", "docx", "xlsx", "pdf"], Query(alias="format")],
) -> FileResponse:
    path = resolve_chat_export(file_id, file_format, settings.rag_data_dir / "exports", user.id, settings.master_key_bytes)
    return FileResponse(
        path,
        media_type=MEDIA_TYPES[file_format],
        filename=Path(path).name,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/images/{token}")
async def agent_studio_image(
    token: Annotated[str, ApiPath(min_length=1, max_length=140)],
    user: CurrentUser,
    settings: AppSettings,
    download: Annotated[bool, Query()] = False,
) -> FileResponse:
    path = resolve_chat_image(token, settings.rag_data_dir / "generated_images", user.id, settings.master_key_bytes)
    return FileResponse(
        path,
        media_type="image/png",
        filename="agent-studio-image.png" if download else None,
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
