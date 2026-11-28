"""Step AM (Self-Evolving Software Foundry), slice 6 — registry (pure part).

The in-memory view of the registry: which generated apps exist, which
builds each app has, each build's test/preview/deploy state, and the
feedback attached to each build. Persistence lives in
`app/foundry/models.py` + migration `0104_foundry_registry`; the API layer
(slice 6, `app/foundry/service.py`) reads/writes the tables and renders
these same records.

Pure functions only (stdlib): the registry is a plain dict
`{slug: AppRecord}` and every function returns a new registry — nothing is
mutated in place, so a recorded build can never be silently rewritten.
Append-only: builds and feedback are added, never edited or deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

__all__ = [
    "AppRecord",
    "AppSummary",
    "BuildRecord",
    "FeedbackRecord",
    "app_summary",
    "empty_registry",
    "get_app_detail",
    "list_apps",
    "record_deploy_state",
    "record_feedback",
    "register_build",
]

DEPLOY_STATES = ("none", "requested", "approved", "deploying", "deployed", "rolled_back", "failed")
GATE_STATES = ("blocked", "preview_ready")


@dataclass(frozen=True)
class BuildRecord:
    """One build of one generated app, as recorded by slices 1–4."""

    slug: str
    app_name: str
    spec_version: int
    spec_fingerprint: str
    build_fingerprint: str
    gate: str = "blocked"  # blocked | preview_ready
    preview_token: str = ""
    preview_expires_in_seconds: int = 0
    health: str = "pending"  # pending | passed | failed
    tests_passed: int = 0
    tests_failed: int = 0
    deploy_state: str = "none"  # see DEPLOY_STATES
    deploy_target: str | None = None
    approval_id: str | None = None
    feedback_count: int = 0


@dataclass(frozen=True)
class FeedbackRecord:
    """One feedback item attached to a build."""

    build_fingerprint: str
    kind: str
    note: str
    changes: tuple[str, ...] = ()  # structured change action names
    author: str | None = None


@dataclass(frozen=True)
class AppRecord:
    """One generated app: its builds (oldest first) and feedback."""

    slug: str
    name: str
    scope_id: str
    builds: tuple[BuildRecord, ...] = ()
    feedback: tuple[FeedbackRecord, ...] = ()

    @property
    def latest_build(self) -> BuildRecord | None:
        return self.builds[-1] if self.builds else None


@dataclass(frozen=True)
class AppSummary:
    """List view of one app: latest build state only."""

    slug: str
    name: str
    builds: int
    latest_version: int | None
    latest_gate: str | None
    latest_deploy_state: str | None
    feedback_count: int


def empty_registry() -> dict:
    """A registry with no apps."""
    return {}


def register_build(registry: dict, build: BuildRecord) -> dict:
    """Record a new build. The same build fingerprint for the same app is
    refused (a build is recorded once); a new fingerprint starts a new
    version of the same app. Returns a new registry."""
    if build.gate not in GATE_STATES:
        raise ValueError(f"invalid gate state: {build.gate!r}.")
    if build.deploy_state not in DEPLOY_STATES:
        raise ValueError(f"invalid deploy state: {build.deploy_state!r}.")
    app = registry.get(build.slug)
    if app is not None:
        if any(b.build_fingerprint == build.build_fingerprint for b in app.builds):
            raise ValueError(f"build {build.build_fingerprint[:12]}… already "
                             f"registered for {build.slug}.")
        if app.name != build.app_name:
            raise ValueError(f"app name mismatch for {build.slug}: "
                             f"{app.name!r} vs {build.app_name!r}.")
        new_app = replace(app, builds=app.builds + (build,))
    else:
        new_app = AppRecord(slug=build.slug, name=build.app_name,
                            scope_id="", builds=(build,))
    return {**registry, build.slug: new_app}


def record_deploy_state(registry: dict, slug: str, build_fingerprint: str,
                        deploy_state: str, *, target: str | None = None,
                        approval_id: str | None = None) -> dict:
    """Move one recorded build to a new deploy state. The build must exist;
    the state must be a known deploy state. Returns a new registry."""
    if deploy_state not in DEPLOY_STATES:
        raise ValueError(f"invalid deploy state: {deploy_state!r}.")
    app = registry.get(slug)
    if app is None:
        raise ValueError(f"unknown app: {slug}.")
    builds = []
    found = False
    for build in app.builds:
        if build.build_fingerprint == build_fingerprint:
            found = True
            builds.append(replace(build, deploy_state=deploy_state,
                                  deploy_target=target if target is not None else build.deploy_target,
                                  approval_id=approval_id if approval_id is not None else build.approval_id))
        else:
            builds.append(build)
    if not found:
        raise ValueError(f"unknown build {build_fingerprint[:12]}… for {slug}.")
    return {**registry, slug: replace(app, builds=tuple(builds))}


def record_feedback(registry: dict, slug: str, feedback: FeedbackRecord) -> dict:
    """Attach a feedback item to a build. The build must exist. The
    build's feedback count is kept in step. Returns a new registry."""
    app = registry.get(slug)
    if app is None:
        raise ValueError(f"unknown app: {slug}.")
    builds = []
    found = False
    for build in app.builds:
        if build.build_fingerprint == feedback.build_fingerprint:
            found = True
            builds.append(replace(build, feedback_count=build.feedback_count + 1))
        else:
            builds.append(build)
    if not found:
        raise ValueError(f"unknown build {feedback.build_fingerprint[:12]}… for {slug}.")
    return {**registry, slug: replace(app, builds=tuple(builds),
                                      feedback=app.feedback + (feedback,))}


def list_apps(registry: dict) -> tuple[AppSummary, ...]:
    """All apps, sorted by slug, as list-view summaries."""
    return tuple(app_summary(app) for app in
                 (registry[slug] for slug in sorted(registry)))


def app_summary(app: AppRecord) -> AppSummary:
    latest = app.latest_build
    return AppSummary(
        slug=app.slug,
        name=app.name,
        builds=len(app.builds),
        latest_version=latest.spec_version if latest else None,
        latest_gate=latest.gate if latest else None,
        latest_deploy_state=latest.deploy_state if latest else None,
        feedback_count=len(app.feedback),
    )


def get_app_detail(registry: dict, slug: str) -> AppRecord | None:
    """The full record for one app, or None when the slug is unknown."""
    return registry.get(slug)
