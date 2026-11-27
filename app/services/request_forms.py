"""Custom request forms: field definitions, answer validation, and the administrator service.

Field types: text (one line), long_text, number, date (YYYY-MM-DD), choice (one of the options), yes_no and
file (uploaded attachments). A required yes_no field must be answered "yes" (a confirmation). A field can be
shown only when an earlier choice or yes/no field has a given answer (`show_if`); a hidden field is never
required and its answer is not kept. Answers are stored on the request as [{key, label, type, value}] with the
labels of the version submitted, so later edits never rewrite history.
"""

from __future__ import annotations

import math
import re
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.db.form_models import FIELD_TYPES, RequestForm
from app.db.models import ROLE_ADMIN, AccessGroup, AccessGroupMember, DepartmentManager, User
from app.db.org_models import Position
from app.services import audit
from app.services.audit import AuditContext

MAX_FIELDS = 30
MAX_OPTIONS = 50
_LIMITS = {"text": 500, "long_text": 4000}


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or "field"


def normalize_fields(raw: list[dict]) -> list[dict]:
    """Validate an administrator's field list and give every field a stable, unique key."""
    if not raw:
        raise InvalidInputError("Add at least one field to the form.")
    if len(raw) > MAX_FIELDS:
        raise InvalidInputError(f"A form can have at most {MAX_FIELDS} fields.")
    fields, keys = [], set()
    for item in raw:
        label = " ".join(str(item.get("label") or "").split())
        kind = item.get("type")
        if not 1 <= len(label) <= 120:
            raise InvalidInputError("Every field needs a label of up to 120 characters.")
        if kind not in FIELD_TYPES:
            raise InvalidInputError(f"Unknown field type for “{label}”.")
        key = _slug(str(item.get("key") or label))
        base, n = key, 2
        while key in keys:
            key, n = f"{base}_{n}", n + 1
        keys.add(key)
        options: list[str] = []
        if kind == "choice":
            options = list(dict.fromkeys(" ".join(str(o).split()) for o in item.get("options") or [] if str(o).strip()))
            if not 2 <= len(options) <= MAX_OPTIONS or any(len(o) > 100 for o in options):
                raise InvalidInputError(f"“{label}” needs 2 to {MAX_OPTIONS} options of up to 100 characters.")
        help_text = " ".join(str(item.get("help") or "").split())[:200] or None
        show_if = None
        if item.get("show_if"):
            reference = str(item["show_if"].get("field") or "")
            parent = next((f for f in fields if f["key"] == reference), None) or next(
                (f for f in fields if _slug(f["label"]) == _slug(reference)), None)
            if parent is None:
                raise InvalidInputError(f"“{label}” can only depend on a field above it.")
            if parent["type"] not in ("choice", "yes_no"):
                raise InvalidInputError(f"“{label}” can only depend on a choice or yes/no field.")
            equals = " ".join(str(item["show_if"].get("equals") or "").split())
            allowed = parent["options"] if parent["type"] == "choice" else ["Yes", "No"]
            if equals not in allowed:
                raise InvalidInputError(f"Pick one of “{parent['label']}”'s answers for when to show “{label}”.")
            show_if = {"field": parent["key"], "equals": equals}
        fields.append({"key": key, "label": label, "type": kind, "required": bool(item.get("required")), "options": options,
                       "help": help_text, "show_if": show_if})
    return fields


def _visible(field: dict, given: dict[str, str]) -> bool:
    """Shown when it has no condition, or its parent is shown and has the expected answer."""
    rule = field.get("show_if")
    return rule is None or given.get(rule["field"]) == rule["equals"]


def validate_answers(fields: list[dict], answers: dict) -> list[dict]:
    """Check answers against the form; return them as [{key, label, type, value}] in field order."""
    unknown = set(answers) - {f["key"] for f in fields}
    if unknown:
        raise InvalidInputError("The form has changed; reload it and try again.")
    out = []
    given: dict[str, str] = {}  # answers of shown choice and yes/no fields, for conditions further down
    for field in fields:
        if not _visible(field, given):
            continue  # hidden: not required, and any answer sent for it is dropped
        label, kind, raw = field["label"], field["type"], answers.get(field["key"])
        empty = raw is None or (isinstance(raw, str) and not raw.strip())
        if kind == "yes_no":
            value = raw if isinstance(raw, bool) else None if empty else str(raw).lower() in ("true", "yes", "1")
            if field["required"] and value is not True:
                raise InvalidInputError(f"Confirm “{label}”.")
            given[field["key"]] = "Yes" if value else "No"
            out.append({"key": field["key"], "label": label, "type": kind, "value": given[field["key"]]})
            continue
        if kind == "file":
            ids = [str(i) for i in raw] if isinstance(raw, list) else [i for i in str(raw or "").split(",") if i.strip()]
            if field["required"] and not ids:
                raise InvalidInputError(f"Attach a file for “{label}”.")
            if ids:
                out.append({"key": field["key"], "label": label, "type": kind, "value": [i.strip() for i in ids]})
            continue
        if empty:
            if field["required"]:
                raise InvalidInputError(f"Fill in “{label}”.")
            continue
        text_value = " ".join(str(raw).split()) if kind != "long_text" else str(raw).replace("\r\n", "\n").strip()
        if kind in _LIMITS:
            if len(text_value) > _LIMITS[kind]:
                raise InvalidInputError(f"“{label}” can be at most {_LIMITS[kind]} characters.")
        elif kind == "number":
            try:
                number = float(text_value.replace(",", ""))
            except ValueError:
                raise InvalidInputError(f"“{label}” must be a number.") from None
            if not math.isfinite(number) or abs(number) > 1e13:
                raise InvalidInputError(f"“{label}” must be a number.")
            text_value = f"{number:,.2f}".rstrip("0").rstrip(".") if number != int(number) else f"{int(number):,}"
        elif kind == "date":
            try:
                text_value = date.fromisoformat(text_value).isoformat()
            except ValueError:
                raise InvalidInputError(f"“{label}” must be a date.") from None
        elif kind == "choice":
            if text_value not in field["options"]:
                raise InvalidInputError(f"Choose one of the options for “{label}”.")
            given[field["key"]] = text_value
        out.append({"key": field["key"], "label": label, "type": kind, "value": text_value})
    return out


def answers_as_text(answers: list[dict]) -> str:
    return "\n".join(f"{a['label']}: {a['value']}" for a in answers)[:8000]


def _audience_ids(form: RequestForm, kind: str) -> list[uuid.UUID]:
    return [uuid.UUID(i) for i in (form.audience or {}).get(kind, [])]


async def can_use_form(db: AsyncSession, user: User, form: RequestForm) -> bool:
    """Everyone may use a form with no audience; otherwise listed departments' members and managers and
    listed positions' holders. Administrators always may."""
    departments, positions = _audience_ids(form, "departments"), _audience_ids(form, "positions")
    if user.role == ROLE_ADMIN or (not departments and not positions):
        return True
    if user.position_id is not None and user.position_id in positions:
        return True
    if departments:
        member = await db.scalar(select(AccessGroupMember.group_id).where(
            AccessGroupMember.user_id == user.id, AccessGroupMember.group_id.in_(departments)).limit(1))
        if member is not None:
            return True
        managed = await db.scalar(select(DepartmentManager.group_id).where(
            DepartmentManager.manager_user_id == user.id, DepartmentManager.group_id.in_(departments)).limit(1))
        if managed is not None:
            return True
    return False


async def audience_names(db: AsyncSession, forms: list[RequestForm]) -> dict[str, str]:
    """Display names for every department and position in these forms' audiences."""
    ids = {i for form in forms for kind in ("departments", "positions") for i in _audience_ids(form, kind)}
    if not ids:
        return {}
    names = {str(g.id): g.name for g in await db.scalars(select(AccessGroup).where(AccessGroup.id.in_(ids)))}
    names.update({str(p.id): p.title for p in await db.scalars(select(Position).where(Position.id.in_(ids)))})
    return names


class RequestFormService:
    def __init__(self, db: AsyncSession, admin: User, ctx: AuditContext) -> None:
        self.db = db
        self.admin = admin
        self.ctx = ctx

    async def list(self, *, include_archived: bool = False) -> list[tuple[RequestForm, str | None]]:
        query = select(RequestForm, AccessGroup.name).outerjoin(AccessGroup, AccessGroup.id == RequestForm.department_group_id)
        if not include_archived:
            query = query.where(RequestForm.is_active.is_(True))
        return [(f, name) for f, name in (await self.db.execute(query.order_by(RequestForm.is_active.desc(), RequestForm.name))).all()]

    async def _department(self, group_id: uuid.UUID | None) -> uuid.UUID | None:
        if group_id is None:
            return None
        group = await self.db.get(AccessGroup, group_id)
        if group is None or group.kind != "department":
            raise InvalidInputError("Choose a department for this form.")
        return group.id

    async def _audience(self, department_ids: list[uuid.UUID], position_ids: list[uuid.UUID]) -> dict:
        departments = list(dict.fromkeys(department_ids))
        positions = list(dict.fromkeys(position_ids))
        if departments:
            found = set(await self.db.scalars(select(AccessGroup.id).where(AccessGroup.id.in_(departments), AccessGroup.kind == "department")))
            if found != set(departments):
                raise InvalidInputError("Choose existing departments for who may use this form.")
        if positions:
            found = set(await self.db.scalars(select(Position.id).where(Position.id.in_(positions), Position.is_active.is_(True))))
            if found != set(positions):
                raise InvalidInputError("Choose active positions for who may use this form.")
        return {"departments": [str(i) for i in departments], "positions": [str(i) for i in positions]}

    async def save(self, form_id: uuid.UUID | None, *, name: str, description: str | None, category: str,
                   department_group_id: uuid.UUID | None, fields: list[dict],
                   audience_department_ids: list[uuid.UUID] | None = None, audience_position_ids: list[uuid.UUID] | None = None) -> RequestForm:
        clean_name = " ".join(name.split())
        normalized = normalize_fields(fields)
        department = await self._department(department_group_id)
        audience = await self._audience(audience_department_ids or [], audience_position_ids or [])
        if form_id is None:
            form = RequestForm(created_by=self.admin.id)
            self.db.add(form)
            action = "request_form.created"
        else:
            form = await self.db.get(RequestForm, form_id, with_for_update=True)
            if form is None or not form.is_active:
                raise NotFoundError("Form not found.")
            if form.fields != normalized or form.category != category:
                form.version += 1
            action = "request_form.changed"
        form.name, form.category, form.department_group_id, form.fields = clean_name, category, department, normalized
        form.audience = audience
        form.description = " ".join((description or "").split())[:500] or None
        form.updated_at = datetime.now(UTC)
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("Another active form already has this name.") from None
        audit.record(self.db, self.ctx, action, target=form.name, details={
            "category": category, "fields": len(normalized), "version": form.version,
            "audience": {k: len(v) for k, v in audience.items()},
        })
        await self.db.commit()
        return form

    async def archive(self, form_id: uuid.UUID) -> None:
        form = await self.db.get(RequestForm, form_id, with_for_update=True)
        if form is None or not form.is_active:
            raise NotFoundError("Form not found.")
        form.is_active, form.updated_at = False, datetime.now(UTC)  # requests keep their answers
        audit.record(self.db, self.ctx, "request_form.archived", target=form.name)
        await self.db.commit()
