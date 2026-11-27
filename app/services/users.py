"""Account administration and single-use, expiring employee onboarding."""

from __future__ import annotations

import csv
import io
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.core.permission_catalog import PERMISSION_STEWARD
from app.core.security import hash_password, hash_session_token
from app.db.models import (
    ROLE_ADMIN,
    ROLE_EMPLOYEE,
    ROLE_MANAGER,
    AccessGroup,
    AccessGroupMember,
    DepartmentManager,
    Document,
    KnowledgeEntry,
    KnowledgeEvidence,
    KnowledgeRevision,
    PermissionGrant,
    RoleTemplate,
    RoleTemplateAssignment,
    User,
    UserInvitation,
    UserSession,
)
from app.db.org_models import OrgAssignmentHistory, Position
from app.services import audit
from app.services.audit import AuditContext
from app.services.auth import normalize_email
from app.services.access_control import find_active_grant
from app.rag.access import readable_by
from app.schemas.users import CsvInvitationPreviewResponse, CsvInvitationPreviewRow

INVITATION_HOURS = 72
CSV_INVITATION_MAX_BYTES = 1_000_000
CSV_INVITATION_MAX_ROWS = 500


class UserAdminService:
    def __init__(self, db: AsyncSession, admin: User, ctx: AuditContext) -> None:
        self.db = db
        self.admin = admin
        self.ctx = ctx

    async def list_users(self) -> list[tuple[User, list[tuple[AccessGroup, bool]]]]:
        users = list((await self.db.execute(select(User).order_by(User.email))).scalars())
        result = []
        for user in users:
            rows = await self.db.execute(
                select(AccessGroup, DepartmentManager.manager_user_id.is_not(None))
                .join(AccessGroupMember, AccessGroupMember.group_id == AccessGroup.id)
                .outerjoin(
                    DepartmentManager,
                    (DepartmentManager.group_id == AccessGroup.id)
                    & (DepartmentManager.manager_user_id == user.id),
                )
                .where(AccessGroupMember.user_id == user.id, AccessGroup.kind == "department")
                .order_by(AccessGroup.name)
            )
            result.append((user, list(rows.all())))
        return result

    async def create_invitation(
        self,
        email: str,
        role: str,
        department_ids: list[uuid.UUID],
        *,
        seat_limit: int | None = None,
        role_template_id: uuid.UUID | None = None,
        position_id: uuid.UUID | None = None,
        reports_to_id: uuid.UUID | None = None,
    ) -> tuple[UserInvitation, str]:
        if seat_limit is not None:
            # Serialize invitation creation so concurrent admins cannot reserve the same final seat.
            await self.db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 7_713_221_004})
            now = datetime.now(UTC)
            active_users = await self.db.scalar(select(func.count()).select_from(User).where(User.is_active)) or 0
            pending_invites = await self.db.scalar(
                select(func.count()).select_from(UserInvitation).where(
                    UserInvitation.accepted_at.is_(None), UserInvitation.revoked_at.is_(None),
                    UserInvitation.expires_at > now,
                )
            ) or 0
            if active_users + pending_invites >= seat_limit:
                raise ConflictError("The active license seat limit has been reached.")
        email = normalize_email(email)
        if role not in (ROLE_MANAGER, ROLE_EMPLOYEE):
            raise InvalidInputError("Invitations are for manager or employee accounts.")
        departments = set(department_ids)
        if len(departments) != len(department_ids):
            raise InvalidInputError("A department can only be selected once.")
        if role == ROLE_MANAGER and not departments:
            raise InvalidInputError("A manager must be assigned to at least one department.")
        if role_template_id is not None:
            if self.admin.role != ROLE_ADMIN:
                raise PermissionDeniedError("Only administrators can invite people with a custom role.")
            template = await self.db.get(RoleTemplate, role_template_id)
            if template is None or not template.is_active:
                raise InvalidInputError("Choose an active custom role.")
            if template.base_role != role:
                raise InvalidInputError(f"The custom role {template.name} is for {template.base_role} accounts.")
            if not departments:
                raise InvalidInputError("Choose at least one department for a custom role.")
        if position_id is not None or reports_to_id is not None:
            if self.admin.role != ROLE_ADMIN:
                raise PermissionDeniedError("Only administrators can set a position or manager on an invitation.")
            await validate_org_placement(self.db, position_id, reports_to_id)
        if await self.db.scalar(select(User.id).where(User.email == email)) is not None:
            raise ConflictError("An account with this email already exists.")
        now = datetime.now(UTC)
        await self.db.execute(
            update(UserInvitation)
            .where(
                UserInvitation.email == email,
                UserInvitation.accepted_at.is_(None),
                UserInvitation.revoked_at.is_(None),
                UserInvitation.expires_at <= now,
            )
            .values(revoked_at=now)
        )
        active_invite = await self.db.scalar(
            select(UserInvitation.id).where(
                UserInvitation.email == email,
                UserInvitation.accepted_at.is_(None),
                UserInvitation.revoked_at.is_(None),
                UserInvitation.expires_at > now,
            )
        )
        if active_invite is not None:
            raise ConflictError("An active invitation already exists for this email.")
        if departments:
            found = set(
                (
                    await self.db.execute(
                        select(AccessGroup.id).where(
                            AccessGroup.id.in_(departments),
                            AccessGroup.kind == "department",
                            AccessGroup.source == "local",
                        )
                    )
                ).scalars()
            )
            if found != departments:
                raise InvalidInputError("Select existing local department groups only.")

        token = secrets.token_urlsafe(32)
        invitation = UserInvitation(
            email=email,
            role=role,
            department_ids=sorted(str(group_id) for group_id in departments),
            role_template_id=role_template_id,
            position_id=position_id,
            reports_to_id=reports_to_id,
            token_hash=hash_session_token(token),
            created_by=self.admin.id,
            expires_at=now + timedelta(hours=INVITATION_HOURS),
        )
        self.db.add(invitation)
        audit.record(
            self.db,
            self.ctx,
            "onboarding.invitation_created",
            target=email,
            details={
                "role": role,
                "department_ids": invitation.department_ids,
                "expires_in_hours": INVITATION_HOURS,
                **({"role_template_id": str(role_template_id)} if role_template_id else {}),
                **({"position_id": str(position_id)} if position_id else {}),
                **({"reports_to_id": str(reports_to_id)} if reports_to_id else {}),
            },
        )
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("An invitation could not be created for this email.") from None
        await self.db.refresh(invitation)
        # The raw bearer token is returned once and is never stored or written to audit logs.
        return invitation, token

    async def resend_invitation(self, invitation_id: uuid.UUID) -> tuple[UserInvitation, str]:
        """Rotate a pending invite's one-time token and restart its expiry window."""
        invitation = (
            await self.db.execute(select(UserInvitation).where(UserInvitation.id == invitation_id).with_for_update())
        ).scalar_one_or_none()
        if invitation is None:
            raise NotFoundError("Invitation not found.")
        if invitation.accepted_at is not None:
            raise ConflictError("This invitation has already been accepted.")
        if invitation.revoked_at is not None:
            raise ConflictError("This invitation has already been revoked.")
        token = secrets.token_urlsafe(32)
        invitation.token_hash = hash_session_token(token)
        invitation.expires_at = datetime.now(UTC) + timedelta(hours=INVITATION_HOURS)
        audit.record(
            self.db,
            self.ctx,
            "onboarding.invitation_resent",
            target=invitation.email,
            details={"role": invitation.role, "expires_in_hours": INVITATION_HOURS},
        )
        await self.db.commit()
        await self.db.refresh(invitation)
        return invitation, token

    async def preview_invitation_csv(
        self, csv_text: str, *, seat_limit: int | None = None
    ) -> CsvInvitationPreviewResponse:
        """Validate a bounded CSV without creating users or invitation records."""
        if len(csv_text.encode("utf-8")) > CSV_INVITATION_MAX_BYTES:
            raise InvalidInputError("CSV preview is limited to 1 MB.")
        try:
            reader = csv.DictReader(io.StringIO(csv_text, newline=""))
            headers = reader.fieldnames
            if not headers:
                raise InvalidInputError("CSV must include an email header.")
            normalized_headers = [header.lstrip("\ufeff").strip().lower() if header else "" for header in headers]
            if len(set(normalized_headers)) != len(normalized_headers):
                raise InvalidInputError("CSV headers must not be duplicated.")
            allowed_headers = {"email", "role", "departments", "position", "reports_to"}
            if "email" not in normalized_headers or set(normalized_headers) - allowed_headers:
                raise InvalidInputError("Use only the CSV columns email, role, departments, position and reports_to.")
            reader.fieldnames = normalized_headers
            raw_rows = list(reader)
        except csv.Error:
            raise InvalidInputError("The CSV could not be parsed. Check its quoting and delimiters.") from None
        if len(raw_rows) > CSV_INVITATION_MAX_ROWS:
            raise InvalidInputError(f"CSV preview is limited to {CSV_INVITATION_MAX_ROWS} rows.")

        department_rows = list(
            (
                await self.db.execute(
                    select(AccessGroup.id, AccessGroup.name).where(
                        AccessGroup.kind == "department", AccessGroup.source == "local"
                    )
                )
            ).all()
        )
        department_by_name = {name.strip().casefold(): (group_id, name) for group_id, name in department_rows}
        position_by_title = {
            title.strip().casefold(): (position_id, title)
            for position_id, title in (await self.db.execute(select(Position.id, Position.title).where(Position.is_active.is_(True)))).all()
        }
        manager_by_email = {
            email: user_id
            for user_id, email in (await self.db.execute(select(User.id, User.email).where(User.is_active.is_(True)))).all()
        }
        parsed: list[dict] = []
        seen_emails: set[str] = set()
        for index, row in enumerate(raw_rows, start=2):
            errors: list[str] = []
            if None in row:
                errors.append("This row has more columns than the CSV header.")
            email_value = (row.get("email") or "").strip()
            email = normalize_email(email_value) if email_value else ""
            role_value = (row.get("role") or "employee").strip().lower() or "employee"
            role = role_value if role_value in (ROLE_MANAGER, ROLE_EMPLOYEE) else None
            if not email:
                errors.append("Enter an email address.")
            elif (
                len(email) > 320
                or email.count("@") != 1
                or "." not in email.rsplit("@", 1)[-1]
                or any(character.isspace() for character in email)
            ):
                errors.append("Enter a valid email address.")
            if role is None:
                errors.append("Role must be employee or manager.")
            if email and email in seen_emails:
                errors.append("This email is duplicated in the CSV.")
            if email:
                seen_emails.add(email)

            department_names = [part.strip() for part in (row.get("departments") or "").split(";") if part.strip()]
            if len({name.casefold() for name in department_names}) != len(department_names):
                errors.append("A department is listed more than once.")
            department_ids: list[uuid.UUID] = []
            normalized_names: set[str] = set()
            for name in department_names:
                normalized_name = name.casefold()
                found = department_by_name.get(normalized_name)
                if found is None:
                    errors.append(f"Department '{name[:120]}' does not exist.")
                    continue
                if normalized_name not in normalized_names:
                    department_ids.append(found[0])
                    normalized_names.add(normalized_name)
            if role == ROLE_MANAGER and not department_ids:
                errors.append("A manager must have at least one existing department.")
            position_value = " ".join((row.get("position") or "").split())
            position = position_by_title.get(position_value.casefold()) if position_value else None
            if position_value and position is None:
                errors.append(f"Position '{position_value[:120]}' does not exist. Add it under Positions first.")
            manager_raw = (row.get("reports_to") or "").strip()
            manager_value = normalize_email(manager_raw) if manager_raw else ""
            manager_id = manager_by_email.get(manager_value) if manager_value else None
            if manager_value and manager_id is None:
                errors.append(f"Manager '{manager_value[:120]}' is not an active account.")
            parsed.append(
                {
                    "row_number": index,
                    "email": email or None,
                    "role": role,
                    "department_ids": department_ids,
                    "departments": [department_by_name[name.casefold()][1] for name in department_names if name.casefold() in department_by_name],
                    "position_id": position[0] if position else None,
                    "position_title": position[1] if position else None,
                    "reports_to_id": manager_id,
                    "reports_to_email": manager_value if manager_id else None,
                    "errors": errors,
                }
            )

        emails = [row["email"] for row in parsed if row["email"]]
        existing_users = set(
            (await self.db.execute(select(User.email).where(User.email.in_(emails)))).scalars()
        ) if emails else set()
        now = datetime.now(UTC)
        active_invitations = set(
            (
                await self.db.execute(
                    select(UserInvitation.email).where(
                        UserInvitation.email.in_(emails),
                        UserInvitation.accepted_at.is_(None),
                        UserInvitation.revoked_at.is_(None),
                        UserInvitation.expires_at > now,
                    )
                )
            ).scalars()
        ) if emails else set()
        base_seats = 0
        if seat_limit is not None:
            active_users = await self.db.scalar(select(func.count()).select_from(User).where(User.is_active)) or 0
            pending_invites = await self.db.scalar(
                select(func.count()).select_from(UserInvitation).where(
                    UserInvitation.accepted_at.is_(None),
                    UserInvitation.revoked_at.is_(None),
                    UserInvitation.expires_at > now,
                )
            ) or 0
            base_seats = active_users + pending_invites

        valid_new_rows = 0
        response_rows: list[CsvInvitationPreviewRow] = []
        for row in parsed:
            if row["email"] in existing_users:
                row["errors"].append("An account already exists for this email.")
            elif row["email"] in active_invitations:
                row["errors"].append("An active invitation already exists for this email.")
            if seat_limit is not None and not row["errors"]:
                if base_seats + valid_new_rows >= seat_limit:
                    row["errors"].append("The active license seat limit would be exceeded.")
                else:
                    valid_new_rows += 1
            response_rows.append(
                CsvInvitationPreviewRow(
                    row_number=row["row_number"],
                    email=row["email"],
                    role=row["role"],
                    department_ids=row["department_ids"],
                    departments=row["departments"],
                    position_id=row["position_id"],
                    position_title=row["position_title"],
                    reports_to_id=row["reports_to_id"],
                    reports_to_email=row["reports_to_email"],
                    valid=not row["errors"],
                    errors=row["errors"],
                )
            )
        valid_count = sum(row.valid for row in response_rows)
        audit.record(
            self.db,
            self.ctx,
            "onboarding.csv_previewed",
            details={"rows": len(response_rows), "valid_rows": valid_count, "invalid_rows": len(response_rows) - valid_count},
        )
        await self.db.commit()
        return CsvInvitationPreviewResponse(
            total_rows=len(response_rows),
            valid_rows=valid_count,
            invalid_rows=len(response_rows) - valid_count,
            rows=response_rows,
        )

    async def list_invitations(self) -> list[UserInvitation]:
        return list(
            (
                await self.db.execute(
                    select(UserInvitation)
                    .where(UserInvitation.accepted_at.is_(None), UserInvitation.revoked_at.is_(None))
                    .order_by(UserInvitation.created_at.desc())
                )
            ).scalars()
        )

    async def revoke_invitation(self, invitation_id: uuid.UUID) -> UserInvitation:
        invitation = (
            await self.db.execute(select(UserInvitation).where(UserInvitation.id == invitation_id).with_for_update())
        ).scalar_one_or_none()
        if invitation is None:
            raise NotFoundError("Invitation not found.")
        if invitation.accepted_at is not None:
            raise ConflictError("This invitation has already been accepted.")
        if invitation.revoked_at is not None:
            raise ConflictError("This invitation has already been revoked.")
        invitation.revoked_at = datetime.now(UTC)
        audit.record(self.db, self.ctx, "onboarding.invitation_revoked", target=invitation.email)
        await self.db.commit()
        await self.db.refresh(invitation)
        return invitation

    async def update_role_and_departments(
        self, user_id: uuid.UUID, role: str, department_ids: list[uuid.UUID]
    ) -> User:
        if role not in (ROLE_ADMIN, ROLE_MANAGER, ROLE_EMPLOYEE):
            raise InvalidInputError("Choose Administrator, Manager or Employee.")
        departments = set(department_ids)
        if len(departments) != len(department_ids):
            raise InvalidInputError("A department can only be selected once.")
        if role == ROLE_MANAGER and not departments:
            raise InvalidInputError("A manager must be assigned to at least one department.")
        # Same lock as deactivation, so concurrent requests cannot both remove "the other" administrator.
        await self.db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 7_713_221_005})
        user = (
            await self.db.execute(select(User).where(User.id == user_id).with_for_update())
        ).scalar_one_or_none()
        if user is None:
            raise NotFoundError("User not found.")
        if user.id == self.admin.id and role != user.role:
            raise PermissionDeniedError("You cannot change your own role. Ask another administrator.")
        if role == ROLE_ADMIN:
            return await self._grant_administrator(user)
        if user.role == ROLE_ADMIN:
            other_admins = await self.db.scalar(
                select(func.count()).select_from(User).where(
                    User.role == ROLE_ADMIN, User.is_active.is_(True), User.id != user.id
                )
            )
            if not other_admins:
                raise PermissionDeniedError("The last active administrator cannot be changed to another role.")
        if departments:
            found = set(
                (
                    await self.db.execute(
                        select(AccessGroup.id).where(
                            AccessGroup.id.in_(departments),
                            AccessGroup.kind == "department",
                            AccessGroup.source == "local",
                        )
                    )
                ).scalars()
            )
            if found != departments:
                raise InvalidInputError("Select existing local department groups only.")

        old_role = user.role
        await self.db.execute(delete(DepartmentManager).where(DepartmentManager.manager_user_id == user.id))
        await self.db.flush()
        # Department changes only affect local department memberships. Directory membership remains authoritative.
        old_local_ids = set(
            (
                await self.db.execute(
                    select(AccessGroupMember.group_id)
                    .join(AccessGroup, AccessGroup.id == AccessGroupMember.group_id)
                    .where(
                        AccessGroupMember.user_id == user.id,
                        AccessGroup.kind == "department",
                        AccessGroup.source == "local",
                    )
                )
            ).scalars()
        )
        if old_role != role or old_local_ids != departments:
            await self._revoke_role_template_assignments(user, reason="role_or_department_changed")
        for group_id in old_local_ids - departments:
            await self.db.execute(
                delete(AccessGroupMember).where(
                    AccessGroupMember.user_id == user.id, AccessGroupMember.group_id == group_id
                )
            )
        demoted_admin = old_role == ROLE_ADMIN
        user.role = role
        if demoted_admin:  # administrator rights end now, not when existing sessions expire
            await self.db.execute(
                update(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None)).values(
                    revoked_at=datetime.now(UTC)
                )
            )
        await self.db.flush()
        for group_id in departments:
            if group_id not in old_local_ids:
                self.db.add(AccessGroupMember(group_id=group_id, user_id=user.id, added_by=self.admin.id))
            if role == ROLE_MANAGER:
                self.db.add(DepartmentManager(group_id=group_id, manager_user_id=user.id, assigned_by=self.admin.id))
        audit.record(
            self.db,
            self.ctx,
            "user.administrator_revoked" if demoted_admin else "user.role_and_departments_changed",
            target=user.email,
            details={"from_role": old_role, "to_role": role, "department_ids": sorted(map(str, departments))},
        )
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("The role or department assignment could not be saved.") from None
        await self.db.refresh(user)
        return user

    async def _grant_administrator(self, user: User) -> User:
        """Make an active Manager or Employee an Administrator. The caller holds the admin advisory lock."""
        if user.role == ROLE_ADMIN:
            return user
        if not user.is_active:
            raise InvalidInputError("Reactivate the account before making it an administrator.")
        old_role = user.role
        # Administrators see every department, so department management and templated grants no longer apply.
        await self._revoke_role_template_assignments(user, reason="role_or_department_changed")
        await self.db.execute(delete(DepartmentManager).where(DepartmentManager.manager_user_id == user.id))
        user.role = ROLE_ADMIN
        audit.record(self.db, self.ctx, "user.administrator_granted", target=user.email, details={"from_role": old_role, "to_role": ROLE_ADMIN})
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def set_active(
        self, user_id: uuid.UUID, active: bool, knowledge_transfer_to: uuid.UUID | None = None
    ) -> User:
        # Serialize offboarding around the active-admin and stewardship checks. This keeps
        # concurrent Admin UI and directory requests from making lockout decisions on stale state.
        await self.db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 7_713_221_005})
        user = (
            await self.db.execute(select(User).where(User.id == user_id).with_for_update())
        ).scalar_one_or_none()
        if user is None:
            raise NotFoundError("User not found.")
        if user.id == self.admin.id and not active:
            raise PermissionDeniedError("You cannot deactivate your own account.")
        if user.role == ROLE_ADMIN and not active:
            active_admins = await self.db.scalar(
                select(func.count()).select_from(User).where(User.role == ROLE_ADMIN, User.is_active.is_(True))
            )
            if (active_admins or 0) <= 1:
                raise PermissionDeniedError("The last active administrator cannot be deactivated.")
        if user.is_active == active:
            return user
        transferred = 0
        if not active:
            successor_id = knowledge_transfer_to or self.admin.id
            successor = (
                await self.db.execute(select(User).where(User.id == successor_id).with_for_update())
            ).scalar_one_or_none()
            if successor is None or not successor.is_active or successor.id == user.id:
                raise InvalidInputError("Choose another active account to steward company knowledge.")
            transferred = await self._transfer_company_knowledge(user, successor)
            await self._revoke_role_template_assignments(user, reason="user_deactivated")
        user.is_active = active
        if not active:
            await self.db.execute(
                update(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None)).values(
                    revoked_at=datetime.now(UTC)
                )
            )
        audit.record(
            self.db,
            self.ctx,
            "user.activated" if active else "user.deactivated",
            target=user.email,
            details={
                "role": user.role,
                **({"knowledge_entries_transferred": transferred} if not active else {}),
            },
        )
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def _revoke_role_template_assignments(self, user: User, *, reason: str) -> int:
        """Revoke templated permissions whenever a principal's role or department boundary changes."""
        assignments = list((await self.db.execute(
            select(RoleTemplateAssignment).where(
                RoleTemplateAssignment.user_id == user.id,
                RoleTemplateAssignment.revoked_at.is_(None),
            ).with_for_update()
        )).scalars())
        if not assignments:
            return 0
        now = datetime.now(UTC)
        ids = [assignment.id for assignment in assignments]
        grants = list((await self.db.execute(
            select(PermissionGrant).where(
                PermissionGrant.role_template_assignment_id.in_(ids),
                PermissionGrant.revoked_at.is_(None),
            ).with_for_update()
        )).scalars())
        for grant in grants:
            grant.revoked_at = now
            grant.revoked_by = self.admin.id
        for assignment in assignments:
            assignment.revoked_at = now
            assignment.revoked_by = self.admin.id
        audit.record(
            self.db,
            self.ctx,
            "access.role_template_assignments_revoked",
            target=str(user.id),
            details={"reason": reason, "assignment_count": len(assignments), "grant_count": len(grants)},
        )
        return len(grants)

    async def transfer_knowledge(self, user_id: uuid.UUID, successor_user_id: uuid.UUID) -> int:
        """Recover company-owned entries from an account that was deactivated before this workflow."""
        departing = (
            await self.db.execute(select(User).where(User.id == user_id).with_for_update())
        ).scalar_one_or_none()
        if departing is None:
            raise NotFoundError("User not found.")
        successor = (
            await self.db.execute(
                select(User).where(User.id == successor_user_id, User.is_active.is_(True)).with_for_update()
            )
        ).scalar_one_or_none()
        if successor is None or successor.id == departing.id:
            raise InvalidInputError("Choose another active account to steward company knowledge.")
        transferred = await self._transfer_company_knowledge(departing, successor)
        if transferred:
            await self.db.commit()
        return transferred

    async def _transfer_company_knowledge(self, departing: User, successor: User) -> int:
        """Move current scoped/shared entries while keeping immutable contributor/revision history.

        Private drafts stay with their original owner. A non-admin successor must hold an active
        category/group-scoped stewardship grant and retain source access; shared entries remain admin-only.
        """
        rows = list(
            (
                await self.db.execute(
                    select(KnowledgeEntry, KnowledgeRevision)
                    .join(KnowledgeRevision, KnowledgeRevision.id == KnowledgeEntry.current_revision_id)
                    .where(
                        KnowledgeEntry.owner_user_id == departing.id,
                        KnowledgeRevision.visibility.in_(("restricted", "shared")),
                    )
                    .with_for_update()
                )
            ).all()
        )
        if not rows:
            return 0

        if successor.role != ROLE_ADMIN:
            for _, revision in rows:
                if revision.visibility == "shared":
                    raise PermissionDeniedError("Shared company knowledge must remain with an administrator.")
                steward_grant = await find_active_grant(
                    self.db,
                    successor.id,
                    PERMISSION_STEWARD,
                    revision.category,
                    revision.scope_group_id,
                    lock=True,
                )
                if steward_grant is None:
                    raise PermissionDeniedError(
                        "The selected steward needs an active data.steward grant for every transferred category and access group."
                    )
                group_id = revision.scope_group_id
                if group_id is None:
                    raise PermissionDeniedError("The selected steward cannot own this knowledge scope without an access group.")
                member = await self.db.get(AccessGroupMember, (group_id, successor.id))
                manager = await self.db.get(DepartmentManager, (group_id, successor.id))
                if member is None and manager is None:
                    raise PermissionDeniedError("The selected steward must belong to each scoped access group or manage its department.")

        for _, revision in rows:
            unreadable_source = await self.db.scalar(
                select(
                    exists().where(
                        KnowledgeEvidence.revision_id == revision.id,
                        or_(
                            KnowledgeEvidence.document_id.is_(None),
                            ~KnowledgeEvidence.document_id.in_(
                                select(Document.id).where(readable_by(successor))
                            ),
                        ),
                    )
                )
            )
            if unreadable_source:
                raise PermissionDeniedError("The selected steward must retain access to every cited source.")

        for entry, _ in rows:
            entry.owner_user_id = successor.id
        audit.record(
            self.db,
            self.ctx,
            "knowledge.stewardship_transferred",
            target=departing.email,
            details={
                "from_user_id": str(departing.id),
                "to_user_id": str(successor.id),
                "entry_count": len(rows),
            },
        )
        await self.db.flush()
        return len(rows)


async def accept_invitation(db: AsyncSession, token: str, password: str, ctx: AuditContext) -> User:
    """Consume an invitation atomically. A failed, expired or reused token has one generic response."""
    invitation = (
        await db.execute(
            select(UserInvitation).where(UserInvitation.token_hash == hash_session_token(token)).with_for_update()
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if (
        invitation is None
        or invitation.accepted_at is not None
        or invitation.revoked_at is not None
        or invitation.expires_at <= now
    ):
        raise PermissionDeniedError("This invitation is invalid or has expired.")
    if await db.scalar(select(User.id).where(User.email == invitation.email)) is not None:
        raise ConflictError("An account with this email already exists. Contact your administrator.")
    department_ids = {uuid.UUID(value) for value in invitation.department_ids}
    groups = list(
        (
            await db.execute(
                select(AccessGroup).where(
                    AccessGroup.id.in_(department_ids),
                    AccessGroup.kind == "department",
                    AccessGroup.source == "local",
                )
            )
        ).scalars()
    ) if department_ids else []
    if len(groups) != len(department_ids):
        raise ConflictError("A department on this invitation is no longer available. Contact your administrator.")

    user = User(email=invitation.email, role=invitation.role, password_hash=hash_password(password))
    db.add(user)
    try:
        await db.flush()
        for group in groups:
            db.add(AccessGroupMember(group_id=group.id, user_id=user.id, added_by=invitation.created_by))
            if invitation.role == ROLE_MANAGER:
                db.add(DepartmentManager(group_id=group.id, manager_user_id=user.id, assigned_by=invitation.created_by))
        invitation.accepted_at = now
        placement = await apply_invited_org_placement(db, invitation, user)
        audit.record(
            db,
            AuditContext(user.id, user.email, ctx.ip_address, ctx.request_id),
            "onboarding.accepted",
            target=user.email,
            details={"role": user.role, "department_ids": sorted(map(str, department_ids)), **placement},
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("This invitation could not be accepted. Contact your administrator.") from None
    await db.refresh(user)
    if invitation.role_template_id is not None:
        await _apply_invited_custom_role(db, invitation, user, ctx)
    return user


async def _apply_invited_custom_role(db: AsyncSession, invitation: UserInvitation, user: User, ctx: AuditContext) -> None:
    """Assign the invitation's custom role for a year on behalf of the inviting administrator. The account is
    already created; if the inviter is no longer an active administrator or the role cannot be applied, sign-up
    still succeeds and the outcome is audited so an administrator can assign the role manually."""
    from app.services.role_templates import RoleTemplateService

    inviter = await db.get(User, invitation.created_by) if invitation.created_by else None
    reason = None
    if inviter is None or not inviter.is_active or inviter.role != ROLE_ADMIN:
        reason = "inviter_not_active_admin"
    else:
        try:
            await RoleTemplateService(db, inviter, AuditContext(inviter.id, inviter.email, ctx.ip_address, ctx.request_id)).assign(
                invitation.role_template_id,
                user.email,
                [uuid.UUID(value) for value in invitation.department_ids],
                datetime.now(UTC) + timedelta(days=365) - timedelta(minutes=5),
            )
            return
        except (ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError) as exc:
            await db.rollback()
            reason = type(exc).__name__
    audit.record(
        db,
        AuditContext(user.id, user.email, ctx.ip_address, ctx.request_id),
        "onboarding.custom_role_not_applied",
        target=user.email,
        details={"role_template_id": str(invitation.role_template_id), "reason": reason},
    )
    await db.commit()


async def validate_org_placement(db: AsyncSession, position_id: uuid.UUID | None, reports_to_id: uuid.UUID | None) -> None:
    """An invitation may name an active position and an active manager; neither grants access."""
    if position_id is not None:
        position = await db.get(Position, position_id)
        if position is None or not position.is_active:
            raise InvalidInputError("Choose an active position.")
    if reports_to_id is not None:
        manager = await db.get(User, reports_to_id)
        if manager is None or not manager.is_active:
            raise InvalidInputError("The manager must be an active account.")


async def apply_invited_org_placement(db: AsyncSession, invitation: UserInvitation, user: User) -> dict:
    """Give the new account the invitation's position and manager. Anything that changed since the invitation
    (an archived position, an inactive manager) is skipped and reported in the audit details, not an error."""
    details: dict = {}
    position = await db.get(Position, invitation.position_id) if invitation.position_id else None
    if position is not None and not position.is_active:
        details["position_skipped"] = "archived"
        position = None
    manager = await db.get(User, invitation.reports_to_id) if invitation.reports_to_id else None
    if manager is not None and not manager.is_active:
        details["manager_skipped"] = "inactive"
        manager = None
    if position is None and manager is None:
        return details
    user.position_id = position.id if position else None
    user.reports_to_id = manager.id if manager else None
    db.add(OrgAssignmentHistory(
        user_id=user.id, position_id=user.position_id, position_title=position.title if position else None,
        reports_to_id=user.reports_to_id, reports_to_email=manager.email if manager else None,
        changed_by=invitation.created_by,
    ))
    details.update({"position": position.title if position else None, "reports_to": manager.email if manager else None})
    return details
