"""Positions (job titles) and reporting lines, kept separate from software access.

A position says what someone is called or does; a manager link says whom they report to. Neither grants a
permission: access stays with the software role, custom roles and department membership.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.db.models import AccessGroup, AccessGroupMember, User
from app.db.org_models import JOB_FAMILIES, OrgAssignmentHistory, Position
from app.services import audit
from app.services.audit import AuditContext
from app.services.position_library import POSITION_LIBRARY

# Deeper than any real organization; a longer chain means corrupt data, not a hierarchy.
MAX_CHAIN = 64


@dataclass(frozen=True)
class ChartNode:
    user_id: uuid.UUID
    email: str
    role: str
    position_id: uuid.UUID | None
    position_title: str | None
    job_family: str | None
    reports_to_id: uuid.UUID | None
    departments: list[str]


class OrgStructureService:
    def __init__(self, db: AsyncSession, admin: User, ctx: AuditContext) -> None:
        self.db = db
        self.admin = admin
        self.ctx = ctx

    # ------------------------------------------------------------------ positions
    async def list_positions(self, include_archived: bool = True) -> list[tuple[Position, int]]:
        holders = (
            select(User.position_id, func.count().label("n"))
            .where(User.is_active.is_(True), User.position_id.is_not(None))
            .group_by(User.position_id)
            .subquery()
        )
        query = select(Position, func.coalesce(holders.c.n, 0)).outerjoin(holders, holders.c.position_id == Position.id)
        if not include_archived:
            query = query.where(Position.is_active.is_(True))
        rows = await self.db.execute(query.order_by(Position.is_active.desc(), Position.level.asc().nulls_last(), func.lower(Position.title)))
        return [(position, count) for position, count in rows.all()]

    async def create_position(self, title: str, job_family: str, level: int | None, description: str) -> Position:
        title = " ".join(title.split())
        self._check(title, job_family)
        await self._ensure_title_free(title)
        position = Position(title=title, job_family=job_family, level=level, description=description.strip(), created_by=self.admin.id)
        self.db.add(position)
        await self._flush_unique()
        audit.record(self.db, self.ctx, "position.created", target=title, details={"job_family": job_family, "level": level})
        await self.db.commit()
        return position

    async def update_position(
        self, position_id: uuid.UUID, *, title: str | None, job_family: str | None, level: int | None,
        description: str | None, is_active: bool | None, clear_level: bool = False,
    ) -> Position:
        position = await self._position(position_id)
        changes: dict = {}
        if title is not None:
            title = " ".join(title.split())
            self._check(title, job_family or position.job_family)
            if title.lower() != position.title.lower() and (is_active if is_active is not None else position.is_active):
                await self._ensure_title_free(title, exclude=position.id)
            if title != position.title:
                changes["title"] = {"from": position.title, "to": title}
                position.title = title
        if job_family is not None and job_family != position.job_family:
            self._check(position.title, job_family)
            changes["job_family"] = {"from": position.job_family, "to": job_family}
            position.job_family = job_family
        if clear_level or (level is not None and level != position.level):
            changes["level"] = {"from": position.level, "to": None if clear_level else level}
            position.level = None if clear_level else level
        if description is not None and description.strip() != position.description:
            position.description = description.strip()
            changes["description"] = "updated"
        if is_active is not None and is_active != position.is_active:
            if is_active:
                await self._ensure_title_free(position.title, exclude=position.id)
            changes["is_active"] = {"from": position.is_active, "to": is_active}
            position.is_active = is_active
        if not changes:
            return position
        position.updated_at = datetime.now(UTC)
        await self._flush_unique()
        # Archiving keeps current holders and every history row; it only stops new assignments.
        action = "position.archived" if changes.get("is_active", {}).get("to") is False else (
            "position.restored" if changes.get("is_active", {}).get("to") is True else "position.updated")
        audit.record(self.db, self.ctx, action, target=position.title, details=changes)
        await self.db.commit()
        return position

    async def apply_library(self, titles: list[str] | None) -> tuple[list[str], list[str]]:
        """Add library positions that do not exist yet (all of them when `titles` is None)."""
        wanted = [p for p in POSITION_LIBRARY if titles is None or p.title in set(titles)]
        existing = {
            t.lower() for t in (await self.db.execute(select(Position.title).where(Position.is_active.is_(True)))).scalars()
        }
        created, skipped = [], []
        for entry in wanted:
            if entry.title.lower() in existing:
                skipped.append(entry.title)
                continue
            self.db.add(Position(title=entry.title, job_family=entry.job_family, level=entry.level, description=entry.description, created_by=self.admin.id))
            existing.add(entry.title.lower())
            created.append(entry.title)
        await self._flush_unique()
        if created:
            audit.record(self.db, self.ctx, "position.library_applied", details={"created": len(created), "skipped": len(skipped)})
        await self.db.commit()
        return created, skipped

    # ------------------------------------------------------------ assignments
    async def assign(self, user_id: uuid.UUID, position_id: uuid.UUID | None, reports_to_id: uuid.UUID | None) -> User:
        user = await self.db.get(User, user_id, with_for_update=True)
        if user is None:
            raise NotFoundError("User not found.")
        if not user.is_active:
            raise InvalidInputError("Reactivate this account before changing its position or manager.")
        position = None
        if position_id is not None:
            position = await self._position(position_id)
            if not position.is_active and position_id != user.position_id:
                raise InvalidInputError("This position is archived. Restore it, or pick an active position.")
        manager = None
        if reports_to_id is not None:
            if reports_to_id == user.id:
                raise InvalidInputError("A person cannot report to themselves.")
            manager = await self.db.get(User, reports_to_id)
            if manager is None or not manager.is_active:
                raise InvalidInputError("The manager must be an active account.")
            await self._reject_cycle(user.id, manager)
        if user.position_id == position_id and user.reports_to_id == reports_to_id:
            return user
        now = datetime.now(UTC)
        await self.db.execute(
            update(OrgAssignmentHistory)
            .where(OrgAssignmentHistory.user_id == user.id, OrgAssignmentHistory.ended_at.is_(None))
            .values(ended_at=now)
        )
        self.db.add(OrgAssignmentHistory(
            user_id=user.id, position_id=position_id, position_title=position.title if position else None,
            reports_to_id=reports_to_id, reports_to_email=manager.email if manager else None,
            changed_by=self.admin.id, started_at=now,
        ))
        previous = {"position_id": str(user.position_id) if user.position_id else None, "reports_to_id": str(user.reports_to_id) if user.reports_to_id else None}
        user.position_id, user.reports_to_id = position_id, reports_to_id
        audit.record(
            self.db, self.ctx, "user.org_assignment_changed", target=user.email,
            details={"from": previous, "position": position.title if position else None, "reports_to": manager.email if manager else None},
        )
        await self.db.commit()
        return user

    async def history(self, user_id: uuid.UUID) -> list[OrgAssignmentHistory]:
        rows = await self.db.execute(
            select(OrgAssignmentHistory).where(OrgAssignmentHistory.user_id == user_id).order_by(OrgAssignmentHistory.started_at.desc(), OrgAssignmentHistory.id.desc())
        )
        return list(rows.scalars())

    # ------------------------------------------------------------------ helpers
    async def _reject_cycle(self, user_id: uuid.UUID, manager: User) -> None:
        """Walk up from the proposed manager; reaching the person means the change would create a loop."""
        seen: set[uuid.UUID] = set()
        current: uuid.UUID | None = manager.id
        for _ in range(MAX_CHAIN):
            if current is None:
                return
            if current == user_id:
                raise InvalidInputError(f"{manager.email} already reports (directly or indirectly) to this person, so this would create a reporting loop.")
            if current in seen:
                return
            seen.add(current)
            current = (await self.db.execute(select(User.reports_to_id).where(User.id == current))).scalar_one_or_none()
        raise InvalidInputError("The reporting chain is too deep.")

    async def _position(self, position_id: uuid.UUID) -> Position:
        position = await self.db.get(Position, position_id)
        if position is None:
            raise NotFoundError("Position not found.")
        return position

    async def _ensure_title_free(self, title: str, exclude: uuid.UUID | None = None) -> None:
        query = select(Position.id).where(Position.is_active.is_(True), func.lower(Position.title) == title.lower())
        if exclude is not None:
            query = query.where(Position.id != exclude)
        if (await self.db.execute(query)).first() is not None:
            raise ConflictError(f"An active position named “{title}” already exists.")

    async def _flush_unique(self) -> None:
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("An active position with this title already exists.") from None

    @staticmethod
    def _check(title: str, job_family: str) -> None:
        if not 2 <= len(title) <= 120:
            raise InvalidInputError("A position title must be 2 to 120 characters.")
        if job_family not in JOB_FAMILIES:
            raise InvalidInputError("Unknown job family.")


async def org_chart(db: AsyncSession) -> list[ChartNode]:
    """Active people with their position, manager and department names; no access or contact details beyond email."""
    users = (await db.execute(select(User).where(User.is_active.is_(True)).order_by(User.email))).scalars().all()
    positions = {p.id: p for p in (await db.execute(select(Position))).scalars()}
    memberships: dict[uuid.UUID, list[str]] = {}
    rows = await db.execute(
        select(AccessGroupMember.user_id, AccessGroup.name)
        .join(AccessGroup, AccessGroup.id == AccessGroupMember.group_id)
        .where(AccessGroup.kind == "department")
        .order_by(AccessGroup.name)
    )
    for uid, name in rows.all():
        memberships.setdefault(uid, []).append(name)
    active_ids = {u.id for u in users}
    nodes = []
    for user in users:
        position = positions.get(user.position_id) if user.position_id else None
        nodes.append(ChartNode(
            user_id=user.id, email=user.email, role=user.role,
            position_id=user.position_id, position_title=position.title if position else None,
            job_family=position.job_family if position else None,
            # A manager who left is shown as no manager; the history keeps the old link.
            reports_to_id=user.reports_to_id if user.reports_to_id in active_ids else None,
            departments=memberships.get(user.id, []),
        ))
    return nodes
