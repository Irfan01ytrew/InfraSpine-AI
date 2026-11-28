"""Step J (AI Workforce OS), slice 2: skill-registry rules (pure functions).

Pure functions (stdlib only, plus the shared skill-name normalizer that the
human-expertise module already owns). No database, no routes, no production
code changed. These rules decide what a *verified skill* means; they never
execute anything and never grant anything.

A skill is a verified fact about an agent, never a permission:

* Recording that an agent has a skill does not give the agent a permission,
  a data scope, or a tool. Assignment (slice 1) uses skills only as a
  matching input; enforcement stays with the AK controls and the platform.
* Verification (plan §7.3) is allowed for the agent's accountable owner, an
  administrator, or a holder of ``workforce.manage`` on the organisation
  scope. Nobody else can verify a skill — and a skill that was never
  verified never counts as a qualification.
* Verified skills expire. An expired skill is not current and must not
  match an assignment (slice 1's ``skill_match`` only ever receives the
  current skill set from the service). Expiry is a fact about the record,
  not a deletion: the row stays so the history is auditable.
* Skill names are normalized exactly like human expertise tags
  (``app.schemas.expertise.normalize_skill`` — NFKC + casefold + collapsed
  whitespace), so "Invoice Triage", "invoice  triage" and "INVOICE TRIAGE"
  are one skill, not three.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta

from app.schemas.expertise import normalize_skill

__all__ = [
    "AGENT_KINDS",
    "DEFAULT_VERIFICATION_VALIDITY_DAYS",
    "STATUS_EXPIRED",
    "STATUS_UNVERIFIED",
    "STATUS_VERIFIED",
    "SKILL_NAME_MAX_LENGTH",
    "SKILL_NAME_MIN_LENGTH",
    "default_expiry",
    "is_skill_current",
    "may_verify_skill",
    "skill_status",
    "validate_agent_kind",
    "validate_skill_name",
]

AGENT_KINDS = ("studio", "governance", "profile")
SKILL_NAME_MIN_LENGTH = 2
SKILL_NAME_MAX_LENGTH = 60
DEFAULT_VERIFICATION_VALIDITY_DAYS = 365

STATUS_UNVERIFIED = "unverified"
STATUS_VERIFIED = "verified"
STATUS_EXPIRED = "expired"


def validate_skill_name(value: object) -> tuple[str | None, str | None]:
    """Normalize a skill name; return ``(name, None)`` or ``(None, error)``.

    Names are 2–60 characters after normalization and may not contain
    control characters. Never raises.
    """
    try:
        raw = value
    except Exception:
        return None, "skill name must be text"
    if not isinstance(raw, str):
        # Never coerce: str(None) would silently become the skill "none".
        return None, "skill name must be text"
    if any(unicodedata.category(character) == "Cc" for character in raw):
        return None, "skill name cannot contain control characters"
    name = normalize_skill(raw)
    if not name:
        return None, "skill name cannot be empty"
    if len(name) < SKILL_NAME_MIN_LENGTH:
        return None, f"skill name must be at least {SKILL_NAME_MIN_LENGTH} characters"
    if len(name) > SKILL_NAME_MAX_LENGTH:
        return None, f"skill name must be at most {SKILL_NAME_MAX_LENGTH} characters"
    return name, None


def validate_agent_kind(value: object) -> tuple[str | None, str | None]:
    """Normalize an agent kind; return ``(kind, None)`` or ``(None, error)``."""
    try:
        kind = value
    except Exception:
        return None, "agent kind must be text"
    if not isinstance(kind, str):
        return None, "agent kind must be text"
    kind = kind.strip().lower()
    if kind not in AGENT_KINDS:
        return None, f"unknown agent kind {kind!r}"
    return kind, None


def skill_status(
    verified_at: datetime | None,
    expires_at: datetime | None,
    now: datetime,
) -> str:
    """One of ``unverified`` / ``verified`` / ``expired`` for a skill row.

    A row with no verification is unverified. A verified row is expired
    once ``now`` reaches its expiry (expiry is inclusive: at exactly
    ``expires_at`` the skill no longer counts). Never raises on bad input —
    an unparseable state reads as unverified, never as verified.
    """
    if verified_at is None or not isinstance(verified_at, datetime):
        return STATUS_UNVERIFIED
    if expires_at is not None and not isinstance(expires_at, datetime):
        return STATUS_UNVERIFIED
    try:
        expired = expires_at is not None and now >= expires_at
    except TypeError:
        return STATUS_UNVERIFIED
    return STATUS_EXPIRED if expired else STATUS_VERIFIED


def is_skill_current(
    verified_at: datetime | None,
    expires_at: datetime | None,
    now: datetime,
) -> bool:
    """True only for a verified, unexpired skill — the only kind assignment
    may match on."""
    return skill_status(verified_at, expires_at, now) == STATUS_VERIFIED


def default_expiry(
    verified_at: datetime,
    *,
    validity_days: int = DEFAULT_VERIFICATION_VALIDITY_DAYS,
) -> datetime:
    """When a verification lapses if no explicit expiry is given."""
    return verified_at + timedelta(days=validity_days)


def may_verify_skill(
    *,
    is_admin: bool,
    is_owner: bool,
    has_workforce_manage: bool,
) -> bool:
    """Plan §7.3, as a pure rule: the agent's accountable owner, an
    administrator, or a ``workforce.manage`` holder may verify a skill.

    Nobody else may — in particular, an agent can never verify its own
    skills, and one agent's owner cannot verify another agent's skills.
    """
    return bool(is_admin or is_owner or has_workforce_manage)
