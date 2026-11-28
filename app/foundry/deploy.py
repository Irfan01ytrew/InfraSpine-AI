"""Step AM (Self-Evolving Software Foundry), slice 4 — approval gate + deploy.

Turns a `preview_ready` build (slice 3) into a deploy that can only happen
after the user's explicit approval of the exact build — and rolls back
automatically when the post-deploy health check fails.

Pure functions only (stdlib), so the gate can be reasoned about without a
database. The one bridge to the platform lives in the API layer (slice 6):
the route builds a real `app.platform.approvals.ActionSpec` and
`deploy_action_hash()` here is byte-identical to its `.hash()` — a test
proves the equality, so the approval the user sees is bound to exactly the
build fingerprint below.

Safety, enforced structurally:
* An approval may only be *requested* when slice 3's gate is `preview_ready`
  (every preview check passed, every required test passed, sandbox verified).
  `may_request_approval()` refuses anything else with named reasons.
* The approval is bound to the exact deploy action: type `foundry.deploy`,
  scope, slug, build fingerprint and target. If any of those change after
  approval, `validate_approval_for_deploy()` reports `approval_hash_mismatch`
  and the deploy must not run.
* `requires_approval` is structural (slice 1): there is no code path that
  deploys without a validated, unexpired, unconsumed approval.
* Separation of duties: the approver must differ from the requester.
* No deploy target is configured by default. `plan_deploy()` refuses with
  `no_deploy_target_configured` until the owner picks a target (plan §5,
  decision 1) and the target is passed in as an explicit allow-list. The
  Foundry never invents a deploy destination.
* A deploy is not "done" until its health check passes. A failed health
  check rolls the app back to the previous build automatically
  (`record_health(..., passed=False)`), and `deploy_complete()` is True
  only for `deployed` + health `passed`.
* Every deploy keeps the previous build fingerprint, so rollback always has
  something to restore. Rollback is terminal: a rolled-back deploy cannot
  be rolled back again or re-activated — it needs a new build + approval.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime

from app.foundry.preview import GateDecision, Preview, SandboxVerification, decide_gate

__all__ = [
    "DEPLOY_ACTION_TYPE",
    "DEPLOY_PERMISSION",
    "DeployApprovalCheck",
    "DeployPlan",
    "DeployRecord",
    "RollbackRecord",
    "approve_deploy_record",
    "deploy_action_canonical",
    "deploy_action_hash",
    "deploy_complete",
    "mark_deploying",
    "may_request_approval",
    "plan_deploy",
    "record_health",
    "request_deploy_record",
    "rollback",
    "validate_approval_for_deploy",
]

DEPLOY_ACTION_TYPE = "foundry.deploy"
# Permission the *decider* must hold on the scope (checked by
# app/platform/approvals.decide_approval via required_permission).
DEPLOY_PERMISSION = "foundry.deploy"

DEPLOY_STEPS = (
    "verify build fingerprint matches the recorded build",
    "run post-deploy health check",
    "activate build (previous build kept for rollback)",
    "record deploy in the audit log",
)

VALID_DEPLOY_STATUSES = ("requested", "approved", "deploying", "deployed", "rolled_back", "failed")


# ---------------------------------------------------------------------------
# The deploy action: canonical form + hash (must match ActionSpec.hash)
# ---------------------------------------------------------------------------

def deploy_action_canonical(scope_id: str, slug: str, build_fingerprint: str,
                            target: str) -> dict:
    """Canonical form of the deploy action, mirroring
    `app.platform.approvals.ActionSpec.canonical()` for type
    `foundry.deploy` with subject = slug and no amount/currency.

    `scope_id` is the scope UUID as a string (ActionSpec canonicalises a
    UUID to its string form before hashing).
    """
    return {
        "type": DEPLOY_ACTION_TYPE,
        "scope_id": str(scope_id),
        "params": {
            "slug": slug,
            "build_fingerprint": build_fingerprint,
            "target": target,
        },
        "amount": None,
        "currency": None,
        "subject": slug,
    }


def deploy_action_hash(scope_id: str, slug: str, build_fingerprint: str,
                       target: str) -> str:
    """SHA-256 of the canonical deploy action — byte-identical to
    `ActionSpec("foundry.deploy", scope_id, params, subject=slug).hash()`."""
    encoded = json.dumps(deploy_action_canonical(scope_id, slug, build_fingerprint, target),
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Gate: approval may only be requested for a preview_ready build
# ---------------------------------------------------------------------------

def may_request_approval(preview: Preview, test_decision: GateDecision,
                         sandbox: SandboxVerification | None) -> GateDecision:
    """Approval may be requested only when slice 3's gate is `preview_ready`.
    Anything else returns the blocking reasons — the approval route must not
    be offered, and no approval request may be created."""
    return decide_gate(preview, test_decision, sandbox)


# ---------------------------------------------------------------------------
# Approval validation (pure): the deploy must not run unless this is valid
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeployApprovalCheck:
    """Outcome of validating a platform approval against a deploy."""

    valid: bool
    reasons: tuple[str, ...] = ()


def validate_approval_for_deploy(*, scope_id: str, slug: str, build_fingerprint: str,
                                 target: str, approval_status: str,
                                 approval_action_hash: str, requested_by: str,
                                 decided_by: str | None, expires_at: datetime,
                                 now: datetime, already_consumed: bool = False,
                                 ) -> DeployApprovalCheck:
    """Validate a platform approval row (plain values, no DB) against the
    exact deploy it must cover. Every failure is a named reason; a deploy
    runs only when `valid` is True.

    Checks, in order: the approval exists in an approvable state; it has not
    expired; it has not been used before; its action hash is exactly the
    hash of this deploy (slug / build fingerprint / target unchanged since
    approval); and the approver is not the requester (separation of duties).
    """
    reasons: list[str] = []
    if approval_status == "consumed" or already_consumed:
        reasons.append("approval_already_used")
    elif approval_status != "approved":
        reasons.append(f"approval_not_approved:{approval_status}")
    if now >= expires_at:
        reasons.append("approval_expired")
    expected = deploy_action_hash(scope_id, slug, build_fingerprint, target)
    if approval_action_hash != expected:
        reasons.append("approval_hash_mismatch")
    if decided_by is not None and requested_by == decided_by:
        reasons.append("separation_of_duties")
    if decided_by is None and approval_status == "approved":
        reasons.append("approval_missing_decider")
    return DeployApprovalCheck(valid=not reasons, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# Deploy plan: destination is explicit, never invented
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeployPlan:
    """The steps a deploy will take, for one exact build + target."""

    slug: str
    build_fingerprint: str
    previous_build_fingerprint: str | None
    target: str
    steps: tuple[str, ...] = DEPLOY_STEPS
    requires_approval: bool = True


def plan_deploy(*, slug: str, build_fingerprint: str, target: str,
                previous_build_fingerprint: str | None = None,
                allowed_targets: tuple[str, ...] = ()) -> DeployPlan:
    """Plan a deploy. `allowed_targets` is the owner's explicit allow-list
    (plan §5, decision 1 — no target is configured by default, so an empty
    allow-list refuses every deploy with `no_deploy_target_configured`).
    A target outside the allow-list is refused, never silently substituted.
    """
    if not allowed_targets:
        raise ValueError("no_deploy_target_configured: the owner has not "
                         "chosen a deploy target yet (plan §5, decision 1).")
    if target not in allowed_targets:
        raise ValueError(f"deploy_target_not_allowed: {target!r} is not in "
                         f"the owner's allow-list {sorted(allowed_targets)}.")
    if not build_fingerprint:
        raise ValueError("build_fingerprint is required: a deploy is bound "
                         "to one exact build.")
    return DeployPlan(slug=slug, build_fingerprint=build_fingerprint,
                      previous_build_fingerprint=previous_build_fingerprint,
                      target=target)


# ---------------------------------------------------------------------------
# Deploy record: requested -> approved -> deploying -> deployed | rolled_back
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeployRecord:
    """One deploy, as data. Status moves forward only; rollback is terminal."""

    slug: str
    build_fingerprint: str
    previous_build_fingerprint: str | None
    target: str
    status: str = "requested"  # requested | approved | deploying | deployed | rolled_back | failed
    approval_id: str | None = None
    health: str = "pending"  # pending | passed | failed
    rollback_to: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class RollbackRecord:
    """What an automatic rollback restored."""

    slug: str
    failed_build_fingerprint: str
    restored_build_fingerprint: str | None
    reason: str


def request_deploy_record(plan: DeployPlan, approval_id: str) -> DeployRecord:
    """A deploy requested for an approved plan. The record starts
    `requested`; it becomes `approved` only via `approve_deploy_record()`."""
    if plan.requires_approval is not True:
        raise ValueError("deploy requires approval: the plan must carry "
                         "requires_approval=True.")
    return DeployRecord(slug=plan.slug, build_fingerprint=plan.build_fingerprint,
                        previous_build_fingerprint=plan.previous_build_fingerprint,
                        target=plan.target, status="requested",
                        approval_id=approval_id)


def approve_deploy_record(record: DeployRecord,
                          check: DeployApprovalCheck) -> DeployRecord:
    """Move a requested deploy to `approved` — only from `requested` and
    only when the approval validation passed."""
    if record.status != "requested":
        raise ValueError(f"cannot approve a deploy in status {record.status!r}.")
    if not check.valid:
        raise ValueError("cannot approve deploy: "
                         + "; ".join(check.reasons))
    return replace(record, status="approved")


def mark_deploying(record: DeployRecord) -> DeployRecord:
    """Move an approved deploy to `deploying` — only from `approved`."""
    if record.status != "approved":
        raise ValueError(f"cannot start a deploy in status {record.status!r}.")
    return replace(record, status="deploying")


def record_health(record: DeployRecord, passed: bool,
                  reason: str = "") -> tuple[DeployRecord, RollbackRecord | None]:
    """Record the post-deploy health check. A deploy is not done until its
    health check passes. A failed check rolls the app back to the previous
    build automatically and returns the rollback record; the deploy ends
    `rolled_back`, never `deployed`."""
    if record.status != "deploying":
        raise ValueError(f"cannot record health for a deploy in status {record.status!r}.")
    if passed:
        return replace(record, status="deployed", health="passed"), None
    rollback_record = RollbackRecord(
        slug=record.slug,
        failed_build_fingerprint=record.build_fingerprint,
        restored_build_fingerprint=record.previous_build_fingerprint,
        reason=reason or "post-deploy health check failed",
    )
    return (replace(record, status="rolled_back", health="failed",
                    rollback_to=record.previous_build_fingerprint,
                    failure_reason=rollback_record.reason),
            rollback_record)


def rollback(record: DeployRecord, reason: str) -> tuple[DeployRecord, RollbackRecord]:
    """Manual rollback of a deployed app to its previous build. Only a
    `deployed` record can be rolled back; rollback is terminal."""
    if record.status != "deployed":
        raise ValueError(f"cannot roll back a deploy in status {record.status!r}.")
    rollback_record = RollbackRecord(
        slug=record.slug,
        failed_build_fingerprint=record.build_fingerprint,
        restored_build_fingerprint=record.previous_build_fingerprint,
        reason=reason,
    )
    return (replace(record, status="rolled_back",
                    rollback_to=record.previous_build_fingerprint,
                    failure_reason=reason),
            rollback_record)


def deploy_complete(record: DeployRecord) -> bool:
    """True only when the deploy finished AND its health check passed."""
    return record.status == "deployed" and record.health == "passed"
