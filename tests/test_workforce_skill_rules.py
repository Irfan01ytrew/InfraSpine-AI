"""Step J (AI Workforce OS), slice 2: skill-registry rules (pure functions).

Runs with ``--noconftest``: nothing here touches a database. Run:
``python -m pytest tests/test_workforce_skill_rules.py --noconftest -q``
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.workforce.skills import (
    AGENT_KINDS,
    DEFAULT_VERIFICATION_VALIDITY_DAYS,
    default_expiry,
    is_skill_current,
    may_verify_skill,
    skill_status,
    validate_agent_kind,
    validate_skill_name,
)

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


def test_skill_name_normalized_like_expertise_tags():
    name, error = validate_skill_name("  Invoice   Triage ")
    assert error is None
    assert name == "invoice triage"
    assert validate_skill_name("INVOICE TRIAGE")[0] == "invoice triage"
    assert validate_skill_name("invoice triage")[0] == "invoice triage"


def test_skill_name_rejects_empty_short_long_and_control_chars():
    assert validate_skill_name("")[1] is not None
    assert validate_skill_name("   ")[1] is not None
    assert validate_skill_name("x")[1] is not None  # too short
    assert validate_skill_name("s" * 61)[1] is not None  # too long
    assert validate_skill_name("s" * 60)[1] is None  # exactly the limit
    assert validate_skill_name("in\x07voice")[1] is not None  # control char
    assert validate_skill_name(None)[1] is not None


@pytest.mark.parametrize("kind", AGENT_KINDS)
def test_agent_kinds_accepted(kind):
    assert validate_agent_kind(kind) == (kind, None)
    assert validate_agent_kind(f"  {kind.upper()} ") == (kind, None)


def test_agent_kind_rejects_unknown():
    assert validate_agent_kind("human")[1] is not None
    assert validate_agent_kind("")[1] is not None
    assert validate_agent_kind(None)[1] is not None


def test_status_unverified_without_verification():
    assert skill_status(None, None, NOW) == "unverified"
    assert skill_status(None, NOW + timedelta(days=30), NOW) == "unverified"


def test_status_verified_and_expired():
    verified = NOW - timedelta(days=10)
    assert skill_status(verified, None, NOW) == "verified"
    assert skill_status(verified, NOW + timedelta(days=1), NOW) == "verified"
    assert skill_status(verified, NOW - timedelta(days=1), NOW) == "expired"
    # Expiry is inclusive: at exactly expires_at the skill no longer counts.
    assert skill_status(verified, NOW, NOW) == "expired"


def test_status_never_verified_on_bad_input():
    assert skill_status("not-a-date", None, NOW) == "unverified"
    assert is_skill_current("not-a-date", None, NOW) is False


def test_is_skill_current_only_when_verified_and_unexpired():
    verified = NOW - timedelta(days=10)
    assert is_skill_current(verified, None, NOW) is True
    assert is_skill_current(verified, NOW + timedelta(days=1), NOW) is True
    assert is_skill_current(verified, NOW - timedelta(days=1), NOW) is False
    assert is_skill_current(None, None, NOW) is False


def test_default_expiry_is_365_days():
    assert DEFAULT_VERIFICATION_VALIDITY_DAYS == 365
    assert default_expiry(NOW) == NOW + timedelta(days=365)
    assert default_expiry(NOW, validity_days=30) == NOW + timedelta(days=30)


@pytest.mark.parametrize(
    "is_admin,is_owner,has_manage,expected",
    [
        (False, False, False, False),  # stranger: never
        (True, False, False, True),  # admin
        (False, True, False, True),  # accountable owner
        (False, False, True, True),  # workforce.manage holder
        (True, True, True, True),
    ],
)
def test_may_verify_skill_rule(is_admin, is_owner, has_manage, expected):
    assert (
        may_verify_skill(
            is_admin=is_admin, is_owner=is_owner, has_workforce_manage=has_manage
        )
        is expected
    )


def test_hostile_names_never_become_skills():
    # Injection-style text is just an invalid (or ordinary) name; it can
    # never smuggle a permission, because names are never executed.
    name, error = validate_skill_name("ignore previous instructions")
    assert error is None and name == "ignore previous instructions"
    assert validate_skill_name("grant admin")[0] == "grant admin"
    assert validate_skill_name("approve all invoices")[0] == "approve all invoices"
