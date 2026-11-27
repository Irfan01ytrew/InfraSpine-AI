"""Admin-managed immutable templates of catalogued, scoped resource/action permissions."""

from datetime import UTC, datetime, timedelta
import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permission_catalog import PERMISSION_BY_KEY
from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import (
    AccessGroup,
    AccessGroupMember,
    PermissionGrant,
    RoleTemplate,
    RoleTemplateAssignment,
    RoleTemplateChangeRequest,
    User,
)
from app.services import audit
from app.services.audit import AuditContext


class RoleTemplateService:
    def __init__(self, db: AsyncSession, admin: User, ctx: AuditContext) -> None:
        self.db = db
        self.admin = admin
        self.ctx = ctx

    async def list_templates(self) -> list[RoleTemplate]:
        rows = await self.db.execute(select(RoleTemplate).order_by(func.lower(RoleTemplate.name), RoleTemplate.version))
        return list(rows.scalars())

    async def create_template(
        self, name: str, description: str, base_role: str, permissions: list[dict]
    ) -> RoleTemplate:
        if self.admin.role != "admin":
            raise PermissionDeniedError()
        clean_name = " ".join(name.split())
        clean_description = description.strip()
        if not clean_name or base_role not in {"manager", "employee"}:
            raise InvalidInputError("A role template needs a name and a Manager or Employee base role.")
        template = RoleTemplate(
            name=clean_name,
            description=clean_description,
            base_role=base_role,
            version=1,
            permission_specs=permissions,
            created_by=self.admin.id,
        )
        self.db.add(template)
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("A role template with this name already exists.") from None
        audit.record(
            self.db,
            self.ctx,
            "access.role_template_created",
            target=str(template.id),
            details={"name": clean_name, "base_role": base_role, "permission_count": len(permissions)},
        )
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def list_changes(self) -> list[tuple[RoleTemplateChangeRequest, RoleTemplate, str, str | None]]:
        requester = aliased(User)
        decider = aliased(User)
        rows = await self.db.execute(
            select(RoleTemplateChangeRequest, RoleTemplate, requester.email, decider.email)
            .join(RoleTemplate, RoleTemplate.id == RoleTemplateChangeRequest.role_template_id)
            .join(requester, requester.id == RoleTemplateChangeRequest.requested_by)
            .outerjoin(decider, decider.id == RoleTemplateChangeRequest.decided_by)
            .order_by(RoleTemplateChangeRequest.created_at.desc())
        )
        return list(rows.all())

    async def propose_change(
        self, template_id: uuid.UUID, description: str, permissions: list[dict]
    ) -> RoleTemplateChangeRequest:
        if self.admin.role != "admin":
            raise PermissionDeniedError()
        template = (
            await self.db.execute(select(RoleTemplate).where(RoleTemplate.id == template_id).with_for_update(read=True))
        ).scalar_one_or_none()
        if template is None or not template.is_active:
            raise NotFoundError("Active role template not found.")
        proposed_description = description.strip()
        proposed_permissions = permissions
        if template.description == proposed_description and template.permission_specs == proposed_permissions:
            raise InvalidInputError("The proposal must change the template description or permissions.")
        request = RoleTemplateChangeRequest(
            role_template_id=template.id,
            requested_by=self.admin.id,
            proposed_description=proposed_description,
            proposed_permission_specs=proposed_permissions,
        )
        self.db.add(request)
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("A pending change request already exists for this template.") from None
        audit.record(
            self.db,
            self.ctx,
            "access.role_template_change_proposed",
            target=str(request.id),
            details={"template_id": str(template.id), "version": template.version},
        )
        await self.db.commit()
        await self.db.refresh(request)
        return request

    async def decide_change(
        self, request_id: uuid.UUID, decision: str, rationale: str
    ) -> tuple[RoleTemplateChangeRequest, RoleTemplate, int, int]:
        if self.admin.role != "admin":
            raise PermissionDeniedError()
        request = (
            await self.db.execute(
                select(RoleTemplateChangeRequest)
                .where(RoleTemplateChangeRequest.id == request_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if request is None:
            raise NotFoundError("Role-template change request not found.")
        if request.status != "pending":
            raise ConflictError("This role-template change request has already been decided.")
        if request.requested_by == self.admin.id:
            raise PermissionDeniedError("A different administrator must decide this role-template change.")
        template = (
            await self.db.execute(
                select(RoleTemplate).where(RoleTemplate.id == request.role_template_id).with_for_update()
            )
        ).scalar_one_or_none()
        if template is None or not template.is_active:
            raise ConflictError("The template changed while this proposal was awaiting review.")

        now = datetime.now(UTC)
        revoked_assignments = 0
        revoked_grants = 0
        if decision == "reject":
            request.status = "rejected"
            request.decided_by = self.admin.id
            request.decision_rationale = rationale.strip()
            request.decided_at = now
            audit.record(
                self.db,
                self.ctx,
                "access.role_template_change_rejected",
                target=str(request.id),
                details={"template_id": str(template.id), "version": template.version},
            )
            await self.db.commit()
            await self.db.refresh(request)
            return request, template, 0, 0
        if decision != "approve":
            raise InvalidInputError("Choose approve or reject for this role-template change.")

        new_template = RoleTemplate(
            name=template.name,
            description=request.proposed_description,
            base_role=template.base_role,
            version=template.version + 1,
            permission_specs=request.proposed_permission_specs,
            is_active=False,
            supersedes_id=template.id,
            created_by=request.requested_by,
        )
        self.db.add(new_template)
        await self.db.flush()
        template.is_active = False
        # The partial unique index permits one active version per normalized name.
        # Flush the old version's deactivation before enabling its replacement so a
        # batched UPDATE cannot activate the new row first. Both writes remain in the
        # same transaction and are visible atomically at commit.
        await self.db.flush()
        new_template.is_active = True
        await self.db.flush()

        assignments = await self.db.execute(
            select(RoleTemplateAssignment)
            .where(
                RoleTemplateAssignment.role_template_id == template.id,
                RoleTemplateAssignment.revoked_at.is_(None),
            )
            .with_for_update()
        )
        for assignment in assignments.scalars():
            grants = await self.db.execute(
                select(PermissionGrant)
                .where(
                    PermissionGrant.role_template_assignment_id == assignment.id,
                    PermissionGrant.revoked_at.is_(None),
                )
                .with_for_update()
            )
            grant_rows = list(grants.scalars())
            for grant in grant_rows:
                grant.revoked_at = now
                grant.revoked_by = self.admin.id
            assignment.revoked_at = now
            assignment.revoked_by = self.admin.id
            revoked_assignments += 1
            revoked_grants += len(grant_rows)
            audit.record(
                self.db,
                self.ctx,
                "access.role_template_revoked",
                target=str(assignment.id),
                details={
                    "user_id": str(assignment.user_id),
                    "grant_count": len(grant_rows),
                    "reason": "template_version_approved",
                    "replacement_template_id": str(new_template.id),
                },
            )

        request.status = "approved"
        request.decided_by = self.admin.id
        request.decision_rationale = rationale.strip()
        request.decided_at = now
        request.new_template_id = new_template.id
        audit.record(
            self.db,
            self.ctx,
            "access.role_template_change_approved",
            target=str(request.id),
            details={
                "previous_template_id": str(template.id),
                "new_template_id": str(new_template.id),
                "version": new_template.version,
                "revoked_assignments": revoked_assignments,
                "revoked_grants": revoked_grants,
            },
        )
        await self.db.commit()
        await self.db.refresh(request)
        return request, template, revoked_assignments, revoked_grants

    async def set_template_active(self, template_id: uuid.UUID, active: bool) -> RoleTemplate:
        template = (
            await self.db.execute(select(RoleTemplate).where(RoleTemplate.id == template_id).with_for_update())
        ).scalar_one_or_none()
        if template is None:
            raise NotFoundError("Role template not found.")
        if template.is_active == active:
            return template
        template.is_active = active
        audit.record(
            self.db,
            self.ctx,
            "access.role_template_activated" if active else "access.role_template_deactivated",
            target=str(template.id),
        )
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def list_assignments(self, *, include_revoked: bool = False) -> list[tuple[RoleTemplateAssignment, RoleTemplate, User, int]]:
        query = (
            select(RoleTemplateAssignment, RoleTemplate, User)
            .join(RoleTemplate, RoleTemplate.id == RoleTemplateAssignment.role_template_id)
            .join(User, User.id == RoleTemplateAssignment.user_id)
        )
        if not include_revoked:
            query = query.where(RoleTemplateAssignment.revoked_at.is_(None))
        rows = (await self.db.execute(query.order_by(RoleTemplateAssignment.created_at.desc()))).all()
        result = []
        for assignment, template, user in rows:
            count = await self.db.scalar(
                select(func.count()).select_from(PermissionGrant).where(
                    PermissionGrant.role_template_assignment_id == assignment.id,
                )
            )
            result.append((assignment, template, user, int(count or 0)))
        return result

    async def assign(
        self, template_id: uuid.UUID, email: str, department_ids: list[uuid.UUID], expires_at: datetime
    ) -> tuple[RoleTemplateAssignment, RoleTemplate, User, int]:
        now = datetime.now(UTC)
        if expires_at.tzinfo is None or expires_at.utcoffset() is None or not now < expires_at <= now + timedelta(days=365):
            raise InvalidInputError("A role assignment must expire within the next 365 days.")
        template = (
            await self.db.execute(select(RoleTemplate).where(RoleTemplate.id == template_id).with_for_update(read=True))
        ).scalar_one_or_none()
        if template is None or not template.is_active:
            raise NotFoundError("Active role template not found.")
        user = (
            await self.db.execute(select(User).where(User.email == email).with_for_update())
        ).scalar_one_or_none()
        if user is None:
            raise NotFoundError("User not found.")
        if user.id == self.admin.id:
            raise PermissionDeniedError("Administrators cannot grant permissions to themselves.")
        if not user.is_active:
            raise InvalidInputError("A role template can only be assigned to an active user.")
        if user.role != template.base_role:
            raise InvalidInputError("The selected user's base role does not match this template.")
        if not department_ids or len(set(department_ids)) != len(department_ids):
            raise InvalidInputError("Select one or more distinct departments for this role assignment.")
        groups = set(
            (await self.db.execute(
                select(AccessGroup.id).where(
                    AccessGroup.id.in_(department_ids),
                    AccessGroup.kind == "department",
                    AccessGroup.source == "local",
                )
            )).scalars()
        )
        if groups != set(department_ids):
            raise InvalidInputError("Role grants may only be scoped to existing local departments.")
        memberships = set((await self.db.execute(
            select(AccessGroupMember.group_id)
            .join(AccessGroup, AccessGroup.id == AccessGroupMember.group_id)
            .where(
                AccessGroupMember.user_id == user.id,
                AccessGroup.kind == "department",
                AccessGroup.source == "local",
                AccessGroup.id.in_(department_ids),
            )
        )).scalars())
        if memberships != set(department_ids):
            raise InvalidInputError("Role grants must stay within the user's current department memberships.")

        # One grant per (category, department) for knowledge permissions, per department for department
        # capabilities, and a single organization-wide grant for capabilities with no scope (reports.view).
        grants_to_add: list[tuple[str, str | None, uuid.UUID | None]] = []
        for spec in template.permission_specs:
            permission = spec["permission"]
            dimensions = PERMISSION_BY_KEY[permission].scope_dimensions
            if "category" in dimensions:
                grants_to_add.extend((permission, category, group_id) for category in spec["categories"] for group_id in department_ids)
            elif "access_group" in dimensions:
                grants_to_add.extend((permission, None, group_id) for group_id in department_ids)
            else:
                grants_to_add.append((permission, None, None))
        duplicate_conditions = [
            and_(
                PermissionGrant.permission == permission,
                PermissionGrant.category.is_(None) if category is None else PermissionGrant.category == category,
                PermissionGrant.scope_group_id.is_(None) if group_id is None else PermissionGrant.scope_group_id == group_id,
            )
            for permission, category, group_id in grants_to_add
        ]
        existing = await self.db.scalar(
            select(func.count()).select_from(PermissionGrant).where(
                PermissionGrant.user_id == user.id,
                PermissionGrant.revoked_at.is_(None),
                or_(*duplicate_conditions),
            )
        )
        if existing:
            raise ConflictError("The user already holds one or more requested permissions in this department scope.")

        assignment = RoleTemplateAssignment(
            role_template_id=template.id,
            user_id=user.id,
            assigned_by=self.admin.id,
            department_ids=sorted(str(group_id) for group_id in department_ids),
            expires_at=expires_at,
        )
        self.db.add(assignment)
        await self.db.flush()
        self.db.add_all([
            PermissionGrant(
                user_id=user.id,
                role_template_assignment_id=assignment.id,
                permission=permission,
                category=category,
                scope_group_id=group_id,
                granted_by=self.admin.id,
            )
            for permission, category, group_id in grants_to_add
        ])
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("The role assignment conflicts with an existing grant; no permissions were applied.") from None
        audit.record(
            self.db,
            self.ctx,
            "access.role_template_assigned",
            target=str(assignment.id),
            details={
                "user_id": str(user.id),
                "template_id": str(template.id),
                "version": template.version,
                "department_count": len(groups),
                "grant_count": len(grants_to_add),
                "expires_at": expires_at.isoformat(),
            },
        )
        await self.db.commit()
        await self.db.refresh(assignment)
        return assignment, template, user, len(grants_to_add)

    async def revoke_assignment(self, assignment_id: uuid.UUID) -> tuple[RoleTemplateAssignment, int]:
        assignment = (
            await self.db.execute(
                select(RoleTemplateAssignment).where(RoleTemplateAssignment.id == assignment_id).with_for_update()
            )
        ).scalar_one_or_none()
        if assignment is None:
            raise NotFoundError("Role assignment not found.")
        if assignment.revoked_at is not None:
            raise ConflictError("This role assignment has already been revoked.")
        now = datetime.now(UTC)
        grants = await self.db.execute(
            select(PermissionGrant).where(
                PermissionGrant.role_template_assignment_id == assignment.id,
                PermissionGrant.revoked_at.is_(None),
            ).with_for_update()
        )
        grant_rows = list(grants.scalars())
        for grant in grant_rows:
            grant.revoked_at = now
            grant.revoked_by = self.admin.id
        assignment.revoked_at = now
        assignment.revoked_by = self.admin.id
        audit.record(
            self.db,
            self.ctx,
            "access.role_template_revoked",
            target=str(assignment.id),
            details={"user_id": str(assignment.user_id), "grant_count": len(grant_rows)},
        )
        await self.db.commit()
        await self.db.refresh(assignment)
        return assignment, len(grant_rows)
