"""Department-scoped agent catalog; execution is dispatched only by approved existing services."""

import json
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select, update

from app.api.deps import AppSettings, CurrentUser, DbSession, audit_context, get_current_user
from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.core.permission_catalog import PERMISSION_AGENTS_MANAGE
from app.db.models import ROLE_ADMIN, ROLE_MANAGER, AccessGroup, AgentProfile, AgentProfileRun, DepartmentManager
from app.services.access_control import holds_department_capability
from app.rag import tracing
from app.rag.components import RagComponents
from app.rag.respond import rag_respond
from app.schemas.agents import (
    AgentRunRequest,
    AgentRunRecordOut,
    AgentRunHistoryOut,
    AgentRunResponse,
    AgentDepartmentListResponse,
    AgentDepartmentOut,
    AgentProfileListResponse,
    AgentProfileOut,
    AgentProfileRequest,
    AgentProfileVersionRequest,
)
from app.schemas.rag import MarketResearchRequest, RagTrainingRequest
from app.services import audit
from app.services import agents

router = APIRouter(prefix="/api/agents", tags=["agents"], dependencies=[Depends(get_current_user)])


def _get_rag(request: Request) -> RagComponents:
    return request.app.state.rag


Rag = Annotated[RagComponents, Depends(_get_rag)]
_RETRYABLE_CAPABILITIES = frozenset({"knowledge_search", "company_data_analysis"})
_RUN_STALE_AFTER = agents.AGENT_RUN_STALE_AFTER


def _retry_is_available(run: AgentProfileRun, profile: AgentProfile | None) -> bool:
    if run.task_ciphertext is None or run.task_nonce is None or run.task_key_id is None:
        return False
    if run.execution_mode == "queue":
        stale_running = (
            run.status == "running"
            and run.locked_until is not None
            and run.locked_until <= datetime.now(timezone.utc)
        )
    else:
        stale_running = (
            run.status == "running"
            and run.started_at is not None
            and run.started_at <= datetime.now(timezone.utc) - _RUN_STALE_AFTER
        )
    retryable_state = run.status == "failed" or stale_running
    return bool(
        retryable_state
        and run.attempts < 2
        and run.capability in _RETRYABLE_CAPABILITIES
        and profile is not None
        and profile.enabled
        and profile.version == run.profile_version
    )


async def _out(db: DbSession, user: CurrentUser, profile: AgentProfile) -> AgentProfileOut:
    department = await db.get(AccessGroup, profile.department_group_id)
    if department is None:
        raise ConflictError("The agent department no longer exists.")
    can_manage = user.role == ROLE_ADMIN or (
        user.role == ROLE_MANAGER
        and await db.get(DepartmentManager, (profile.department_group_id, user.id)) is not None
    ) or await holds_department_capability(db, user.id, PERMISSION_AGENTS_MANAGE, profile.department_group_id)
    return AgentProfileOut(
        id=profile.id,
        agent_key=profile.agent_key,
        version=profile.version,
        name=profile.name,
        agent_type=profile.agent_type,
        department_group_id=profile.department_group_id,
        department_name=department.name,
        knowledge_scope=profile.knowledge_scope,
        tool_policy=profile.tool_policy,
        enabled=profile.enabled,
        created_at=profile.created_at,
        can_manage=bool(can_manage),
    )


@router.get("", response_model=AgentProfileListResponse)
async def list_agent_profiles(db: DbSession, user: CurrentUser) -> AgentProfileListResponse:
    rows = await agents.list_profiles(db, user)
    return AgentProfileListResponse(agents=[await _out(db, user, row) for row in rows])


@router.get("/departments", response_model=AgentDepartmentListResponse)
async def list_manageable_departments(db: DbSession, user: CurrentUser) -> AgentDepartmentListResponse:
    groups = await agents.manageable_departments(db, user)
    return AgentDepartmentListResponse(
        departments=[AgentDepartmentOut(id=group.id, name=group.name) for group in groups]
    )


@router.post("", response_model=AgentProfileOut, status_code=status.HTTP_201_CREATED)
async def create_agent_profile(
    payload: AgentProfileRequest, db: DbSession, user: CurrentUser, request: Request
) -> AgentProfileOut:
    profile = await agents.create_profile(
        db,
        user,
        audit_context(request, user),
        name=payload.name,
        department_group_id=payload.department_group_id,
        agent_type=payload.agent_type,
        knowledge_scope=payload.knowledge_scope,
        tool_policy=payload.tool_policy,
    )
    return await _out(db, user, profile)


@router.put("/{profile_id}", response_model=AgentProfileOut, status_code=status.HTTP_201_CREATED)
async def version_agent_profile(
    profile_id: uuid.UUID,
    payload: AgentProfileVersionRequest,
    db: DbSession,
    user: CurrentUser,
    request: Request,
) -> AgentProfileOut:
    profile = await agents.create_version(
        db,
        user,
        audit_context(request, user),
        profile_id,
        expected_version=payload.expected_version,
        name=payload.name,
        agent_type=payload.agent_type,
        knowledge_scope=payload.knowledge_scope,
        tool_policy=payload.tool_policy,
    )
    return await _out(db, user, profile)


@router.post("/{profile_id}/disable", response_model=AgentProfileOut)
async def disable_agent_profile(
    profile_id: uuid.UUID, db: DbSession, user: CurrentUser, request: Request
) -> AgentProfileOut:
    profile = await agents.disable_profile(db, user, audit_context(request, user), profile_id)
    return await _out(db, user, profile)


async def _run_capability(
    *,
    run: AgentProfileRun,
    profile: AgentProfile,
    task: str,
    document_ids: list[uuid.UUID] | None,
    confirm_external_search: bool,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    rag: Rag,
    context,
    event_name: str,
) -> AgentRunResponse:
    details = {
        "profile_id": str(profile.id),
        "profile_version": profile.version,
        "capability": run.capability,
        "task_chars": len(task),
        "attempt": run.attempts,
    }
    audit.record(db, context, event_name, target=str(run.id), details=details)
    await db.commit()
    department_scope = profile.department_group_id if (
        run.capability in {"knowledge_search", "company_data_analysis", "training_plan"}
        and profile.knowledge_scope == "department_only"
    ) else None
    try:
        if run.capability in {"knowledge_search", "company_data_analysis"}:
            async with tracing.traced(
                request.app.state.sessionmaker,
                settings,
                route="agent_profile_run",
                question=task,
                user_id=user.id,
                request_id=request.state.request_id,
            ):
                result = await rag_respond(
                    db,
                    user,
                    rag,
                    settings,
                    lambda: request.app.state.llm_runtime.get_client(db),
                    task,
                    history=[],
                    document_ids=document_ids,
                    focus_document_ids=document_ids,
                    memory=None,
                    department_group_id=department_scope,
                )
                await db.commit()
        elif run.capability == "public_web_research":
            from app.api.routes.rag import market_research

            result = await market_research(
                MarketResearchRequest(topic=task, confirm_external_search=confirm_external_search),
                request,
                db,
                user,
                settings,
            )
        elif run.capability == "training_plan":
            from app.api.routes.rag import employee_training

            result = await employee_training(
                RagTrainingRequest(topic=task, document_ids=document_ids),
                request,
                db,
                user,
                settings,
                rag,
                department_scope_group_id=department_scope,
            )
        else:
            raise InvalidInputError("This agent capability is not supported.")

        serialized = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > 2_000_000:
            raise ConflictError("The agent result exceeds the encrypted history limit.")
        encrypted = agents.seal_run_value(request.app.state.cipher, run.id, "result", serialized)
        completed_at = datetime.now(timezone.utc)
        saved = await db.execute(
            update(AgentProfileRun)
            .where(
                AgentProfileRun.id == run.id,
                AgentProfileRun.status == "running",
                AgentProfileRun.attempts == run.attempts,
            )
            .values(
                result_ciphertext=encrypted.ciphertext,
                result_nonce=encrypted.nonce,
                result_key_id=encrypted.key_id,
                status="completed",
                failure_code=None,
                locked_until=None,
                finished_at=completed_at,
            )
        )
        if saved.rowcount != 1:
            raise ConflictError("This agent run attempt is no longer active.")
        audit.record(
            db,
            context,
            "agent.profile_run_completed",
            target=str(run.id),
            details={"profile_id": str(profile.id), "profile_version": profile.version, "capability": run.capability},
        )
        await db.commit()
        return AgentRunResponse(
            run_id=run.id,
            profile_id=profile.id,
            profile_version=profile.version,
            capability=run.capability,
            result=result,
        )
    except Exception as exc:
        await db.rollback()
        failed_at = datetime.now(timezone.utc)
        saved = await db.execute(
            update(AgentProfileRun)
            .where(
                AgentProfileRun.id == run.id,
                AgentProfileRun.status == "running",
                AgentProfileRun.attempts == run.attempts,
            )
            .values(status="failed", failure_code=type(exc).__name__[:60], locked_until=None, finished_at=failed_at)
        )
        if saved.rowcount == 1:
            audit.record(
                db,
                context,
                "agent.profile_run_failed",
                target=str(run.id),
                outcome="failure",
                details={
                    "profile_id": str(profile.id),
                    "profile_version": profile.version,
                    "capability": run.capability,
                    "attempt": run.attempts,
                    "error_type": type(exc).__name__,
                },
            )
            try:
                await db.commit()
            except Exception:
                await db.rollback()
        raise


@router.post("/{profile_id}/queue", response_model=AgentRunRecordOut, status_code=status.HTTP_202_ACCEPTED)
async def queue_agent_profile_run(
    profile_id: uuid.UUID,
    payload: AgentRunRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> AgentRunRecordOut:
    """Persist a bounded, read-only agent run for the separately operated agent worker."""
    if payload.capability not in _RETRYABLE_CAPABILITIES:
        raise InvalidInputError("Only internal read-only agent capabilities can be queued.")
    profile = await agents.profile_for_run(db, user, profile_id, payload.capability)
    run_id = uuid.uuid4()
    requested_at = datetime.now(timezone.utc)
    encrypted_task = agents.seal_run_value(
        request.app.state.cipher,
        run_id,
        "task",
        json.dumps(
            {"task": payload.task, "document_ids": [str(value) for value in (payload.document_ids or [])]},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    run = AgentProfileRun(
        id=run_id,
        profile_id=profile.id,
        profile_version=profile.version,
        department_group_id=profile.department_group_id,
        requester_user_id=user.id,
        capability=payload.capability,
        task_ciphertext=encrypted_task.ciphertext,
        task_nonce=encrypted_task.nonce,
        task_key_id=encrypted_task.key_id,
        status="queued",
        execution_mode="queue",
        attempts=0,
        requested_at=requested_at,
        available_at=requested_at,
        started_at=None,
    )
    db.add(run)
    audit.record(
        db,
        audit_context(request, user),
        "agent.profile_run_queued",
        target=str(run.id),
        details={"profile_id": str(profile.id), "profile_version": profile.version, "capability": run.capability},
    )
    await db.commit()
    await db.refresh(run)
    return AgentRunRecordOut(
        run_id=run.id,
        profile_id=run.profile_id,
        profile_version=run.profile_version,
        capability=run.capability,
        status=run.status,
        attempts=run.attempts,
        failure_code=run.failure_code,
        retry_available=False,
        requested_at=run.requested_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        content_purged_at=run.content_purged_at,
    )


@router.post("/{profile_id}/runs", response_model=AgentRunResponse)
async def run_agent_profile(
    profile_id: uuid.UUID,
    payload: AgentRunRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    rag: Rag,
) -> AgentRunResponse:
    """Dispatch registered tools and retain a caller-scoped encrypted result record."""
    profile = await agents.profile_for_run(db, user, profile_id, payload.capability)
    if payload.capability == "public_web_research" and not payload.confirm_external_search:
        raise InvalidInputError("Confirm that this topic may be sent to the configured Gemini public search service.")

    run_id = uuid.uuid4()
    encrypted_task = agents.seal_run_value(
        request.app.state.cipher,
        run_id,
        "task",
        json.dumps(
            {"task": payload.task, "document_ids": [str(value) for value in (payload.document_ids or [])]},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    run = AgentProfileRun(
        id=run_id,
        profile_id=profile.id,
        profile_version=profile.version,
        department_group_id=profile.department_group_id,
        requester_user_id=user.id,
        capability=payload.capability,
        task_ciphertext=encrypted_task.ciphertext,
        task_nonce=encrypted_task.nonce,
        task_key_id=encrypted_task.key_id,
        status="running",
        execution_mode="sync",
        attempts=1,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    await db.commit()
    return await _run_capability(
        run=run,
        profile=profile,
        task=payload.task,
        document_ids=payload.document_ids,
        confirm_external_search=payload.confirm_external_search,
        request=request,
        db=db,
        user=user,
        settings=settings,
        rag=rag,
        context=audit_context(request, user),
        event_name="agent.profile_run_started",
    )


@router.get("/runs/{run_id}", response_model=AgentRunRecordOut)
async def get_agent_run(
    run_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser
) -> AgentRunRecordOut:
    run = await db.get(AgentProfileRun, run_id)
    if run is None:
        raise NotFoundError("Agent run not found.")
    await agents.require_run_owner_access(db, user, run)
    result = None
    if run.status == "completed" and run.result_ciphertext is not None:
        raw = agents.open_run_value(request.app.state.cipher, run, "result")
        try:
            result = json.loads(raw)
        except ValueError:
            raise ConflictError("The stored agent result could not be decoded.") from None
    profile = await db.get(AgentProfile, run.profile_id)
    retry_available = _retry_is_available(run, profile)
    return AgentRunRecordOut(
        run_id=run.id,
        profile_id=run.profile_id,
        profile_version=run.profile_version,
        capability=run.capability,
        status=run.status,
        attempts=run.attempts,
        failure_code=run.failure_code,
        retry_available=retry_available,
        requested_at=run.requested_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        content_purged_at=run.content_purged_at,
        result=result,
    )


@router.get("/{profile_id}/runs", response_model=AgentRunHistoryOut)
async def list_agent_profile_runs(
    profile_id: uuid.UUID, request: Request, db: DbSession, user: CurrentUser
) -> AgentRunHistoryOut:
    profile = await db.get(AgentProfile, profile_id)
    if profile is None:
        raise NotFoundError("Agent profile not found.")
    await agents.require_profile_history_access(db, user, profile)
    rows = (
        await db.execute(
            select(AgentProfileRun)
            .where(AgentProfileRun.profile_id == profile_id, AgentProfileRun.requester_user_id == user.id)
            .order_by(AgentProfileRun.requested_at.desc())
            .limit(20)
        )
    ).scalars().all()
    output = []
    for run in rows:
        result = None
        if run.status == "completed" and run.result_ciphertext is not None:
            try:
                result = json.loads(agents.open_run_value(request.app.state.cipher, run, "result"))
            except ValueError:
                raise ConflictError("The stored agent result could not be decoded.") from None
        output.append(
            AgentRunRecordOut(
                run_id=run.id,
                profile_id=run.profile_id,
                profile_version=run.profile_version,
                capability=run.capability,
                status=run.status,
                attempts=run.attempts,
                failure_code=run.failure_code,
                retry_available=_retry_is_available(run, profile),
                requested_at=run.requested_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
                content_purged_at=run.content_purged_at,
                result=result,
            )
        )
    return AgentRunHistoryOut(runs=output)


@router.post("/runs/{run_id}/retry", response_model=AgentRunResponse)
async def retry_agent_run(
    run_id: uuid.UUID,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
    rag: Rag,
) -> AgentRunResponse:
    run = (
        await db.execute(select(AgentProfileRun).where(AgentProfileRun.id == run_id).with_for_update())
    ).scalar_one_or_none()
    if run is None:
        raise NotFoundError("Agent run not found.")
    await agents.require_run_owner_access(db, user, run)
    if run.capability not in _RETRYABLE_CAPABILITIES:
        raise ConflictError("This capability is not automatically retryable because it may have external or persistent effects.")
    now = datetime.now(timezone.utc)
    if run.status == "running":
        if run.execution_mode == "queue":
            if run.locked_until is None or run.locked_until > now:
                raise ConflictError("This agent run is still in progress.")
        elif run.started_at is None or run.started_at > now - _RUN_STALE_AFTER:
            raise ConflictError("This agent run is still in progress.")
        run.status = "failed"
        run.failure_code = "interrupted"
        run.finished_at = now
        audit.record(
            db,
            audit_context(request, user),
            "agent.profile_run_interrupted",
            outcome="failure",
            target=str(run.id),
            details={"profile_id": str(run.profile_id), "attempt": run.attempts},
        )
    if run.status != "failed" or run.attempts >= 2:
        raise ConflictError("Only a failed agent run with a remaining retry may be retried.")
    if run.task_ciphertext is None or run.task_nonce is None or run.task_key_id is None:
        raise ConflictError("The task content expired under the retention policy and cannot be retried.")
    profile = await agents.profile_for_run(db, user, run.profile_id, run.capability)
    if profile.version != run.profile_version:
        raise ConflictError("The agent profile changed; start a new run using its active version.")
    try:
        stored_input = json.loads(agents.open_run_value(request.app.state.cipher, run, "task"))
        task = stored_input["task"]
        document_ids = [uuid.UUID(value) for value in stored_input["document_ids"]]
    except (ValueError, KeyError, TypeError):
        raise ConflictError("The stored agent request failed its integrity check.") from None
    if not isinstance(task, str):
        raise ConflictError("The stored agent request failed its integrity check.")
    run.status = "running"
    run.execution_mode = "sync"
    run.attempts += 1
    run.failure_code = None
    run.started_at = now
    run.finished_at = None
    run.locked_until = None
    await db.commit()
    return await _run_capability(
        run=run,
        profile=profile,
        task=task,
        document_ids=document_ids or None,
        confirm_external_search=False,
        request=request,
        db=db,
        user=user,
        settings=settings,
        rag=rag,
        context=audit_context(request, user),
        event_name="agent.profile_run_retry_started",
    )
