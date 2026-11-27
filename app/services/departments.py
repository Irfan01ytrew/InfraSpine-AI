"""Department manager operations. Every mutation is confined to an assigned local department."""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.db.models import (
    ROLE_EMPLOYEE,
    ROLE_MANAGER,
    ROLE_USER,
    AccessGroup,
    AccessGroupMember,
    DepartmentManager,
    ROLE_ADMIN,
    User,
    UserInvitation,
)
from app.services import audit
from app.services.audit import AuditContext
from app.services.auth import normalize_email


class DepartmentService:
    def __init__(self, db: AsyncSession, manager: User, ctx: AuditContext) -> None:
        self.db = db
        self.manager = manager
        self.ctx = ctx

    async def managed(self) -> list[AccessGroup]:
        return list(
            (
                await self.db.execute(
                    select(AccessGroup)
                    .join(DepartmentManager, DepartmentManager.group_id == AccessGroup.id)
                    .where(DepartmentManager.manager_user_id == self.manager.id)
                    .order_by(AccessGroup.name)
                )
            ).scalars()
        )

    async def mine(self) -> list[AccessGroup]:
        if self.manager.role == ROLE_ADMIN:
            query = select(AccessGroup).where(AccessGroup.kind == "department", AccessGroup.source == "local")
        else:
            memberships = select(AccessGroupMember.group_id).where(AccessGroupMember.user_id == self.manager.id)
            if self.manager.role == "manager":
                memberships = memberships.union(
                    select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == self.manager.id)
                )
            query = select(AccessGroup).where(
                AccessGroup.id.in_(memberships), AccessGroup.kind == "department", AccessGroup.source == "local"
            )
        return list((await self.db.execute(query.order_by(AccessGroup.name))).scalars())

    async def members(self, group_id: uuid.UUID) -> list[User]:
        await self._department(group_id)
        return list(
            (
                await self.db.execute(
                    select(User)
                    .join(AccessGroupMember, AccessGroupMember.user_id == User.id)
                    .where(AccessGroupMember.group_id == group_id, User.is_active.is_(True))
                    .order_by(User.email)
                )
            ).scalars()
        )

    async def add_member(self, group_id: uuid.UUID, email: str) -> None:
        group = await self._department(group_id)
        user = (
            await self.db.execute(select(User).where(User.email == normalize_email(email)).with_for_update())
        ).scalar_one_or_none()
        if user is None or not user.is_active:
            raise NotFoundError("Active employee not found.")
        if user.role not in (ROLE_EMPLOYEE, ROLE_USER):
            raise PermissionDeniedError("Department managers can only add employee accounts.")
        member = await self.db.get(AccessGroupMember, (group.id, user.id))
        if member is not None:
            return
        self.db.add(AccessGroupMember(group_id=group.id, user_id=user.id, added_by=self.manager.id))
        audit.record(self.db, self.ctx, "department.member_added", target=str(group.id), details={"user": user.email})
        await self.db.commit()

    async def create_employee_invitation(
        self, group_id: uuid.UUID, email: str, *, seat_limit: int | None = None
    ):
        """Invite an employee only into a local department this account manages or holds people.invite for."""
        group = await self._invite_department(group_id)
        from app.services.users import UserAdminService

        return await UserAdminService(self.db, self.manager, self.ctx).create_invitation(
            email, ROLE_EMPLOYEE, [group.id], seat_limit=seat_limit
        )

    async def invitations(self, group_id: uuid.UUID):
        group = await self._invite_department(group_id)
        return list(
            (
                await self.db.execute(
                    select(UserInvitation)
                    .where(
                        UserInvitation.created_by == self.manager.id,
                        UserInvitation.department_ids == [str(group.id)],
                        UserInvitation.accepted_at.is_(None),
                        UserInvitation.revoked_at.is_(None),
                    )
                    .order_by(UserInvitation.created_at.desc())
                )
            ).scalars()
        )

    async def resend_invitation(self, group_id: uuid.UUID, invitation_id: uuid.UUID):
        await self._manager_invitation(group_id, invitation_id)
        from app.services.users import UserAdminService

        return await UserAdminService(self.db, self.manager, self.ctx).resend_invitation(invitation_id)

    async def revoke_invitation(self, group_id: uuid.UUID, invitation_id: uuid.UUID):
        await self._manager_invitation(group_id, invitation_id)
        from app.services.users import UserAdminService

        return await UserAdminService(self.db, self.manager, self.ctx).revoke_invitation(invitation_id)

    async def _manager_invitation(self, group_id: uuid.UUID, invitation_id: uuid.UUID):
        group = await self._invite_department(group_id)
        invitation = (
            await self.db.execute(
                select(UserInvitation)
                .where(
                    UserInvitation.id == invitation_id,
                    UserInvitation.created_by == self.manager.id,
                    UserInvitation.department_ids == [str(group.id)],
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if invitation is None:
            raise NotFoundError("Invitation not found in this department.")
        return invitation

    async def remove_member(self, group_id: uuid.UUID, user_id: uuid.UUID) -> None:
        group = await self._department(group_id)
        target = await self.db.get(User, user_id)
        if target is None or target.role not in (ROLE_EMPLOYEE, ROLE_USER):
            raise NotFoundError("Employee not found in this department.")
        result = await self.db.execute(
            delete(AccessGroupMember).where(
                AccessGroupMember.group_id == group.id,
                AccessGroupMember.user_id == target.id,
            )
        )
        if result.rowcount == 0:
            raise NotFoundError("Employee not found in this department.")
        audit.record(
            self.db, self.ctx, "department.member_removed", target=str(group.id), details={"user_id": str(target.id)}
        )
        await self.db.commit()

    async def invitable(self) -> list[AccessGroup]:
        """Local departments this account may invite Employees into (managed, or covered by people.invite)."""
        from app.core.permission_catalog import PERMISSION_PEOPLE_INVITE
        from app.services.access_control import delegated_department_scope

        query = select(AccessGroup).where(AccessGroup.kind == "department", AccessGroup.source == "local")
        scope = await delegated_department_scope(self.db, self.manager.id, PERMISSION_PEOPLE_INVITE)
        if scope is not None:
            managed = select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == self.manager.id)
            query = query.where(AccessGroup.id.in_(scope) | AccessGroup.id.in_(managed))
        return list((await self.db.execute(query.order_by(AccessGroup.name))).scalars())

    async def _invite_department(self, group_id: uuid.UUID) -> AccessGroup:
        from app.core.permission_catalog import PERMISSION_PEOPLE_INVITE
        from app.services.access_control import delegated_department_scope, holds_department_capability

        if self.manager.role != ROLE_MANAGER and await delegated_department_scope(
            self.db, self.manager.id, PERMISSION_PEOPLE_INVITE
        ) == set():
            raise PermissionDeniedError("Department manager access is required.")
        group = await self.db.get(AccessGroup, group_id)
        if group is None or group.kind != "department" or group.source != "local":
            raise NotFoundError("Department not found.")
        if await self.db.get(DepartmentManager, (group_id, self.manager.id)) is not None:
            return group
        if await holds_department_capability(self.db, self.manager.id, PERMISSION_PEOPLE_INVITE, group_id):
            return group
        raise NotFoundError("Department not found.")  # hide departments outside this account's scope

    async def _department(self, group_id: uuid.UUID) -> AccessGroup:
        group = (
            await self.db.execute(
                select(AccessGroup)
                .join(DepartmentManager, DepartmentManager.group_id == AccessGroup.id)
                .where(
                    AccessGroup.id == group_id,
                    AccessGroup.kind == "department",
                    AccessGroup.source == "local",
                    DepartmentManager.manager_user_id == self.manager.id,
                )
            )
        ).scalar_one_or_none()
        if group is None:
            # Hide departments outside this manager's assignment.
            raise NotFoundError("Department not found.")
        return group
