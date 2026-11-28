"""Step I slice 4 — audit coverage of the privileged admin surface.

The first measured answer to "does every privileged action leave an audit
trail?": a curated inventory of every state-changing route under
``/api/admin/*`` and the audit event each one must emit.

Two rules keep the inventory honest:

* Completeness — ``measure_audit_coverage`` walks the real app and fails
  the inventory (via the tests) when a state-changing admin route is not
  listed here. A new privileged route cannot slip in unaudited.
* Coverage — an inventoried action is covered when it emits at least one
  of its declared audit events, or when it is read-only by effect (a POST
  used only to carry a body for a read, e.g. the retention preview).
  ``coverage`` is the fraction of inventoried state-changing actions that
  are covered. It is 1.0 on the current app.

The inventory is data, not enforcement: the behavioural tests in
``tests/test_step_i_audit_coverage.py`` prove the declared events are
actually written to ``audit_logs`` for the actions added or changed in
this slice, and the pre-existing events are proven by the services' own
test suites (cited in the plan doc).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.routing import APIRoute

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

ADMIN_PREFIX = "/api/admin/"


@dataclass(frozen=True)
class PrivilegedAction:
    """One state-changing admin route and the audit trail it must leave."""

    method: str
    path: str  # FastAPI path template, e.g. "/api/admin/access/groups"
    action: str  # human-readable name of the privileged action
    audit_events: tuple[str, ...] = ()  # acceptable audit events; any one proves coverage
    read_only: bool = False  # POST/PUT carrying a body for a read-only operation


def _a(method: str, path: str, action: str, *events: str, read_only: bool = False) -> PrivilegedAction:
    return PrivilegedAction(method, path, action, tuple(events), read_only)


# Every state-changing route under /api/admin/* on HEAD 20dc227 (70 routes).
# Order: alphabetical by path, matching the route walk. Event names were read
# from the service that performs each action (see the plan doc §6b slice 4).
PRIVILEGED_ADMIN_ACTIONS: tuple[PrivilegedAction, ...] = (
    # Access administration (app/services/access_control.py)
    _a("POST", "/api/admin/access/grants", "grant a permission", "access.permission_granted"),
    _a("POST", "/api/admin/access/grants/{grant_id}/revoke", "revoke a permission grant", "access.permission_revoked"),
    _a("POST", "/api/admin/access/groups", "create an access group", "access.group_created"),
    _a("PATCH", "/api/admin/access/groups/{group_id}", "update an access group", "access.group_updated"),
    _a("DELETE", "/api/admin/access/groups/{group_id}", "delete an access group", "access.group_deleted"),
    _a("POST", "/api/admin/access/groups/{group_id}/members", "add a group member", "access.member_added"),
    _a("DELETE", "/api/admin/access/groups/{group_id}/members/{user_id}", "remove a group member", "access.member_removed"),
    # Agent-run retention (app/services/agent_run_retention.py)
    _a("POST", "/api/admin/agent-runs/retention/preview", "preview retention purge", read_only=True),
    _a("POST", "/api/admin/agent-runs/retention/purge", "purge expired agent-run content", "agent.profile_run_content_purged"),
    # Approval chains and limits (app/services/approval_chains.py, approval_authority.py)
    _a("POST", "/api/admin/approval-chain", "add an approval-chain step", "approval_chain.step_added"),
    _a("PATCH", "/api/admin/approval-chain/{step_id}", "change an approval-chain step", "approval_chain.stage_changed"),
    _a("DELETE", "/api/admin/approval-chain/{step_id}", "remove an approval-chain step", "approval_chain.step_removed"),
    _a("POST", "/api/admin/approval-chain/{step_id}/move", "move an approval-chain step", "approval_chain.step_moved"),
    _a("POST", "/api/admin/approval-limits", "create an approval limit", "approval_limit.created"),
    _a("PATCH", "/api/admin/approval-limits/{limit_id}", "change or remove an approval limit",
       "approval_limit.changed", "approval_limit.removed"),
    # Deployment: company profile, license, industry templates
    _a("PUT", "/api/admin/deployment/company", "update the company profile", "deployment.company_profile_updated"),
    _a("PUT", "/api/admin/deployment/license", "install a signed license", "deployment.license_installed"),
    _a("POST", "/api/admin/deployment/templates/{template_id}/apply", "apply an industry template",
       "deployment.industry_template_applied"),
    # Engine health checks — audited by slice 4 (they invoke a privileged
    # external check even though they change no stored state).
    _a("POST", "/api/admin/document-parsers/{file_format}/test", "test a document parser", "admin.document_parser_tested"),
    _a("POST", "/api/admin/embedding-models/test", "test the embedding model", "admin.embedding_model_tested"),
    _a("POST", "/api/admin/ocr-engine/test", "test the OCR engine", "admin.ocr_engine_tested"),
    # Partner workspaces (app/services/partner_portal.py)
    _a("POST", "/api/admin/partner-workspaces", "create a partner workspace", "partner.workspace_created"),
    _a("PATCH", "/api/admin/partner-workspaces/{workspace_id}/identities/{identity_id}/active",
       "change a partner identity status", "partner.identity_status_changed"),
    _a("POST", "/api/admin/partner-workspaces/{workspace_id}/invitations", "invite a partner user",
       "partner.invitation_created"),
    _a("DELETE", "/api/admin/partner-workspaces/{workspace_id}/invitations/{invitation_id}",
       "revoke a partner invitation", "partner.invitation_revoked"),
    _a("POST", "/api/admin/partner-workspaces/{workspace_id}/sources", "add a partner source", "partner.source_created"),
    _a("POST", "/api/admin/partner-workspaces/{workspace_id}/sources/upload", "upload a partner document",
       "partner.document_uploaded"),
    _a("DELETE", "/api/admin/partner-workspaces/{workspace_id}/sources/{source_id}", "archive a partner source",
       "partner.source_archived"),
    _a("POST", "/api/admin/partner-workspaces/{workspace_id}/sources/{source_id}/retry", "retry a partner source",
       "partner.source_retry_requested"),
    _a("PATCH", "/api/admin/partner-workspaces/{workspace_id}/status", "change a partner workspace status",
       "partner.workspace_status_changed"),
    # Platform administration (already platform-gated at the service layer)
    _a("PUT", "/api/admin/platform/org-settings", "change the organisation time zone", "platform.org_time_zone_changed"),
    _a("POST", "/api/admin/platform/outbox/requeue", "requeue dead outbox events", "platform.outbox_requeued"),
    # Positions / org chart (app/services/org_structure.py)
    _a("POST", "/api/admin/positions", "create a position", "position.created"),
    _a("POST", "/api/admin/positions/library/apply", "apply the position library", "position.library_applied"),
    _a("PATCH", "/api/admin/positions/{position_id}", "update, archive or restore a position",
       "position.updated", "position.archived", "position.restored"),
    # AI providers (app/services/provider_admin.py)
    _a("POST", "/api/admin/providers/custom", "create a custom provider", "provider.custom_created"),
    _a("PUT", "/api/admin/providers/{provider_id}", "update a provider config",
       "provider.config_updated", "provider.endpoint_changed"),
    _a("DELETE", "/api/admin/providers/{provider_id}", "remove a provider", "provider.removed"),
    _a("POST", "/api/admin/providers/{provider_id}/activate", "activate a provider", "provider.activated"),
    _a("POST", "/api/admin/providers/{provider_id}/credentials", "stage a provider credential",
       "provider.credential_staged"),
    _a("POST", "/api/admin/providers/{provider_id}/deactivate", "deactivate a provider", "provider.deactivated"),
    _a("PUT", "/api/admin/providers/{provider_id}/image-sharing", "change provider image sharing",
       "provider.image_sharing_authorized", "provider.image_sharing_revoked"),
    _a("POST", "/api/admin/providers/{provider_id}/test", "test a provider connection",
       "provider.connection_tested", "provider.endpoint_rejected"),
    # Quality gates (app/qa/service.py)
    _a("POST", "/api/admin/quality/runs", "enqueue a QA run", "qa.run.enqueue"),
    _a("POST", "/api/admin/quality/runs/{run_id}/cancel", "cancel a QA run", "qa.run.cancel"),
    _a("POST", "/api/admin/quality/signoff", "record a release sign-off", "qa.release.signoff"),
    # Reports — quota change audited by slice 4 (in-memory config, no DB row).
    _a("PUT", "/api/admin/reports/quotas/{department_id}", "update a department token quota",
       "admin.department_quota_updated"),
    # Request forms (app/services/request_forms.py)
    _a("POST", "/api/admin/request-forms", "create a request form", "request_form.created"),
    _a("PUT", "/api/admin/request-forms/{form_id}", "update a request form", "request_form.changed"),
    _a("DELETE", "/api/admin/request-forms/{form_id}", "archive a request form", "request_form.archived"),
    # Role templates (app/services/role_templates.py)
    _a("POST", "/api/admin/role-template-assignments/{assignment_id}/revoke", "revoke a role-template assignment",
       "access.role_template_revoked"),
    _a("POST", "/api/admin/role-template-change-requests/{request_id}/decision",
       "decide a role-template change request",
       "access.role_template_change_approved", "access.role_template_change_rejected"),
    _a("POST", "/api/admin/role-templates", "create a role template", "access.role_template_created"),
    _a("PATCH", "/api/admin/role-templates/{template_id}/active", "activate or deactivate a role template",
       "access.role_template_activated", "access.role_template_deactivated"),
    _a("POST", "/api/admin/role-templates/{template_id}/assignments", "assign a role template",
       "access.role_template_assigned"),
    _a("POST", "/api/admin/role-templates/{template_id}/change-requests", "propose a role-template change",
       "access.role_template_change_proposed"),
    # Identity administration (app/services/users.py) — platform-gated in slice 3.
    _a("POST", "/api/admin/users/invitations", "invite a user", "onboarding.invitation_created"),
    _a("POST", "/api/admin/users/invitations/csv-preview", "preview a CSV invitation batch",
       "onboarding.csv_previewed", read_only=True),
    _a("POST", "/api/admin/users/invitations/{invitation_id}/resend", "resend a user invitation",
       "onboarding.invitation_resent"),
    _a("POST", "/api/admin/users/invitations/{invitation_id}/revoke", "revoke a user invitation",
       "onboarding.invitation_revoked"),
    _a("PATCH", "/api/admin/users/{user_id}", "change a user's role and departments",
       "user.role_and_departments_changed", "user.administrator_granted", "user.administrator_revoked",
       "access.role_template_assignments_revoked"),
    _a("POST", "/api/admin/users/{user_id}/active", "activate or deactivate a user",
       "user.activated", "user.deactivated"),
    _a("POST", "/api/admin/users/{user_id}/knowledge-transfer", "transfer knowledge stewardship",
       "knowledge.stewardship_transferred"),
    _a("PUT", "/api/admin/users/{user_id}/org", "change a user's org assignment", "user.org_assignment_changed"),
    # Webhooks (app/services/webhooks.py)
    _a("POST", "/api/admin/webhooks", "create a webhook", "webhook.created"),
    _a("POST", "/api/admin/webhooks/deliveries/{delivery_id}/redeliver", "redeliver a webhook event",
       "webhook.redelivered"),
    _a("PATCH", "/api/admin/webhooks/{webhook_id}", "update a webhook", "webhook.changed"),
    _a("DELETE", "/api/admin/webhooks/{webhook_id}", "delete a webhook", "webhook.deleted"),
    _a("POST", "/api/admin/webhooks/{webhook_id}/rotate-secret", "rotate a webhook secret", "webhook.secret_rotated"),
    _a("POST", "/api/admin/webhooks/{webhook_id}/test", "test a webhook", "webhook.test", "webhook.test_queued"),
)


@dataclass(frozen=True)
class AuditCoverageReport:
    """Result of measuring the inventory against the live app."""

    total: int  # state-changing routes under /api/admin/*
    inventoried: int  # of those, listed in PRIVILEGED_ADMIN_ACTIONS
    covered: int  # of the inventoried, audited or read-only by effect
    uninventoried: tuple[str, ...]  # "METHOD path" lines missing from the inventory
    uncovered: tuple[str, ...]  # "METHOD path" lines with no audit event and not read-only
    stale: tuple[str, ...] = ()  # inventory entries for routes no longer in the app

    @property
    def coverage(self) -> float:
        return self.covered / self.inventoried if self.inventoried else 1.0


def _iter_api_routes(routes, prefix: str = ""):
    """Yield (path, APIRoute), flattening deferred router inclusions.

    Same walk as the slice-1 authorization-coverage test: the app registers
    routers through FastAPI's deferred inclusion, so ``app.routes`` alone
    does not list them.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif getattr(route, "original_router", None) is not None:
            context = getattr(route, "include_context", None)
            included_prefix = (getattr(context, "prefix", "") or "") if context is not None else ""
            yield from _iter_api_routes(route.original_router.routes, prefix + included_prefix)
        elif hasattr(route, "routes"):
            yield from _iter_api_routes(route.routes, prefix + (getattr(route, "path", "") or ""))


def _is_covered(action: PrivilegedAction) -> bool:
    return bool(action.audit_events) or action.read_only


def measure_audit_coverage(app) -> AuditCoverageReport:
    """Measure the inventory against a FastAPI app's registered routes."""
    inventory = {(a.method, a.path): a for a in PRIVILEGED_ADMIN_ACTIONS}
    seen: set[tuple[str, str]] = set()
    for path, route in _iter_api_routes(app.routes):
        if not path.startswith(ADMIN_PREFIX):
            continue
        for method in sorted(set(route.methods or set()) & STATE_CHANGING_METHODS):
            seen.add((method, path))
    uninventoried = tuple(sorted(f"{m} {p}" for m, p in seen - set(inventory)))
    stale = tuple(sorted(f"{m} {p}" for m, p in set(inventory) - seen))
    covered = sum(1 for a in PRIVILEGED_ADMIN_ACTIONS if _is_covered(a))
    uncovered = tuple(sorted(
        f"{a.method} {a.path}" for a in PRIVILEGED_ADMIN_ACTIONS if not _is_covered(a)
    ))
    return AuditCoverageReport(
        total=len(seen),
        inventoried=len(seen & set(inventory)),
        covered=covered,
        uninventoried=uninventoried,
        uncovered=uncovered,
        stale=stale,
    )


def format_report(report: AuditCoverageReport) -> str:
    lines = [
        f"Audit coverage: {report.covered}/{report.inventoried} inventoried privileged actions covered "
        f"({report.coverage:.0%}); {report.total} state-changing /api/admin/* routes, "
        f"{len(report.uninventoried)} uninventoried.",
    ]
    if report.uninventoried:
        lines.append("Uninventoried privileged routes (add them to PRIVILEGED_ADMIN_ACTIONS):")
        lines.extend(f"  - {line}" for line in report.uninventoried)
    if report.uncovered:
        lines.append("Inventoried actions with no audit event:")
        lines.extend(f"  - {line}" for line in report.uncovered)
    if report.stale:
        lines.append("Stale inventory entries (route no longer exists, remove them):")
        lines.extend(f"  - {line}" for line in report.stale)
    return "\n".join(lines)
