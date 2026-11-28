"""Step AM (Self-Evolving Software Foundry), slice 6 — registry service.

Async service functions behind the Foundry API routes. Every function
authorises through `app/platform` first: only scope members see an app
(unknown scope / non-member → 404, member without the permission → 403),
exactly like every other module route.

Permissions:
* `foundry.read` — list apps, view one app's detail.
* `foundry.manage` — record a build, request a deploy, submit feedback.
* `foundry.deploy` — the permission the *decider* of a deploy approval
  must hold (set as the approval's required_permission, slice 4).

Deploy requests go through the platform approval flow: the route builds
the exact `ActionSpec` (slice 4) and `request_approval()` stores it; the
deploy itself runs only after a different person approves that exact
action and the record is validated by slice 4.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.foundry.deploy import (
    DEPLOY_ACTION_TYPE,
    DEPLOY_PERMISSION,
    plan_deploy,
)
from app.foundry.feedback import (
    FeedbackChange,
    FeedbackItem,
    revise_from_feedback,
    validate_feedback_item,
)
from app.foundry.models import FoundryApp, FoundryBuild, FoundryFeedback
from app.foundry.registry import BuildRecord
from app.foundry.spec import spec_fingerprint, validate_spec
from app.platform.access import Principal, require, visible_scopes
from app.platform.approvals import ActionSpec, request_approval
from app.platform.events import audit
from app.platform.models import PlatformScope

__all__ = [
    "FOUNDRY_DEPLOY_PERMISSION",
    "FOUNDRY_MANAGE",
    "FOUNDRY_READ",
    "get_app_detail",
    "list_apps",
    "list_scopes",
    "record_build",
    "request_deploy",
    "submit_feedback",
]

FOUNDRY_READ = "foundry.read"
FOUNDRY_MANAGE = "foundry.manage"
FOUNDRY_DEPLOY_PERMISSION = DEPLOY_PERMISSION

# Deploy targets are an owner decision (plan §5, decision 1). Until the
# owner configures targets, the allow-list is empty and every deploy
# request is refused with `no_deploy_target_configured`. Pass the
# configured allow-list in from settings at the call site.
DEFAULT_ALLOWED_TARGETS: tuple[str, ...] = ()


def _build_out(row: FoundryBuild) -> dict:
    return {
        "id": str(row.id),
        "spec_version": row.spec_version,
        "spec_fingerprint": row.spec_fingerprint,
        "build_fingerprint": row.build_fingerprint,
        "gate": row.gate,
        "preview_token": row.preview_token,
        "preview_expires_in_seconds": row.preview_expires_in_seconds,
        "health": row.health,
        "tests_passed": row.tests_passed,
        "tests_failed": row.tests_failed,
        "deploy_state": row.deploy_state,
        "deploy_target": row.deploy_target,
        "approval_id": str(row.approval_id) if row.approval_id else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _feedback_out(row: FoundryFeedback) -> dict:
    return {
        "id": str(row.id),
        "build_fingerprint": row.build_fingerprint,
        "kind": row.kind,
        "note": row.note,
        "changes": row.changes,
        "author_id": str(row.author_id) if row.author_id else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _app_row(db: AsyncSession, scope_id: uuid.UUID, slug: str) -> FoundryApp:
    row = await db.scalar(select(FoundryApp).where(
        FoundryApp.scope_id == scope_id, FoundryApp.slug == slug))
    if row is None:
        raise NotFoundError("Foundry app not found.")
    return row


async def list_scopes(db: AsyncSession, p: Principal) -> list[dict]:
    """Scopes where the caller holds `foundry.read` (granted scopes and
    their descendants). The frontend uses this to pick a scope without
    having to know the org scope id."""
    visible = await visible_scopes(db, p, FOUNDRY_READ)
    if not visible.scope_ids:
        return []
    rows = (await db.scalars(select(PlatformScope).where(
        PlatformScope.id.in_(sorted(visible.scope_ids))).order_by(PlatformScope.name))).all()
    return [{"id": str(row.id), "name": row.name, "kind": row.kind} for row in rows]


async def list_apps(db: AsyncSession, p: Principal, scope_id: uuid.UUID) -> list[dict]:
    """All registered apps in this scope, newest build first per app."""
    await require(db, p, FOUNDRY_READ, scope_id)
    apps = (await db.scalars(select(FoundryApp).where(
        FoundryApp.scope_id == scope_id).order_by(FoundryApp.slug))).all()
    out = []
    for app in apps:
        builds = (await db.scalars(select(FoundryBuild).where(
            FoundryBuild.app_id == app.id).order_by(FoundryBuild.spec_version))).all()
        latest = builds[-1] if builds else None
        feedback_rows = (await db.scalars(select(FoundryFeedback).where(
            FoundryFeedback.app_id == app.id))).all()
        out.append({
            "slug": app.slug,
            "name": app.name,
            "builds": len(builds),
            "latest_version": latest.spec_version if latest else None,
            "latest_gate": latest.gate if latest else None,
            "latest_deploy_state": latest.deploy_state if latest else None,
            "feedback_count": len(feedback_rows),
        })
    return out


async def get_app_detail(db: AsyncSession, p: Principal, scope_id: uuid.UUID,
                         slug: str) -> dict:
    """One app: its builds (oldest first), its feedback, and its latest state."""
    await require(db, p, FOUNDRY_READ, scope_id)
    app = await _app_row(db, scope_id, slug)
    builds = (await db.scalars(select(FoundryBuild).where(
        FoundryBuild.app_id == app.id).order_by(FoundryBuild.spec_version))).all()
    feedback = (await db.scalars(select(FoundryFeedback).where(
        FoundryFeedback.app_id == app.id).order_by(FoundryFeedback.created_at))).all()
    return {
        "slug": app.slug,
        "name": app.name,
        "builds": [_build_out(b) for b in builds],
        "feedback": [_feedback_out(f) for f in feedback],
    }


async def record_build(db: AsyncSession, p: Principal, scope_id: uuid.UUID, slug: str,
                       app_name: str, build: BuildRecord, spec: dict) -> dict:
    """Record a build the pipeline produced (slices 1–3). The spec is
    re-validated here and the fingerprints must agree with it — a build
    row can never claim a spec it was not built from."""
    await require(db, p, FOUNDRY_MANAGE, scope_id)
    validation = validate_spec(spec)
    if not validation.valid:
        raise InvalidInputError("Spec invalid: "
                                + "; ".join(f"{i.code} ({i.field})" for i in validation.issues))
    if spec_fingerprint(spec) != build.spec_fingerprint:
        raise InvalidInputError("Spec fingerprint does not match the spec.")
    if build.slug != slug or build.app_name != app_name:
        raise InvalidInputError("Build slug/name do not match the app.")
    existing = await db.scalar(select(FoundryBuild).where(
        FoundryBuild.build_fingerprint == build.build_fingerprint))
    if existing is not None:
        raise ConflictError("This build is already registered.")
    app = await db.scalar(select(FoundryApp).where(
        FoundryApp.scope_id == scope_id, FoundryApp.slug == slug))
    if app is None:
        app = FoundryApp(scope_id=scope_id, slug=slug, name=app_name)
        db.add(app)
        await db.flush()
    elif app.name != app_name:
        raise ConflictError(f"App {slug} is already registered as {app.name!r}.")
    row = FoundryBuild(
        app_id=app.id, spec_version=build.spec_version,
        spec_fingerprint=build.spec_fingerprint,
        build_fingerprint=build.build_fingerprint, gate=build.gate,
        preview_token=build.preview_token,
        preview_expires_in_seconds=build.preview_expires_in_seconds,
        health=build.health, tests_passed=build.tests_passed,
        tests_failed=build.tests_failed, deploy_state=build.deploy_state,
        spec=spec, test_summary={"passed": build.tests_passed,
                                 "failed": build.tests_failed})
    db.add(row)
    audit(db, p, "foundry.build_recorded", target=slug,
          details={"build_fingerprint": build.build_fingerprint,
                   "gate": build.gate, "spec_version": build.spec_version})
    await db.commit()
    await db.refresh(row)
    return _build_out(row)


async def request_deploy(db: AsyncSession, p: Principal, scope_id: uuid.UUID,
                         slug: str, build_fingerprint: str, target: str,
                         *, allowed_targets: tuple[str, ...] = DEFAULT_ALLOWED_TARGETS,
                         ) -> dict:
    """Request a deploy approval for one recorded build. The build must be
    `preview_ready`; the approval is bound to the exact deploy action
    (slice 4). Returns the approval id + action hash the approver must see.
    No deploy happens here — only the approval request."""
    await require(db, p, FOUNDRY_MANAGE, scope_id)
    app = await _app_row(db, scope_id, slug)
    build = await db.scalar(select(FoundryBuild).where(
        FoundryBuild.app_id == app.id,
        FoundryBuild.build_fingerprint == build_fingerprint))
    if build is None:
        raise NotFoundError("Foundry build not found.")
    if build.gate != "preview_ready":
        raise ConflictError(f"Build gate is {build.gate}: approval can only be "
                            "requested for a preview_ready build.")
    previous_fingerprint = await _previous_build_fingerprint(db, app, build)
    try:
        plan = plan_deploy(slug=slug, build_fingerprint=build_fingerprint,
                           target=target,
                           previous_build_fingerprint=previous_fingerprint,
                           allowed_targets=allowed_targets)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    action = ActionSpec(DEPLOY_ACTION_TYPE, scope_id,
                        {"slug": slug, "build_fingerprint": build_fingerprint,
                         "target": target},
                        subject=slug)
    approval = await request_approval(db, p, action, required_permission=FOUNDRY_DEPLOY_PERMISSION)
    build.deploy_state = "requested"
    build.approval_id = approval.id
    build.deploy_target = target
    await db.flush()
    audit(db, p, "foundry.deploy_requested", target=slug,
          details={"build_fingerprint": build_fingerprint, "target": target,
                   "approval_id": str(approval.id), "action_hash": approval.action_hash})
    await db.commit()
    return {"approval_id": str(approval.id), "action_hash": approval.action_hash,
            "action_type": DEPLOY_ACTION_TYPE,
            "build_fingerprint": build_fingerprint, "target": target}


async def _previous_build_fingerprint(db: AsyncSession, app: FoundryApp,
                                      build: FoundryBuild) -> str | None:
    previous = await db.scalar(select(FoundryBuild).where(
        FoundryBuild.app_id == app.id,
        FoundryBuild.spec_version < build.spec_version).order_by(
            FoundryBuild.spec_version.desc()).limit(1))
    return previous.build_fingerprint if previous else None


async def submit_feedback(db: AsyncSession, p: Principal, scope_id: uuid.UUID,
                          slug: str, build_fingerprint: str, kind: str, note: str,
                          changes: list[dict[str, Any]]) -> dict:
    """Submit feedback on one recorded build. The note is stored for a
    human; structured changes produce a revision preview (slice 5) — the
    revision is NOT built or deployed here, it re-enters the pipeline."""
    await require(db, p, FOUNDRY_MANAGE, scope_id)
    app = await _app_row(db, scope_id, slug)
    build = await db.scalar(select(FoundryBuild).where(
        FoundryBuild.app_id == app.id,
        FoundryBuild.build_fingerprint == build_fingerprint))
    if build is None:
        raise NotFoundError("Foundry build not found.")
    parsed_changes = [_parse_change(c) for c in changes or []]
    item = FeedbackItem(slug=slug, build_fingerprint=build_fingerprint,
                        kind=kind, note=note or "",
                        changes=tuple(parsed_changes), author=str(p.user_id))
    reasons = validate_feedback_item(item, known_build_fingerprint=build_fingerprint)
    if reasons:
        raise InvalidInputError("Invalid feedback: " + "; ".join(reasons))
    row = FoundryFeedback(
        app_id=app.id, build_fingerprint=build_fingerprint, kind=kind,
        note=note or "", changes=[c.action for c in parsed_changes],
        author_id=p.user_id)
    db.add(row)
    revision_preview = None
    if parsed_changes:
        try:
            # The build fingerprint (file-set hash, slice 2) and the spec
            # fingerprint (slice 1) differ: the revision must be based on
            # the build's recorded spec fingerprint.
            revision = revise_from_feedback(build.spec or {}, [item], slug=slug,
                                            known_build_fingerprint=build_fingerprint,
                                            known_spec_fingerprint=build.spec_fingerprint)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        revision_preview = {
            "new_version": revision.new_version,
            "new_fingerprint": revision.new_fingerprint,
            "status": revision.status,
            "live_build_fingerprint": revision.live_build_fingerprint,
        }
    audit(db, p, "foundry.feedback_submitted", target=slug,
          details={"build_fingerprint": build_fingerprint, "kind": kind,
                   "has_changes": bool(parsed_changes)})
    await db.commit()
    await db.refresh(row)
    return {"feedback": _feedback_out(row), "revision": revision_preview}


def _parse_change(raw: dict[str, Any]) -> FeedbackChange:
    """Map one API change object to a structured FeedbackChange. Unknown
    fields are ignored; unknown actions are refused by validation."""
    columns = tuple(
        (str(c[0]), str(c[1])) for c in (raw.get("columns") or [])
        if isinstance(c, (list, tuple)) and len(c) == 2)
    return FeedbackChange(
        action=str(raw.get("action") or ""),
        name=raw.get("name"),
        path=raw.get("path"),
        method=raw.get("method"),
        table=raw.get("table"),
        columns=columns,
        endpoints=tuple(raw.get("endpoints") or ()),
        new_name=raw.get("new_name"),
        purpose=raw.get("purpose"),
    )
