"""Step J (AI Workforce OS), slice 3: org-registry + lifecycle rules (pure functions).

Pure functions (stdlib only). No database, no routes, no production code
changed. These rules decide what a workforce agent's lifecycle state
means; they never execute anything and never grant anything.

Lifecycle states (plan §1): ``active`` / ``paused`` / ``retired``.

* ``active`` — the agent may be assigned work and may run.
* ``paused`` — the agent is stopped for now; queued work is not claimed
  and new work is not assigned. Pausing is reversible.
* ``retired`` — terminal. A retired agent never runs again and can never
  be resumed. Retirement leaves a lifecycle history (who, when, why).

Transitions: active <-> paused, active -> retired, paused -> retired.
Retired has no way out. Repeating the same state is a no-op at the
service layer (idempotent), not an error.

Effective status — the registry never reports a rosier state than the
most restrictive fact it knows:

* A retired registry entry is retired, whatever the island says.
* An island-disabled agent is disabled (Studio ``enabled=False``,
  governance ``enabled=False``, profile ``enabled=False``).
* A paused registry entry, a paused island agent, or an agent whose
  accountable owner has been revoked is paused.
* Otherwise the agent is active.

The owner-revocation rule (the open question from prep, answered here):
when an agent's accountable owner is revoked (``User.is_active`` is
false — see ``app.platform.access.revoke_user_access``), the agent must
be PAUSED and its verified skills FROZEN. Revocation never silently
reassigns ownership and never leaves the revoked owner's agent running:

* The registry entry keeps its owner — nobody is substituted.
* The pause is recorded as a lifecycle event with the reason
  "owner access revoked".
* Freezing a skill means its ``expires_at`` is moved to now: the
  verification history (``verified_at`` / ``verified_by``) is preserved,
  the skill reads as ``expired`` from that moment, and assignment (slice
  1) never matches on it again. A new accountable owner may re-verify
  the skill later; the freeze itself deletes nothing.
* The effective status derives the pause even before the revocation
  hook has run, so a missed hook cannot leave a revoked owner's agent
  looking active.

Who may change a lifecycle (plan §7.7, safe defaults decided here):
pause/resume — the agent's accountable owner, an administrator, or a
holder of ``agents.manage`` / ``workforce.manage`` on the organisation
scope. Retire — administrator or ``agents.manage`` / ``workforce.manage``
holder only; the owner may ask for retirement but cannot retire the
agent themselves.
"""

from __future__ import annotations

__all__ = [
    "LIFECYCLE_ACTIVE",
    "LIFECYCLE_PAUSED",
    "LIFECYCLE_RETIRED",
    "LIFECYCLE_STATES",
    "STATUS_ACTIVE",
    "STATUS_DISABLED",
    "STATUS_PAUSED",
    "STATUS_RETIRED",
    "ALLOWED_TRANSITIONS",
    "effective_lifecycle",
    "island_status",
    "may_pause_resume",
    "may_retire",
    "transition_allowed",
    "validate_lifecycle_state",
]

LIFECYCLE_ACTIVE = "active"
LIFECYCLE_PAUSED = "paused"
LIFECYCLE_RETIRED = "retired"
LIFECYCLE_STATES = (LIFECYCLE_ACTIVE, LIFECYCLE_PAUSED, LIFECYCLE_RETIRED)

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_RETIRED = "retired"
STATUS_DISABLED = "disabled"

ALLOWED_TRANSITIONS = frozenset({
    (LIFECYCLE_ACTIVE, LIFECYCLE_PAUSED),
    (LIFECYCLE_PAUSED, LIFECYCLE_ACTIVE),
    (LIFECYCLE_ACTIVE, LIFECYCLE_RETIRED),
    (LIFECYCLE_PAUSED, LIFECYCLE_RETIRED),
})


def validate_lifecycle_state(value: object) -> tuple[str | None, str | None]:
    """Normalize a lifecycle state; return ``(state, None)`` or ``(None, error)``."""
    if not isinstance(value, str):
        return None, "lifecycle state must be text"
    state = value.strip().lower()
    if state not in LIFECYCLE_STATES:
        return None, f"unknown lifecycle state {state!r}"
    return state, None


def transition_allowed(from_state: str, to_state: str) -> bool:
    """True only for a listed edge. Same-state is False (the service
    treats it as an idempotent no-op, not a transition)."""
    return (from_state, to_state) in ALLOWED_TRANSITIONS


def island_status(kind: str, *, enabled: bool, paused: bool = False) -> str:
    """One of ``active`` / ``paused`` / ``disabled`` for an island agent.

    Studio agents carry ``enabled`` + ``paused``; governance and profile
    agents carry ``enabled`` only. A disabled island agent is disabled
    whatever else the registry says. Never raises on bad input — an
    unparseable island row reads as disabled, never as active.
    """
    try:
        if not bool(enabled):
            return STATUS_DISABLED
        if kind == "studio" and bool(paused):
            return STATUS_PAUSED
    except Exception:
        return STATUS_DISABLED
    return STATUS_ACTIVE


def effective_lifecycle(
    *,
    registry_state: str,
    island_status_value: str,
    owner_revoked: bool,
) -> str:
    """The status the registry reports: the most restrictive fact wins.

    Retired (registry) > disabled (island) > paused (registry, island,
    or revoked owner) > active. A revoked owner forces paused unless the
    agent is already retired — so the pause holds even before the
    revocation hook has run.
    """
    if registry_state == LIFECYCLE_RETIRED:
        return STATUS_RETIRED
    if island_status_value == STATUS_DISABLED:
        return STATUS_DISABLED
    if (
        registry_state == LIFECYCLE_PAUSED
        or island_status_value == STATUS_PAUSED
        or bool(owner_revoked)
    ):
        return STATUS_PAUSED
    return STATUS_ACTIVE


def may_pause_resume(
    *,
    is_admin: bool,
    is_owner: bool,
    has_manage: bool,
) -> bool:
    """Plan §7.7: the accountable owner, an administrator, or an
    ``agents.manage`` / ``workforce.manage`` holder may pause or resume
    an agent. Nobody else may."""
    return bool(is_admin or is_owner or has_manage)


def may_retire(
    *,
    is_admin: bool,
    has_manage: bool,
) -> bool:
    """Plan §7.7: retiring an agent is administrator or manage-holder
    only. The accountable owner may ask for retirement but cannot retire
    the agent themselves."""
    return bool(is_admin or has_manage)
