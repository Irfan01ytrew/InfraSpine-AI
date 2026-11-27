"""Administration of groups, group membership and permission grants, plus the review/publish decision
foundation (ADR-004 WP1). PostgreSQL is the authority: every check here is repeated by constraints or
triggers, so a code path that skips the service still cannot break separation of duties."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.core.permission_catalog import KNOWLEDGE_CATEGORIES, PERMISSION_BY_KEY
from app.db.models import (
    ACL_QUARANTINED,
    CLAIM_SUBMITTED,
    GROUP_DIRECTORY,
    GROUP_LOCAL,
    PERMISSION_PUBLISH,
    PERMISSION_REVIEW,
    STAGE_PUBLISH,
    STAGE_REVIEW,
    SUBJECT_KNOWLEDGE_CLAIM,
    SUBJECT_KNOWLEDGE_REVISION,
    KNOWLEDGE_SUBMITTED,
    AccessGroup,
    AccessGroupMember,
    Document,
    GovernanceApproval,
    KnowledgeClaim,
    KnowledgeRevision,
    PermissionGrant,
    ResourceAcl,
    RoleTemplateAssignment,
    User,
)
from app.services import audit
from app.services.audit import AuditContext
from app.services.auth import normalize_email

STAGE_PERMISSION = {STAGE_REVIEW: PERMISSION_REVIEW, STAGE_PUBLISH: PERMISSION_PUBLISH}


class AccessAdminService:
    """Admin-only operations. The caller (route dependency) has already required an admin."""

    def __init__(self, db: AsyncSession, admin: User, ctx: AuditContext) -> None:
        self.db = db
        self.admin = admin
        self.ctx = ctx

    # ---------------------------------------------------------------- groups

    async def list_groups(self) -> list[tuple[AccessGroup, int]]:
        counts = (
            select(AccessGroupMember.group_id, func.count().label("n")).group_by(AccessGroupMember.group_id).subquery()
        )
        rows = await self.db.execute(
            select(AccessGroup, func.coalesce(counts.c.n, 0))
            .outerjoin(counts, counts.c.group_id == AccessGroup.id)
            .order_by(AccessGroup.kind, func.lower(AccessGroup.name))
        )
        return [(g, n) for g, n in rows]

    async def create_group(
        self,
        name: str,
        kind: str,
        external_id: str | None,
        parent_id: uuid.UUID | None = None,
        owner_id: uuid.UUID | None = None,
    ) -> AccessGroup:
        if parent_id is not None:
            parent = await self.db.get(AccessGroup, parent_id)
            if parent is None:
                raise NotFoundError("The parent workspace does not exist.")
            if not (external_id is None and kind == "project" and parent.kind == "client" and parent.source == GROUP_LOCAL):
                raise InvalidInputError("A local project can only have a local client workspace as its parent.")
        if owner_id is not None:
            owner = await self.db.get(User, owner_id)
            if owner is None:
                raise NotFoundError("The specified workspace owner does not exist.")

        group = AccessGroup(
            name=name,
            kind=kind,
            source=GROUP_DIRECTORY if external_id else GROUP_LOCAL,
            external_id=external_id,
            parent_id=parent_id,
            owner_id=owner_id,
            lifecycle_status="active",
            created_by=self.admin.id,
        )
        self.db.add(group)
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("A group with this name or directory ID already exists.") from None
        audit.record(
            self.db, self.ctx, "access.group_created", target=str(group.id),
            details={"kind": kind, "source": group.source, "parent_id": str(parent_id) if parent_id else None},
        )
        await self.db.commit()
        await self.db.refresh(group)
        return group

    async def update_group(
        self,
        group_id: uuid.UUID,
        *,
        name: str | None = None,
        parent_id: uuid.UUID | None = None,
        owner_id: uuid.UUID | None = None,
        lifecycle_status: str | None = None,
        parent_id_is_set: bool = False,
        owner_id_is_set: bool = False,
    ) -> AccessGroup:
        group = await self._group(group_id, lock=True)
        if name is not None:
            group.name = name
        if parent_id_is_set:
            if parent_id is None:
                group.parent_id = None
            else:
                if parent_id == group.id:
                    raise InvalidInputError("A workspace cannot be its own parent.")
                parent = await self.db.get(AccessGroup, parent_id)
                if parent is None:
                    raise NotFoundError("The parent workspace does not exist.")
                if not (
                    group.source == GROUP_LOCAL
                    and group.kind == "project"
                    and parent.kind == "client"
                    and parent.source == GROUP_LOCAL
                ):
                    raise InvalidInputError("A local project can only have a local client workspace as its parent.")
                group.parent_id = parent_id
        if owner_id_is_set:
            if owner_id is None:
                group.owner_id = None
            else:
                owner = await self.db.get(User, owner_id)
                if owner is None:
                    raise NotFoundError("The specified workspace owner does not exist.")
                group.owner_id = owner_id
        if lifecycle_status is not None:
            if lifecycle_status not in ("active", "archived"):
                raise InvalidInputError("Lifecycle status must be 'active' or 'archived'.")
            group.lifecycle_status = lifecycle_status
        await self.db.flush()
        audit.record(
            self.db, self.ctx, "access.group_updated", target=str(group.id),
            details={
                "lifecycle_status": group.lifecycle_status,
                "parent_id": str(group.parent_id) if group.parent_id else None,
                "owner_id": str(group.owner_id) if group.owner_id else None,
            },
        )
        await self.db.commit()
        await self.db.refresh(group)
        return group

    async def workspace_summary(self, group_id: uuid.UUID) -> dict:
        group = await self._group(group_id)
        member_count = (
            await self.db.execute(
                select(func.count()).select_from(AccessGroupMember).where(AccessGroupMember.group_id == group.id)
            )
        ).scalar() or 0

        child_ids = list(
            (await self.db.execute(select(AccessGroup.id).where(AccessGroup.parent_id == group.id))).scalars().all()
        )
        target_ids = [group.id] + child_ids

        doc_stats = (
            await self.db.execute(
                select(
                    func.count(func.distinct(Document.id)),
                    func.coalesce(func.sum(Document.size_bytes), 0),
                    func.coalesce(func.sum(Document.chunk_count), 0),
                )
                .join(ResourceAcl, ResourceAcl.document_id == Document.id)
                .where(ResourceAcl.principal_group_id.in_(target_ids))
            )
        ).first()

        total_docs, total_bytes, total_chunks = doc_stats if doc_stats else (0, 0, 0)
        return {
            "group_id": group.id,
            "name": group.name,
            "kind": group.kind,
            "parent_id": group.parent_id,
            "owner_id": group.owner_id,
            "lifecycle_status": group.lifecycle_status,
            "member_count": member_count,
            "child_project_count": len(child_ids),
            "total_documents": int(total_docs),
            "total_bytes": int(total_bytes),
            "total_chunks": int(total_chunks),
        }

    async def delete_group(self, group_id: uuid.UUID) -> None:
        group = await self._group(group_id, lock=True)
        if await self.db.scalar(select(AccessGroup.id).where(AccessGroup.parent_id == group.id).limit(1)) is not None:
            raise ConflictError("This client has child projects. Reparent or unlink them before deleting the client.")
        await self.db.delete(group)  # memberships and ACL entries go with it (ON DELETE CASCADE)
        try:
            await self.db.flush()
        except IntegrityError:  # referenced by a permission grant or a decision: history must stay intact
            await self.db.rollback()
            raise ConflictError("This group is the scope of a permission grant or decision and cannot be deleted.") from None
        audit.record(self.db, self.ctx, "access.group_deleted", target=str(group_id))
        await self.db.commit()

    async def members(self, group_id: uuid.UUID) -> list[User]:
        await self._group(group_id)
        rows = await self.db.execute(
            select(User)
            .join(AccessGroupMember, AccessGroupMember.user_id == User.id)
            .where(AccessGroupMember.group_id == group_id)
            .order_by(User.email)
        )
        return list(rows.scalars())

    async def add_member(self, group_id: uuid.UUID, email: str) -> None:
        group = await self._local_group(group_id)
        user = await self._user(email)
        exists = await self.db.get(AccessGroupMember, (group.id, user.id))
        if exists is None:
            self.db.add(AccessGroupMember(group_id=group.id, user_id=user.id, added_by=self.admin.id))
            audit.record(
                self.db, self.ctx, "access.member_added", target=str(group.id), details={"user": user.email}
            )
            await self.db.commit()

    async def remove_member(self, group_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Remove access and revoke any role bundle that included the departed department."""
        group = await self._local_group(group_id)
        user = (
            await self.db.execute(select(User).where(User.id == user_id).with_for_update())
        ).scalar_one_or_none()
        if user is None:
            raise NotFoundError("User not found.")
        result = await self.db.execute(
            delete(AccessGroupMember).where(AccessGroupMember.group_id == group.id, AccessGroupMember.user_id == user_id)
        )
        if result.rowcount == 0:
            raise NotFoundError("This user is not a member of the group.")
        await self._revoke_role_templates_for_department(user.id, group.id)
        audit.record(self.db, self.ctx, "access.member_removed", target=str(group.id), details={"user_id": str(user_id)})
        await self.db.commit()

    async def _revoke_role_templates_for_department(self, user_id: uuid.UUID, group_id: uuid.UUID) -> int:
        assignments = list((await self.db.execute(
            select(RoleTemplateAssignment).where(
                RoleTemplateAssignment.user_id == user_id,
                RoleTemplateAssignment.revoked_at.is_(None),
            ).with_for_update()
        )).scalars())
        affected = [assignment for assignment in assignments if str(group_id) in assignment.department_ids]
        if not affected:
            return 0
        now = datetime.now(UTC)
        assignment_ids = [assignment.id for assignment in affected]
        grants = list((await self.db.execute(
            select(PermissionGrant).where(
                PermissionGrant.role_template_assignment_id.in_(assignment_ids),
                PermissionGrant.revoked_at.is_(None),
            ).with_for_update()
        )).scalars())
        for grant in grants:
            grant.revoked_at = now
            grant.revoked_by = self.admin.id
        for assignment in affected:
            assignment.revoked_at = now
            assignment.revoked_by = self.admin.id
        audit.record(
            self.db,
            self.ctx,
            "access.role_template_assignments_revoked",
            target=str(user_id),
            details={
                "reason": "department_membership_removed",
                "department_id": str(group_id),
                "assignment_count": len(affected),
                "grant_count": len(grants),
            },
        )
        return len(grants)

    # ---------------------------------------------------------------- grants

    async def list_grants(self, include_revoked: bool = False) -> list[tuple[PermissionGrant, str]]:
        # Do not include ResourceAcl here: revocation removes the active ACL row, and an
        # implicit cross join would then hide the retained audit/history grant itself.
        query = select(PermissionGrant, User.email).join(User, User.id == PermissionGrant.user_id)
        if not include_revoked:
            query = query.where(PermissionGrant.revoked_at.is_(None))
        rows = await self.db.execute(query.order_by(PermissionGrant.granted_at.desc()))
        return [(g, email) for g, email in rows]

    async def grant(
        self, email: str, permission: str, category: str | None, scope_group_id: uuid.UUID | None
    ) -> tuple[PermissionGrant, str]:
        definition = PERMISSION_BY_KEY.get(permission)
        if definition is None:
            raise InvalidInputError("Choose a permission from the supported permission catalog.")
        if category is not None and ("category" not in definition.scope_dimensions or category not in KNOWLEDGE_CATEGORIES):
            raise InvalidInputError("This permission does not support the selected category scope.")
        if scope_group_id is not None and "access_group" not in definition.scope_dimensions:
            raise InvalidInputError("This permission does not support an access-group scope.")
        user = await self._user(email)
        if user.id == self.admin.id:  # also a CHECK constraint (no_self_grant)
            raise PermissionDeniedError("Administrators cannot grant permissions to themselves.")
        if not user.is_active:
            raise InvalidInputError("Permissions can only be granted to active users.")
        if scope_group_id is not None:
            await self._group(scope_group_id)
        grant = PermissionGrant(
            user_id=user.id, permission=permission, category=category,
            scope_group_id=scope_group_id, granted_by=self.admin.id,
        )
        self.db.add(grant)
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("This user already holds this permission for this category and scope.") from None
        audit.record(
            self.db, self.ctx, "access.permission_granted", target=str(grant.id),
            details={
                "user": user.email, "permission": permission, "category": category,
                "scope_group_id": str(scope_group_id) if scope_group_id else None,
            },
        )
        await self.db.commit()
        await self.db.refresh(grant)
        return grant, user.email

    async def revoke(self, grant_id: uuid.UUID) -> tuple[PermissionGrant, str]:
        """Effective immediately: decision-time checks read the grant row, locked, in PostgreSQL."""
        grant = (
            await self.db.execute(select(PermissionGrant).where(PermissionGrant.id == grant_id).with_for_update())
        ).scalar_one_or_none()
        if grant is None:
            raise NotFoundError("Permission grant not found.")
        if grant.revoked_at is not None:
            raise ConflictError("This permission grant is already revoked.")
        grant.revoked_at = datetime.now(UTC)
        grant.revoked_by = self.admin.id
        audit.record(self.db, self.ctx, "access.permission_revoked", target=str(grant.id))
        await self.db.commit()
        await self.db.refresh(grant)
        return grant, await self.db.scalar(select(User.email).where(User.id == grant.user_id))

    # ------------------------------------------------------------ quarantine

    async def quarantined_documents(self) -> list[Document]:
        """Quarantine queue. Callers expose identifiers and reasons only, never names or content."""
        rows = await self.db.execute(
            select(Document).where(Document.acl_status == ACL_QUARANTINED).order_by(Document.created_at)
        )
        return list(rows.scalars())

    # --------------------------------------------------------------- helpers

    async def _group(self, group_id: uuid.UUID, *, lock: bool = False) -> AccessGroup:
        query = select(AccessGroup).where(AccessGroup.id == group_id)
        if lock:
            query = query.with_for_update()
        group = (await self.db.execute(query)).scalar_one_or_none()
        if group is None:
            raise NotFoundError("Group not found.")
        return group

    async def _local_group(self, group_id: uuid.UUID) -> AccessGroup:
        group = await self._group(group_id)
        if group.source != GROUP_LOCAL:
            raise ConflictError("Membership of a directory group is managed by the company directory.")
        return group

    async def _user(self, email: str) -> User:
        user = (await self.db.execute(select(User).where(User.email == normalize_email(email)))).scalar_one_or_none()
        if user is None:
            raise NotFoundError("User not found.")
        return user


# ------------------------------------------------------------------ decisions


async def delegated_department_scope(
    db: AsyncSession, user_id: uuid.UUID, permission: str
) -> set[uuid.UUID] | None:
    """Departments an account may act on through an active grant of `permission`:
    None = every department (an unscoped grant), an empty set = none."""
    rows = (
        await db.execute(
            select(PermissionGrant.scope_group_id)
            .outerjoin(RoleTemplateAssignment, RoleTemplateAssignment.id == PermissionGrant.role_template_assignment_id)
            .where(
                PermissionGrant.user_id == user_id,
                PermissionGrant.permission == permission,
                PermissionGrant.revoked_at.is_(None),
                (PermissionGrant.role_template_assignment_id.is_(None))
                | (
                    RoleTemplateAssignment.revoked_at.is_(None)
                    & (RoleTemplateAssignment.expires_at.is_(None) | (RoleTemplateAssignment.expires_at > func.now()))
                ),
            )
        )
    ).scalars().all()
    if any(scope is None for scope in rows):
        return None
    return set(rows)


async def holds_department_capability(
    db: AsyncSession, user_id: uuid.UUID, permission: str, department_id: uuid.UUID
) -> bool:
    scope = await delegated_department_scope(db, user_id, permission)
    return scope is None or department_id in scope


async def find_active_grant(
    db: AsyncSession,
    user_id: uuid.UUID,
    permission: str,
    category: str,
    scope_group_id: uuid.UUID | None,
    *,
    lock: bool = False,
    at: datetime | None = None,
) -> PermissionGrant | None:
    """The most specific active grant covering (category, scope), read from PostgreSQL now."""
    query = (
        select(PermissionGrant)
        .outerjoin(RoleTemplateAssignment, RoleTemplateAssignment.id == PermissionGrant.role_template_assignment_id)
        .where(
            PermissionGrant.user_id == user_id,
            PermissionGrant.permission == permission,
            PermissionGrant.revoked_at.is_(None),
            (PermissionGrant.role_template_assignment_id.is_(None))
            | (
                RoleTemplateAssignment.revoked_at.is_(None)
                & (
                    RoleTemplateAssignment.expires_at.is_(None)
                    | (RoleTemplateAssignment.expires_at > (at or func.now()))
                )
            ),
            (PermissionGrant.category.is_(None)) | (PermissionGrant.category == category),
            (PermissionGrant.scope_group_id.is_(None)) | (PermissionGrant.scope_group_id == scope_group_id),
        )
        .order_by(PermissionGrant.category.is_(None), PermissionGrant.scope_group_id.is_(None))
        .limit(1)
    )
    if lock:
        # Lock only the grant: the left join also covers legacy/direct grants with no assignment row.
        query = query.with_for_update(read=True, of=PermissionGrant)  # FOR SHARE: revocation waits or wins
    return (await db.execute(query)).scalar_one_or_none()


async def record_approval(
    db: AsyncSession,
    approver: User,
    ctx: AuditContext,
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    stage: str,
    category: str,
    scope_group_id: uuid.UUID | None,
    rationale: str,
) -> GovernanceApproval:
    """Record a review or publish decision. PostgreSQL re-checks identity, grants and separation of duties."""
    if subject_type not in (SUBJECT_KNOWLEDGE_CLAIM, SUBJECT_KNOWLEDGE_REVISION):
        raise InvalidInputError("Only a knowledge claim or organizational revision can be approved.")
    if stage not in STAGE_PERMISSION:
        raise InvalidInputError("stage must be 'review' or 'publish'.")
    if subject_type == SUBJECT_KNOWLEDGE_CLAIM:
        subject = (
            await db.execute(
                select(KnowledgeClaim).where(KnowledgeClaim.id == subject_id).with_for_update(read=True)
            )
        ).scalar_one_or_none()
        author_id = subject.claimant_user_id if subject is not None else None
        expected_status = CLAIM_SUBMITTED
        missing_message = "Claim not found."
        status_message = "Only a submitted claim can be approved."
        category_message = "The decision must match the claim category and scope."
    else:
        subject = (
            await db.execute(
                select(KnowledgeRevision).where(KnowledgeRevision.id == subject_id).with_for_update(read=True)
            )
        ).scalar_one_or_none()
        author_id = subject.author_user_id if subject is not None else None
        expected_status = KNOWLEDGE_SUBMITTED
        missing_message = "Knowledge revision not found."
        status_message = "Only a submitted knowledge revision can be approved."
        category_message = "The decision must match the knowledge revision category and scope."
    if subject is None:
        raise NotFoundError(missing_message)
    if subject.status != expected_status:
        raise ConflictError(status_message)
    if category != subject.category or scope_group_id != subject.scope_group_id:
        raise InvalidInputError(category_message)
    if approver.id == author_id:
        raise PermissionDeniedError("You cannot approve your own submission.")
    grant = await find_active_grant(db, approver.id, STAGE_PERMISSION[stage], category, scope_group_id, lock=True)
    if grant is None:
        raise PermissionDeniedError("You do not hold the permission required for this decision.")
    approval = GovernanceApproval(
        subject_type=subject_type,
        subject_id=subject.id,
        subject_author_user_id=author_id,
        approver_user_id=approver.id,
        stage=stage,
        permission=STAGE_PERMISSION[stage],
        category=category,
        scope_group_id=scope_group_id,
        permission_grant_id=grant.id,
        rationale=rationale,
    )
    db.add(approval)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(_decision_conflict(exc)) from None
    audit.record(
        db, ctx, "governance.approval_recorded", target=str(subject_id),
        details={"stage": stage, "category": category, "permission_grant_id": str(grant.id)},
    )
    await db.commit()
    return approval


def _decision_conflict(exc: IntegrityError) -> str:
    text = str(exc.orig)
    if "uq_governance_approvals_one_role" in text:
        return "One person cannot hold two approval roles on the same item."
    if "uq_governance_approvals_one_stage" in text:
        return "This stage has already been decided."
    if "prior review" in text:
        return "A publish decision requires a prior review decision."
    if "matching knowledge claim" in text or "matching knowledge revision" in text:
        return "The decision does not match a submitted item."
    if "submitted claim" in text or "submitted knowledge revision" in text:
        return "Only a submitted item can be approved."
    if "must match the claim" in text or "must match the knowledge revision" in text:
        return "The decision must match the item's category and scope."
    return "The decision was rejected by an authorization rule."
