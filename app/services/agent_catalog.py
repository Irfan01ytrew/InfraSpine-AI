"""Publisher-granted, independently reviewed, revocable internal agent templates."""

from datetime import UTC, datetime
import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.agent_catalog_models import (
    InternalAgentCatalogEvent,
    InternalAgentCatalogGrant,
    InternalAgentCatalogTemplate,
    InternalAgentCatalogVersion,
)
from app.db.models import ROLE_ADMIN, User
from app.services import agents, audit
from app.services.audit import AuditContext

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _constraint_name(exc: IntegrityError) -> str | None:
    current = exc.orig
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        name = getattr(current, "constraint_name", None) or getattr(
            getattr(current, "diag", None), "constraint_name", None
        )
        if name:
            return name
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return None


async def permission_flags(db: AsyncSession, actor: User, *, lock: bool = False) -> tuple[bool, bool]:
    if actor.role == ROLE_ADMIN:
        return True, True
    query = select(InternalAgentCatalogGrant.permission).where(
        InternalAgentCatalogGrant.user_id == actor.id,
        InternalAgentCatalogGrant.revoked_at.is_(None),
    )
    if lock:
        query = query.with_for_update()
    rows = await db.execute(query)
    grants = set(rows.scalars())
    return "publish" in grants, "review" in grants


async def _require(db: AsyncSession, actor: User, permission: str) -> None:
    publish, review = await permission_flags(db, actor, lock=True)
    if not (publish if permission == "publish" else review):
        raise PermissionDeniedError(f"An active agent-catalog {permission} grant is required.")


def _definition(*, name: str, summary: str, agent_type: str, knowledge_scope: str, tool_policy: list[str]):
    normalized_name, normalized_tools = agents._validate_config(
        name=name,
        agent_type=agent_type,
        knowledge_scope=knowledge_scope,
        tool_policy=tool_policy,
    )
    clean_summary = " ".join(summary.split())
    if not clean_summary or len(clean_summary) > 2000:
        raise InvalidInputError("Template summary must contain between 1 and 2000 characters.")
    return normalized_name, clean_summary, normalized_tools


def _event(
    db: AsyncSession,
    *,
    template_id: uuid.UUID,
    version_id: uuid.UUID | None,
    actor: User,
    event_type: str,
    rationale: str | None = None,
) -> None:
    db.add(
        InternalAgentCatalogEvent(
            template_id=template_id,
            version_id=version_id,
            actor_user_id=actor.id,
            event_type=event_type,
            rationale=rationale,
        )
    )


async def create_template(
    db: AsyncSession,
    actor: User,
    ctx: AuditContext,
    *,
    slug: str,
    name: str,
    summary: str,
    agent_type: str,
    knowledge_scope: str,
    tool_policy: list[str],
) -> tuple[InternalAgentCatalogTemplate, InternalAgentCatalogVersion]:
    await _require(db, actor, "publish")
    normalized_slug = slug.strip().lower()
    if not _SLUG.fullmatch(normalized_slug):
        raise InvalidInputError("Template slug must use lowercase letters, digits and single hyphens.")
    name, summary, tools = _definition(
        name=name, summary=summary, agent_type=agent_type, knowledge_scope=knowledge_scope, tool_policy=tool_policy
    )
    template = InternalAgentCatalogTemplate(
        id=uuid.uuid4(), slug=normalized_slug, latest_version=1, created_by_user_id=actor.id
    )
    version = InternalAgentCatalogVersion(
        id=uuid.uuid4(), template_id=template.id, version=1, name=name, summary=summary,
        agent_type=agent_type, knowledge_scope=knowledge_scope, tool_policy=tools,
        status="draft", author_user_id=actor.id,
    )
    # These rows are connected by database foreign keys but deliberately avoid
    # ORM relationships. Flush them in dependency order before appending the event.
    db.add(template)
    await db.flush()
    db.add(version)
    await db.flush()
    _event(db, template_id=template.id, version_id=version.id, actor=actor, event_type="version_created")
    audit.record(
        db, ctx, "agent_catalog.template_created", target=str(template.id),
        details={"slug": normalized_slug, "version": 1, "agent_type": agent_type, "tool_count": len(tools)},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        constraint = _constraint_name(exc)
        if constraint == "uq_internal_agent_catalog_templates_slug":
            raise ConflictError("A catalog template with this slug already exists.") from None
        raise
    await db.refresh(template)
    await db.refresh(version)
    return template, version


async def create_version(
    db: AsyncSession,
    actor: User,
    ctx: AuditContext,
    template_id: uuid.UUID,
    *,
    name: str,
    summary: str,
    agent_type: str,
    knowledge_scope: str,
    tool_policy: list[str],
) -> tuple[InternalAgentCatalogTemplate, InternalAgentCatalogVersion]:
    await _require(db, actor, "publish")
    template = (
        await db.execute(
            select(InternalAgentCatalogTemplate).where(InternalAgentCatalogTemplate.id == template_id).with_for_update()
        )
    ).scalar_one_or_none()
    if template is None or template.revoked_at is not None:
        raise NotFoundError("Active catalog template not found.")
    name, summary, tools = _definition(
        name=name, summary=summary, agent_type=agent_type, knowledge_scope=knowledge_scope, tool_policy=tool_policy
    )
    version_number = template.latest_version + 1
    template.latest_version = version_number
    version = InternalAgentCatalogVersion(
        id=uuid.uuid4(), template_id=template.id, version=version_number, name=name, summary=summary,
        agent_type=agent_type, knowledge_scope=knowledge_scope, tool_policy=tools,
        status="draft", author_user_id=actor.id,
    )
    db.add(version)
    await db.flush()
    _event(db, template_id=template.id, version_id=version.id, actor=actor, event_type="version_created")
    audit.record(
        db, ctx, "agent_catalog.version_created", target=str(template.id),
        details={"slug": template.slug, "version": version_number, "agent_type": agent_type, "tool_count": len(tools)},
    )
    await db.commit()
    await db.refresh(template)
    await db.refresh(version)
    return template, version


async def submit_version(
    db: AsyncSession, actor: User, ctx: AuditContext, version_id: uuid.UUID
) -> InternalAgentCatalogVersion:
    await _require(db, actor, "publish")
    version = (
        await db.execute(select(InternalAgentCatalogVersion).where(InternalAgentCatalogVersion.id == version_id).with_for_update())
    ).scalar_one_or_none()
    if version is None:
        raise NotFoundError("Catalog version not found.")
    template = (
        await db.execute(
            select(InternalAgentCatalogTemplate).where(
                InternalAgentCatalogTemplate.id == version.template_id
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if template is None or template.revoked_at is not None:
        raise NotFoundError("Active catalog template not found.")
    if version.status != "draft":
        raise ConflictError("Only a draft catalog version can be submitted for review.")
    pending_exists = await db.scalar(
        select(InternalAgentCatalogVersion.id).where(
            InternalAgentCatalogVersion.template_id == template.id,
            InternalAgentCatalogVersion.status == "pending_review",
            InternalAgentCatalogVersion.id != version.id,
        ).limit(1)
    )
    if pending_exists is not None:
        raise ConflictError("Another version of this template is already awaiting review.")
    now = datetime.now(UTC)
    version.status = "pending_review"
    version.submitted_by_user_id = actor.id
    version.submitted_at = now
    _event(db, template_id=template.id, version_id=version.id, actor=actor, event_type="version_submitted")
    audit.record(
        db, ctx, "agent_catalog.version_submitted", target=str(version.id),
        details={"slug": template.slug, "version": version.version},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        constraint = _constraint_name(exc)
        if constraint == "uq_internal_agent_catalog_versions_pending_template":
            raise ConflictError("Another version of this template was submitted for review first.") from None
        raise
    await db.refresh(version)
    return version


async def review_version(
    db: AsyncSession,
    actor: User,
    ctx: AuditContext,
    version_id: uuid.UUID,
    *,
    decision: str,
    rationale: str,
) -> InternalAgentCatalogVersion:
    await _require(db, actor, "review")
    if decision not in {"approve", "reject"}:
        raise InvalidInputError("Choose approve or reject for this catalog version.")
    clean_rationale = " ".join(rationale.split())
    if not 3 <= len(clean_rationale) <= 2000:
        raise InvalidInputError("Review rationale must contain between 3 and 2000 characters.")
    version = (
        await db.execute(select(InternalAgentCatalogVersion).where(InternalAgentCatalogVersion.id == version_id).with_for_update())
    ).scalar_one_or_none()
    if version is None or version.status != "pending_review":
        raise NotFoundError("Pending catalog version not found.")
    template = (
        await db.execute(
            select(InternalAgentCatalogTemplate).where(
                InternalAgentCatalogTemplate.id == version.template_id
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if template is None or template.revoked_at is not None:
        raise ConflictError("The catalog template was revoked while this version awaited review.")
    if actor.id in {version.author_user_id, version.submitted_by_user_id}:
        raise PermissionDeniedError("The author and submitter cannot review their own catalog version.")
    now = datetime.now(UTC)
    event_type = "version_approved" if decision == "approve" else "version_rejected"
    if decision == "approve":
        prior_rows = await db.execute(
            select(InternalAgentCatalogVersion).where(
                InternalAgentCatalogVersion.template_id == template.id,
                InternalAgentCatalogVersion.status == "approved",
            ).with_for_update()
        )
        for prior in prior_rows.scalars():
            prior.status = "superseded"
        template.published_version = version.version
        version.status = "approved"
    else:
        version.status = "rejected"
    version.reviewed_by_user_id = actor.id
    version.reviewed_at = now
    version.review_rationale = clean_rationale
    _event(
        db, template_id=template.id, version_id=version.id, actor=actor,
        event_type=event_type, rationale=clean_rationale,
    )
    audit.record(
        db, ctx, f"agent_catalog.{event_type}", target=str(version.id),
        details={"slug": template.slug, "version": version.version},
    )
    await db.commit()
    await db.refresh(version)
    return version


async def revoke_template(
    db: AsyncSession,
    actor: User,
    ctx: AuditContext,
    template_id: uuid.UUID,
    rationale: str,
) -> InternalAgentCatalogTemplate:
    await _require(db, actor, "review")
    clean_rationale = " ".join(rationale.split())
    if not 3 <= len(clean_rationale) <= 2000:
        raise InvalidInputError("Revocation rationale must contain between 3 and 2000 characters.")
    template = (
        await db.execute(
            select(InternalAgentCatalogTemplate).where(InternalAgentCatalogTemplate.id == template_id).with_for_update()
        )
    ).scalar_one_or_none()
    if template is None:
        raise NotFoundError("Catalog template not found.")
    if template.revoked_at is not None:
        raise ConflictError("This catalog template has already been revoked.")
    template.revoked_at = datetime.now(UTC)
    template.revoked_by_user_id = actor.id
    template.revoke_rationale = clean_rationale
    _event(
        db, template_id=template.id, version_id=None, actor=actor,
        event_type="catalog_revoked", rationale=clean_rationale,
    )
    audit.record(
        db, ctx, "agent_catalog.template_revoked", target=str(template.id),
        details={"slug": template.slug, "published_version": template.published_version},
    )
    await db.commit()
    await db.refresh(template)
    return template


async def list_public(db: AsyncSession) -> list[tuple[InternalAgentCatalogTemplate, InternalAgentCatalogVersion]]:
    rows = await db.execute(
        select(InternalAgentCatalogTemplate, InternalAgentCatalogVersion)
        .join(
            InternalAgentCatalogVersion,
            (InternalAgentCatalogVersion.template_id == InternalAgentCatalogTemplate.id)
            & (InternalAgentCatalogVersion.version == InternalAgentCatalogTemplate.published_version),
        )
        .where(
            InternalAgentCatalogTemplate.revoked_at.is_(None),
            InternalAgentCatalogVersion.status == "approved",
        )
        .order_by(func.lower(InternalAgentCatalogTemplate.slug))
    )
    return list(rows.all())


async def list_managed(db: AsyncSession, actor: User) -> tuple[list[dict], bool, bool]:
    can_publish, can_review = await permission_flags(db, actor)
    if not can_publish and not can_review:
        raise PermissionDeniedError("An active agent-catalog publisher or reviewer grant is required.")
    templates_query = select(InternalAgentCatalogTemplate).order_by(
        func.lower(InternalAgentCatalogTemplate.slug)
    )
    if actor.role != ROLE_ADMIN and not can_review:
        templates_query = templates_query.where(InternalAgentCatalogTemplate.created_by_user_id == actor.id)
    templates = list((await db.execute(templates_query)).scalars())
    if not templates:
        return [], can_publish, can_review
    template_ids = [template.id for template in templates]
    author = aliased(User)
    submitter = aliased(User)
    reviewer = aliased(User)
    version_rows = await db.execute(
        select(InternalAgentCatalogVersion, author.email, submitter.email, reviewer.email)
        .join(author, author.id == InternalAgentCatalogVersion.author_user_id)
        .outerjoin(submitter, submitter.id == InternalAgentCatalogVersion.submitted_by_user_id)
        .outerjoin(reviewer, reviewer.id == InternalAgentCatalogVersion.reviewed_by_user_id)
        .where(InternalAgentCatalogVersion.template_id.in_(template_ids))
        .order_by(InternalAgentCatalogVersion.template_id, InternalAgentCatalogVersion.version.desc())
    )
    version_map: dict[uuid.UUID, list[dict]] = {template_id: [] for template_id in template_ids}
    for version, author_email, submitter_email, reviewer_email in version_rows.all():
        version_map[version.template_id].append(
            {
                "id": version.id,
                "version": version.version,
                "name": version.name,
                "summary": version.summary,
                "agent_type": version.agent_type,
                "knowledge_scope": version.knowledge_scope,
                "tool_policy": version.tool_policy,
                "status": version.status,
                "author_email": author_email,
                "created_at": version.created_at,
                "submitted_by_email": submitter_email,
                "submitted_at": version.submitted_at,
                "reviewed_by_email": reviewer_email,
                "reviewed_at": version.reviewed_at,
                "review_rationale": version.review_rationale,
            }
        )
    creator = aliased(User)
    revoker = aliased(User)
    details_rows = await db.execute(
        select(InternalAgentCatalogTemplate, creator.email, revoker.email)
        .join(creator, creator.id == InternalAgentCatalogTemplate.created_by_user_id)
        .outerjoin(revoker, revoker.id == InternalAgentCatalogTemplate.revoked_by_user_id)
        .where(InternalAgentCatalogTemplate.id.in_(template_ids))
    )
    result = []
    for template, creator_email, revoker_email in details_rows.all():
        result.append(
            {
                "id": template.id,
                "slug": template.slug,
                "latest_version": template.latest_version,
                "published_version": template.published_version,
                "created_by_email": creator_email,
                "created_at": template.created_at,
                "revoked_at": template.revoked_at,
                "revoked_by_email": revoker_email,
                "revoke_rationale": template.revoke_rationale,
                "versions": version_map[template.id],
            }
        )
    return result, can_publish, can_review


async def create_grant(
    db: AsyncSession, actor: User, ctx: AuditContext, *, email: str, permission: str
) -> InternalAgentCatalogGrant:
    if actor.role != ROLE_ADMIN:
        raise PermissionDeniedError()
    if permission not in {"publish", "review"}:
        raise InvalidInputError("Choose the publish or review catalog grant.")
    normalized_email = email.strip().casefold()
    user = (
        await db.execute(select(User).where(func.lower(User.email) == normalized_email).with_for_update())
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise NotFoundError("An active account with that email was not found.")
    active_grant_id = await db.scalar(
        select(InternalAgentCatalogGrant.id).where(
            InternalAgentCatalogGrant.user_id == user.id,
            InternalAgentCatalogGrant.permission == permission,
            InternalAgentCatalogGrant.revoked_at.is_(None),
        )
    )
    if active_grant_id is not None:
        raise ConflictError("This account already has an active grant of that type.")
    grant = InternalAgentCatalogGrant(
        id=uuid.uuid4(), user_id=user.id, permission=permission, granted_by_user_id=actor.id
    )
    db.add(grant)
    audit.record(
        db, ctx, "agent_catalog.grant_created", target=str(grant.id),
        details={"user_id": str(user.id), "permission": permission},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        constraint = _constraint_name(exc)
        if constraint == "uq_internal_agent_catalog_grants_active":
            raise ConflictError("This account already has an active grant of that type.") from None
        raise
    await db.refresh(grant)
    return grant


async def revoke_grant(
    db: AsyncSession, actor: User, ctx: AuditContext, grant_id: uuid.UUID
) -> InternalAgentCatalogGrant:
    if actor.role != ROLE_ADMIN:
        raise PermissionDeniedError()
    grant = (
        await db.execute(
            select(InternalAgentCatalogGrant).where(InternalAgentCatalogGrant.id == grant_id).with_for_update()
        )
    ).scalar_one_or_none()
    if grant is None:
        raise NotFoundError("Catalog grant not found.")
    if grant.revoked_at is not None:
        raise ConflictError("This catalog grant has already been revoked.")
    grant.revoked_at = datetime.now(UTC)
    grant.revoked_by_user_id = actor.id
    audit.record(
        db, ctx, "agent_catalog.grant_revoked", target=str(grant.id),
        details={"user_id": str(grant.user_id), "permission": grant.permission},
    )
    await db.commit()
    await db.refresh(grant)
    return grant


async def list_grants(db: AsyncSession, actor: User) -> list[dict]:
    if actor.role != ROLE_ADMIN:
        raise PermissionDeniedError()
    grantor = aliased(User)
    revoker = aliased(User)
    rows = await db.execute(
        select(InternalAgentCatalogGrant, User.email, grantor.email, revoker.email)
        .join(User, User.id == InternalAgentCatalogGrant.user_id)
        .join(grantor, grantor.id == InternalAgentCatalogGrant.granted_by_user_id)
        .outerjoin(revoker, revoker.id == InternalAgentCatalogGrant.revoked_by_user_id)
        .order_by(InternalAgentCatalogGrant.created_at.desc())
    )
    return [
        {
            "id": grant.id,
            "user_id": grant.user_id,
            "email": email,
            "permission": grant.permission,
            "granted_by_email": granted_by,
            "created_at": grant.created_at,
            "revoked_at": grant.revoked_at,
            "revoked_by_email": revoked_by,
        }
        for grant, email, granted_by, revoked_by in rows.all()
    ]
