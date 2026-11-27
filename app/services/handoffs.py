"""Durable, department-scoped transfer notes for employee knowledge preservation."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import (
    ROLE_ADMIN,
    AccessGroup,
    AccessGroupMember,
    DepartmentManager,
    EmployeeHandoff,
    User,
)
from app.services import audit
from app.services.audit import AuditContext


class HandoffService:
    def __init__(self, db: AsyncSession, user: User, ctx: AuditContext) -> None:
        self.db = db
        self.user = user
        self.ctx = ctx

    async def list_readable(self) -> list[tuple[EmployeeHandoff, str, str | None]]:
        query = (
            select(EmployeeHandoff, AccessGroup.name, User.email)
            .join(AccessGroup, AccessGroup.id == EmployeeHandoff.department_group_id)
            .outerjoin(User, User.id == EmployeeHandoff.acknowledged_by)
            .order_by(EmployeeHandoff.created_at.desc())
        )
        if self.user.role != ROLE_ADMIN:
            managed = exists().where(
                DepartmentManager.group_id == EmployeeHandoff.department_group_id,
                DepartmentManager.manager_user_id == self.user.id,
            )
            query = query.where(or_(EmployeeHandoff.author_user_id == self.user.id, managed))
        return list((await self.db.execute(query)).all())

    async def create(self, department_id: uuid.UUID, title: str, body: str) -> EmployeeHandoff:
        group = (
            await self.db.execute(
                select(AccessGroup).where(
                    AccessGroup.id == department_id,
                    AccessGroup.kind == "department",
                    AccessGroup.source == "local",
                )
            )
        ).scalar_one_or_none()
        if group is None:
            raise InvalidInputError("Select an existing local department.")
        if self.user.role != ROLE_ADMIN:
            member = await self.db.get(AccessGroupMember, (group.id, self.user.id))
            manager = await self.db.get(DepartmentManager, (group.id, self.user.id))
            if member is None and manager is None:
                raise PermissionDeniedError("You can only submit a handoff for a department you belong to or manage.")
        clean_title = " ".join(title.split())
        clean_body = body.replace("\r\n", "\n").strip()
        if not clean_title or len(clean_title) > 180 or not clean_body or len(clean_body) > 12000:
            raise InvalidInputError("Add a title and handoff notes within the field limits.")
        handoff = EmployeeHandoff(
            author_user_id=self.user.id,
            author_email=self.user.email,
            department_group_id=group.id,
            title=clean_title,
            body=clean_body,
        )
        self.db.add(handoff)
        audit.record(
            self.db,
            self.ctx,
            "handoff.submitted",
            target=str(handoff.id),
            details={"department_id": str(group.id)},
        )
        await self.db.commit()
        await self.db.refresh(handoff)
        return handoff

    async def acknowledge(self, handoff_id: uuid.UUID) -> EmployeeHandoff:
        query = select(EmployeeHandoff).where(EmployeeHandoff.id == handoff_id).with_for_update()
        handoff = (await self.db.execute(query)).scalar_one_or_none()
        if handoff is None:
            raise NotFoundError("Handoff not found.")
        if self.user.role != ROLE_ADMIN:
            manager = await self.db.get(DepartmentManager, (handoff.department_group_id, self.user.id))
            if manager is None:
                raise NotFoundError("Handoff not found.")
        if handoff.author_user_id == self.user.id:
            raise PermissionDeniedError("You cannot acknowledge your own handoff.")
        if handoff.status != "submitted":
            raise ConflictError("This handoff has already been acknowledged.")
        handoff.status = "acknowledged"
        handoff.acknowledged_by = self.user.id
        handoff.acknowledged_at = datetime.now(UTC)
        audit.record(
            self.db,
            self.ctx,
            "handoff.acknowledged",
            target=str(handoff.id),
            details={"department_id": str(handoff.department_group_id)},
        )
        await self.db.commit()
        await self.db.refresh(handoff)
        return handoff
