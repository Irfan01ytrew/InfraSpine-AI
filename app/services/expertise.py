"""Employee-authored skill tags and department-scoped expertise search."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError
from app.db.models import AccessGroup, AccessGroupMember, DepartmentManager, User
from app.schemas.expertise import ExpertiseMatch, ExpertiseProfile, ExpertiseSearchResponse, normalize_skill
from app.services import audit
from app.services.audit import AuditContext

MAX_MATCHES = 50


class ExpertiseService:
    def __init__(self, db: AsyncSession, actor: User, ctx: AuditContext) -> None:
        self.db = db
        self.actor = actor
        self.ctx = ctx

    def profile(self) -> ExpertiseProfile:
        return ExpertiseProfile(user_id=str(self.actor.id), tags=list(self.actor.expertise_tags or []))

    async def update_profile(self, tags: list[str]) -> ExpertiseProfile:
        cleaned = [normalize_skill(tag) for tag in tags]
        if len(cleaned) > 20 or any(not tag or len(tag) > 60 for tag in cleaned):
            raise InvalidInputError("Enter up to 20 expertise tags, each 1 to 60 characters.")
        if len(cleaned) != len(set(cleaned)):
            raise InvalidInputError("Expertise tags must be unique, ignoring letter case.")
        self.actor.expertise_tags = cleaned
        audit.record(
            self.db,
            self.ctx,
            "expertise.profile_updated",
            target=str(self.actor.id),
            details={"tag_count": len(cleaned)},
        )
        await self.db.commit()
        await self.db.refresh(self.actor)
        return self.profile()

    async def search(self, skill: str, *, after: str | None = None) -> ExpertiseSearchResponse:
        normalized = normalize_skill(skill)
        if len(normalized) < 2 or len(normalized) > 60:
            raise InvalidInputError("Search for an exact expertise tag between 2 and 60 characters.")

        if after is not None and (not after or len(after) > 320 or any(ord(c) < 32 for c in after)):
            raise InvalidInputError("Invalid expertise search cursor.")

        scoped_groups: set[uuid.UUID] | None
        if self.actor.role == "admin":
            scoped_groups = None
        elif self.actor.role == "manager":
            scoped_groups = set(
                (
                    await self.db.execute(
                        select(DepartmentManager.group_id)
                        .join(AccessGroup, AccessGroup.id == DepartmentManager.group_id)
                        .where(
                            DepartmentManager.manager_user_id == self.actor.id,
                            AccessGroup.kind == "department",
                        )
                    )
                ).scalars()
            )
        else:
            scoped_groups = set(
                (
                    await self.db.execute(
                        select(AccessGroupMember.group_id)
                        .join(AccessGroup, AccessGroup.id == AccessGroupMember.group_id)
                        .where(
                            AccessGroupMember.user_id == self.actor.id,
                            AccessGroup.kind == "department",
                        )
                    )
                ).scalars()
            )

        if scoped_groups == set():
            users = []
        else:
            statement = select(User).where(
                User.is_active.is_(True),
                User.id != self.actor.id,
                User.expertise_tags.contains([normalized]),
            )
            if after is not None:
                statement = statement.where(User.email > after)
            if scoped_groups is not None:
                statement = (
                    statement.join(AccessGroupMember, AccessGroupMember.user_id == User.id)
                    .where(AccessGroupMember.group_id.in_(scoped_groups))
                    .distinct()
                )
            users = list(
                (
                    await self.db.execute(statement.order_by(User.email).limit(MAX_MATCHES + 1))
                ).scalars()
            )
        has_more = len(users) > MAX_MATCHES
        users = users[:MAX_MATCHES]
        departments_by_user: dict[uuid.UUID, list[str]] = {user.id: [] for user in users}
        if users:
            department_filters = [
                AccessGroupMember.user_id.in_([user.id for user in users]),
                AccessGroup.kind == "department",
            ]
            if scoped_groups is not None:
                department_filters.append(AccessGroup.id.in_(scoped_groups))
            group_rows = await self.db.execute(
                select(AccessGroupMember.user_id, AccessGroup.name)
                .join(AccessGroup, AccessGroup.id == AccessGroupMember.group_id)
                .where(*department_filters)
                .order_by(AccessGroup.name)
            )
            for user_id, name in group_rows:
                departments_by_user[user_id].append(name)

        response = ExpertiseSearchResponse(
            skill=normalized,
            next_cursor=users[-1].email if has_more else None,
            matches=[
                ExpertiseMatch(
                    user_id=str(user.id),
                    email=user.email,
                    tags=list(user.expertise_tags or []),
                    shared_departments=departments_by_user[user.id],
                )
                for user in users
            ],
        )
        # Directory searches can reveal employee-authored skill labels. Record the event
        # without copying the query itself into the immutable audit trail.
        audit.record(
            self.db,
            self.ctx,
            "expertise.directory_searched",
            target=str(self.actor.id),
            details={"result_count": len(response.matches), "query_length": len(normalized)},
        )
        await self.db.commit()
        return response
