"""Enterprise SSO (OIDC / SAML 2.0) and SCIM 2.0 provisioning services."""

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, InvalidInputError, InvalidLoginError, NotFoundError, PermissionDeniedError
from app.core.security import hash_password, hash_session_token, new_session_token
from app.db.models import GROUP_DIRECTORY, ROLE_ADMIN, AccessGroup, AccessGroupMember, User, UserSession
from app.services import audit
from app.services.audit import AuditContext
from app.schemas.sso import (
    ScimEmail,
    ScimGroupCreate,
    ScimGroupListResponse,
    ScimGroupMember,
    ScimGroupResponse,
    ScimListResponse,
    ScimName,
    ScimPatchRequest,
    ScimUserCreate,
    ScimUserResponse,
    SsoConfigResponse,
)


# The legacy implementation decoded claims/XML without verifying signatures,
# issuer, audience, nonce, request binding or assertion replay. Fail closed until
# a supported verifier and IdP integration have passed end-to-end acceptance.
SSO_UNAVAILABLE_REASON = "SSO sign-in is disabled pending a validated identity-provider integration. Use local sign-in."


def get_sso_authorization_url(settings: Settings, state: str | None = None) -> str:
    return ""


async def process_oidc_callback(
    code: str, db: AsyncSession, settings: Settings, *,
    id_token: str | None = None, client: httpx.AsyncClient | None = None,
    audit_ctx: AuditContext | None = None,
) -> tuple[User, str]:
    if audit_ctx is not None:
        audit.record(db, audit_ctx, "auth.sso_rejected", outcome="failure", details={"protocol": "oidc", "reason": "verifier_unavailable"})
        await db.commit()
    raise InvalidLoginError(SSO_UNAVAILABLE_REASON)


async def process_saml_assertion(
    saml_response_b64: str, db: AsyncSession, settings: Settings, *,
    audit_ctx: AuditContext | None = None,
) -> tuple[User, str]:
    if audit_ctx is not None:
        audit.record(db, audit_ctx, "auth.sso_rejected", outcome="failure", details={"protocol": "saml", "reason": "verifier_unavailable"})
        await db.commit()
    raise InvalidLoginError(SSO_UNAVAILABLE_REASON)


# ---------------------------------------------------------------------------
# SCIM 2.0 User Provisioning
# ---------------------------------------------------------------------------


def _user_to_scim(user: User) -> ScimUserResponse:
    return ScimUserResponse(
        id=str(user.id),
        userName=user.email,
        externalId=user.external_id,
        name=ScimName(formatted=user.email, givenName=user.email.split("@")[0]),
        emails=[ScimEmail(value=user.email, primary=True)],
        active=user.is_active,
        roles=[user.role],
        meta={
            "resourceType": "User",
            "created": user.created_at.isoformat() if hasattr(user, "created_at") and user.created_at else "",
            "location": f"/api/scim/v2/Users/{user.id}",
        },
    )


_SCIM_FILTER_RE = re.compile(
    r'^\s*(?P<attribute>userName|emails\.value|externalId|active)\s+eq\s+'
    r'(?P<value>"(?:[^"\\]|\\.)*"|true|false)\s*$',
    re.IGNORECASE,
)
_SCIM_MAX_RESULTS = 100
_SCIM_GROUP_MEMBER_MAX_RESULTS = 1000
_SCIM_GROUP_FILTER_RE = re.compile(
    r'^\s*(?P<attribute>displayName|externalId)\s+eq\s+'
    r'(?P<value>"(?:[^"\\]|\\.)*")\s*$',
    re.IGNORECASE,
)


def _scim_filter_condition(filter_expr: str | None):
    if filter_expr is None:
        return None
    match = _SCIM_FILTER_RE.fullmatch(filter_expr)
    if match is None:
        raise InvalidInputError("The SCIM filter is not supported.")

    attribute = match.group("attribute").casefold()
    raw_value = match.group("value")
    if attribute == "active":
        if raw_value.casefold() not in {"true", "false"}:
            raise InvalidInputError("The SCIM filter is not supported.")
        return User.is_active.is_(raw_value.casefold() == "true")

    if not raw_value.startswith('"'):
        raise InvalidInputError("The SCIM filter is not supported.")
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError:
        raise InvalidInputError("The SCIM filter is not supported.") from None
    if not isinstance(value, str):
        raise InvalidInputError("The SCIM filter is not supported.")

    if attribute in {"username", "emails.value"}:
        return func.lower(User.email) == value.strip().lower()
    return User.external_id == value.strip()


def _normalized_scim_external_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise InvalidInputError("SCIM externalId cannot be empty.")
    if len(normalized) > 256:
        raise InvalidInputError("SCIM externalId exceeds the supported length.")
    return normalized


async def _ensure_scim_external_id_available(
    db: AsyncSession,
    external_id: str | None,
    *,
    exclude_user_id: uuid.UUID | None = None,
) -> None:
    if external_id is None:
        return
    query = select(User.id).where(User.external_id == external_id)
    if exclude_user_id is not None:
        query = query.where(User.id != exclude_user_id)
    existing_id = (await db.execute(query)).scalar_one_or_none()
    if existing_id is not None:
        raise ConflictError("SCIM externalId is already assigned.")


async def scim_list_users(
    db: AsyncSession,
    start_index: int = 1,
    count: int = 100,
    filter_expr: str | None = None,
) -> ScimListResponse:
    """List users according to SCIM 2.0 specification."""
    if start_index < 1 or count < 0:
        raise InvalidInputError("SCIM pagination values are out of range.")
    query = select(User)
    count_query = select(func.count(User.id))
    filter_condition = _scim_filter_condition(filter_expr)
    if filter_condition is not None:
        query = query.where(filter_condition)
        count_query = count_query.where(filter_condition)

    total_results = (await db.execute(count_query)).scalar_one()

    offset = max(0, start_index - 1)
    query = query.order_by(User.email).offset(offset).limit(min(count, _SCIM_MAX_RESULTS))
    users = (await db.execute(query)).scalars().all()

    resources = [_user_to_scim(u) for u in users]
    return ScimListResponse(
        totalResults=total_results,
        startIndex=start_index,
        itemsPerPage=len(resources),
        Resources=resources,
    )


async def scim_get_user(db: AsyncSession, user_id: uuid.UUID) -> ScimUserResponse:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")
    return _user_to_scim(user)


async def scim_create_user(
    db: AsyncSession,
    payload: ScimUserCreate,
    settings: Settings,
) -> ScimUserResponse:
    email = payload.userName.strip().lower()
    external_id = _normalized_scim_external_id(payload.externalId)
    existing = (await db.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"User {email} already exists.")
    await _ensure_scim_external_id_available(db, external_id)

    assigned_role = settings.sso_default_role
    for r in payload.roles:
        if r.lower() in ("admin", "manager", "employee"):
            assigned_role = r.lower()
            break

    unusable_pw_hash = hash_password(str(uuid.uuid4()) + str(uuid.uuid4()))
    now = datetime.now(UTC)
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=unusable_pw_hash,
        role=assigned_role,
        is_active=payload.active,
        external_id=external_id,
        created_at=now,
    )
    db.add(user)
    audit.record(
        db,
        AuditContext(None, "SCIM directory", None, None),
        "user.scim_created",
        target=str(user.id),
        details={"role": user.role, "active": user.is_active},
    )
    await db.commit()
    await db.refresh(user)
    return _user_to_scim(user)


async def scim_patch_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    payload: ScimPatchRequest,
    *,
    settings: Settings | None = None,
) -> ScimUserResponse:
    needs_offboarding_lock = any(
        operation.op.casefold() in {"add", "replace"}
        and (
            (operation.path or "").casefold() == "active" and operation.value is False
            or isinstance(operation.value, dict)
            and any(str(key).casefold() == "active" and value is False for key, value in operation.value.items())
        )
        for operation in payload.Operations
    )
    if needs_offboarding_lock:
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 7_713_221_005})
        user = (
            await db.execute(select(User).where(User.id == user_id).with_for_update())
        ).scalar_one_or_none()
    else:
        user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")

    requested_active = user.is_active
    changed_fields: set[str] = set()
    for op in payload.Operations:
        op_type = op.op.lower()
        if op_type in ("replace", "add"):
            value_map = {str(key).casefold(): value for key, value in op.value.items()} if isinstance(op.value, dict) else {}
            path = (op.path or "").casefold()
            if path == "active" or "active" in value_map:
                val = op.value if path == "active" else value_map["active"]
                if isinstance(val, bool):
                    requested_active = val
            elif path == "roles" or "roles" in value_map:
                roles_val = op.value if path == "roles" else value_map["roles"]
                if isinstance(roles_val, list) and roles_val:
                    for r in roles_val:
                        candidate = r if isinstance(r, str) else r.get("value", "")
                        if candidate.lower() in ("admin", "manager", "employee"):
                            if user.role != candidate.lower():
                                changed_fields.add("role")
                            user.role = candidate.lower()
                            break
            elif path == "externalid" or "externalid" in value_map:
                raw_external_id = op.value if path == "externalid" else value_map["externalid"]
                external_id = _normalized_scim_external_id(raw_external_id)
                await _ensure_scim_external_id_available(db, external_id, exclude_user_id=user.id)
                if user.external_id != external_id:
                    changed_fields.add("externalId")
                user.external_id = external_id
        elif op_type == "remove" and (op.path or "").casefold() == "externalid":
            if user.external_id is not None:
                changed_fields.add("externalId")
            user.external_id = None

    if requested_active != user.is_active:
        changed_fields.add("active")
    if changed_fields:
        audit.record(
            db,
            AuditContext(None, "SCIM directory", None, None),
            "user.scim_updated",
            target=str(user.id),
            details={"fields": sorted(changed_fields)},
        )
    if requested_active is False and user.is_active:
        await _scim_deprovision_locked(db, user)
    else:
        user.is_active = requested_active
        await db.commit()
        await db.refresh(user)
    return _user_to_scim(user)


async def _scim_deprovision_locked(db: AsyncSession, user: User) -> None:
    if not user.is_active:
        return

    # SCIM deprovisioning must have the same immediate session and permission revocation as
    # Admin offboarding. Lock the active administrator rows so concurrent directory events
    # cannot deactivate every administrator based on the same stale count.
    active_admins = list(
        (
            await db.execute(
                select(User)
                .where(User.role == ROLE_ADMIN, User.is_active.is_(True))
                .order_by(User.id)
                .with_for_update()
            )
        ).scalars()
    )
    successor = next((admin for admin in active_admins if admin.id != user.id), None)
    if successor is None:
        raise PermissionDeniedError(
            "SCIM cannot deactivate the last active administrator or proceed without an active administrator."
        )

    # The audit event records the configured directory as actor. The grant schema also keeps
    # its existing required Admin-user revoker reference, for which the selected successor is used.
    from app.services.users import UserAdminService

    service = UserAdminService(
        db,
        successor,
        AuditContext(actor_user_id=None, actor_email="SCIM directory", ip_address=None, request_id=None),
    )
    await service.set_active(user.id, False, knowledge_transfer_to=successor.id)


async def scim_delete_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    # Share the same transaction lock as Admin-initiated account offboarding.
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 7_713_221_005})
    user = (
        await db.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")
    await _scim_deprovision_locked(db, user)


def _scim_group_filter_condition(filter_expr: str | None):
    if filter_expr is None:
        return None
    match = _SCIM_GROUP_FILTER_RE.fullmatch(filter_expr)
    if match is None:
        raise InvalidInputError("The SCIM group filter is not supported.")
    try:
        value = json.loads(match.group("value"))
    except json.JSONDecodeError:
        raise InvalidInputError("The SCIM group filter is not supported.") from None
    if not isinstance(value, str):
        raise InvalidInputError("The SCIM group filter is not supported.")
    attribute = match.group("attribute").casefold()
    column = AccessGroup.name if attribute == "displayname" else AccessGroup.external_id
    return func.lower(column) == value.strip().lower()


async def _scim_group_member_ids(db: AsyncSession, values: list[Any]) -> set[uuid.UUID]:
    if len(values) > _SCIM_GROUP_MEMBER_MAX_RESULTS:
        raise InvalidInputError("A SCIM group cannot contain more than 1,000 members in one request.")
    user_ids: set[uuid.UUID] = set()
    for value in values:
        try:
            member = value if isinstance(value, ScimGroupMember) else ScimGroupMember.model_validate(value)
            if member.type.casefold() != "user":
                raise ValueError
            user_ids.add(uuid.UUID(member.value))
        except (ValidationError, ValueError, TypeError):
            raise InvalidInputError("SCIM group members must reference valid InfraSpine User IDs.") from None
    if not user_ids:
        return set()
    known = set((await db.execute(select(User.id).where(User.id.in_(user_ids)))).scalars())
    if known != user_ids:
        raise InvalidInputError("One or more SCIM group members do not exist.")
    return user_ids


async def _scim_group_to_response(db: AsyncSession, group: AccessGroup) -> ScimGroupResponse:
    users = list(
        (
            await db.execute(
                select(User)
                .join(AccessGroupMember, AccessGroupMember.user_id == User.id)
                .where(AccessGroupMember.group_id == group.id)
                .order_by(User.email)
                .limit(_SCIM_GROUP_MEMBER_MAX_RESULTS + 1)
            )
        ).scalars()
    )
    if len(users) > _SCIM_GROUP_MEMBER_MAX_RESULTS:
        raise InvalidInputError("This directory group exceeds the supported 1,000-member SCIM response limit.")
    return ScimGroupResponse(
        id=str(group.id),
        externalId=group.external_id or "",
        displayName=group.name,
        members=[ScimGroupMember(value=str(user.id), display=user.email, type="User") for user in users],
        meta={
            "resourceType": "Group",
            "created": group.created_at.isoformat() if group.created_at else "",
            "location": f"/api/scim/v2/Groups/{group.id}",
        },
    )


async def scim_list_groups(
    db: AsyncSession,
    start_index: int = 1,
    count: int = 100,
    filter_expr: str | None = None,
) -> ScimGroupListResponse:
    if start_index < 1 or count < 0:
        raise InvalidInputError("SCIM pagination values are out of range.")
    filters = [AccessGroup.source == GROUP_DIRECTORY, AccessGroup.lifecycle_status == "active"]
    filter_condition = _scim_group_filter_condition(filter_expr)
    if filter_condition is not None:
        filters.append(filter_condition)
    total = (await db.execute(select(func.count(AccessGroup.id)).where(*filters))).scalar_one()
    groups = list(
        (
            await db.execute(
                select(AccessGroup)
                .where(*filters)
                .order_by(func.lower(AccessGroup.name), AccessGroup.id)
                .offset(start_index - 1)
                .limit(min(count, _SCIM_MAX_RESULTS))
            )
        ).scalars()
    )
    resources = [await _scim_group_to_response(db, group) for group in groups]
    return ScimGroupListResponse(
        totalResults=total,
        startIndex=start_index,
        itemsPerPage=len(resources),
        Resources=resources,
    )


async def scim_get_group(db: AsyncSession, group_id: uuid.UUID) -> ScimGroupResponse:
    group = (
        await db.execute(
            select(AccessGroup).where(
                AccessGroup.id == group_id,
                AccessGroup.source == GROUP_DIRECTORY,
                AccessGroup.lifecycle_status == "active",
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise NotFoundError("SCIM group not found.")
    return await _scim_group_to_response(db, group)


async def scim_create_group(db: AsyncSession, payload: ScimGroupCreate) -> ScimGroupResponse:
    external_id = _normalized_scim_external_id(payload.externalId)
    display_name = payload.displayName.strip()
    if not display_name:
        raise InvalidInputError("SCIM group displayName cannot be empty.")
    member_ids = await _scim_group_member_ids(db, payload.members)
    existing = (
        await db.execute(select(AccessGroup).where(AccessGroup.external_id == external_id))
    ).scalar_one_or_none()
    if existing is not None and existing.lifecycle_status == "active":
        raise ConflictError("A directory group with this externalId already exists.")

    from app.services.source_acl import sync_directory_group

    group = await sync_directory_group(
        db,
        external_id,
        display_name,
        "custom",
        [],
        AuditContext(None, "SCIM directory", None, None),
        member_user_ids=member_ids,
    )
    return await _scim_group_to_response(db, group)


_SCIM_MEMBER_FILTER_RE = re.compile(r'^members\[value\s+eq\s+"(?P<value>[^"]+)"\]$', re.IGNORECASE)


async def scim_patch_group(
    db: AsyncSession, group_id: uuid.UUID, payload: ScimPatchRequest
) -> ScimGroupResponse:
    group = (
        await db.execute(
            select(AccessGroup)
            .where(
                AccessGroup.id == group_id,
                AccessGroup.source == GROUP_DIRECTORY,
                AccessGroup.lifecycle_status == "active",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if group is None:
        raise NotFoundError("SCIM group not found.")

    display_name = group.name
    member_ids = set(
        (
            await db.execute(
                select(AccessGroupMember.user_id).where(AccessGroupMember.group_id == group.id)
            )
        ).scalars()
    )
    for operation in payload.Operations:
        op_type = operation.op.casefold()
        if op_type not in {"add", "replace", "remove"}:
            raise InvalidInputError("The SCIM group patch operation is not supported.")
        path = (operation.path or "").casefold()
        value_map = {str(key).casefold(): value for key, value in operation.value.items()} if isinstance(operation.value, dict) else {}
        if not path and value_map:
            if "displayname" in value_map:
                if op_type == "remove" or not isinstance(value_map["displayname"], str) or not value_map["displayname"].strip():
                    raise InvalidInputError("SCIM group displayName cannot be removed or empty.")
                display_name = value_map["displayname"].strip()
            if "members" in value_map:
                raw_members = value_map["members"] or []
                if not isinstance(raw_members, list):
                    raw_members = [raw_members]
                value_ids = await _scim_group_member_ids(db, raw_members)
                if op_type == "replace":
                    member_ids = value_ids
                elif op_type == "add":
                    member_ids |= value_ids
                else:
                    member_ids -= value_ids
            if "externalid" in value_map and value_map["externalid"] != group.external_id:
                raise InvalidInputError("A directory group's externalId is immutable after creation.")
            if set(value_map) - {"displayname", "members", "externalid"}:
                raise InvalidInputError("The SCIM group patch contains an unsupported attribute.")
            continue
        if path == "displayname":
            if op_type == "remove" or not isinstance(operation.value, str) or not operation.value.strip():
                raise InvalidInputError("SCIM group displayName cannot be removed or empty.")
            display_name = operation.value.strip()
        elif path == "members":
            if op_type == "remove":
                member_ids.clear()
            else:
                values = operation.value if isinstance(operation.value, list) else [operation.value]
                value_ids = await _scim_group_member_ids(db, values)
                member_ids = value_ids if op_type == "replace" else member_ids | value_ids
        elif path.startswith("members[") and op_type == "remove":
            match = _SCIM_MEMBER_FILTER_RE.fullmatch(path)
            if match is None:
                raise InvalidInputError("The SCIM group member filter is not supported.")
            try:
                member_ids.discard(uuid.UUID(match.group("value")))
            except ValueError:
                raise InvalidInputError("The SCIM group member filter is not supported.") from None
        elif path == "externalid":
            if op_type == "remove" or operation.value != group.external_id:
                raise InvalidInputError("A directory group's externalId is immutable after creation.")
        else:
            raise InvalidInputError("The SCIM group patch path is not supported.")
        if len(member_ids) > _SCIM_GROUP_MEMBER_MAX_RESULTS:
            raise InvalidInputError("A SCIM group cannot contain more than 1,000 members.")

    from app.services.source_acl import sync_directory_group

    updated = await sync_directory_group(
        db,
        group.external_id or "",
        display_name,
        "custom",
        [],
        AuditContext(None, "SCIM directory", None, None),
        member_user_ids=member_ids,
    )
    return await _scim_group_to_response(db, updated)


async def scim_delete_group(db: AsyncSession, group_id: uuid.UUID) -> None:
    group = (
        await db.execute(
            select(AccessGroup)
            .where(
                AccessGroup.id == group_id,
                AccessGroup.source == GROUP_DIRECTORY,
                AccessGroup.lifecycle_status == "active",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if group is None:
        raise NotFoundError("SCIM group not found.")
    result = await db.execute(delete(AccessGroupMember).where(AccessGroupMember.group_id == group.id))
    group.lifecycle_status = "archived"
    audit.record(
        db,
        AuditContext(None, "SCIM directory", None, None),
        "access.directory_group_deprovisioned",
        target=str(group.id),
        details={"members_removed": max(0, result.rowcount or 0)},
    )
    await db.commit()
