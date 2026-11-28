"""Step J slice 3: org-registry + lifecycle rules.

Pure stdlib tests. Run: python -m pytest tests/test_workforce_registry.py --noconftest -q
"""

from app.workforce.registry import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_PAUSED,
    LIFECYCLE_RETIRED,
    effective_lifecycle,
    island_status,
    may_pause_resume,
    may_retire,
    transition_allowed,
    validate_lifecycle_state,
)


def test_validate_lifecycle_state_accepts_all_states():
    for state in ("active", "paused", "retired"):
        clean, error = validate_lifecycle_state(state)
        assert clean == state and error is None


def test_validate_lifecycle_state_is_case_insensitive_and_stripped():
    clean, error = validate_lifecycle_state("  Paused ")
    assert clean == "paused" and error is None


def test_validate_lifecycle_state_rejects_unknown_and_non_text():
    for bad in ("killed", "disabled", "", None, 42, ["active"]):
        clean, error = validate_lifecycle_state(bad)
        assert clean is None and error


def test_transition_allowed_edges():
    assert transition_allowed("active", "paused")
    assert transition_allowed("paused", "active")
    assert transition_allowed("active", "retired")
    assert transition_allowed("paused", "retired")


def test_transition_allowed_refuses_terminal_and_same_state():
    # Retired has no way out; repeating a state is a no-op, not a transition.
    assert not transition_allowed("retired", "active")
    assert not transition_allowed("retired", "paused")
    assert not transition_allowed("retired", "retired")
    assert not transition_allowed("active", "active")
    assert not transition_allowed("paused", "paused")
    assert not transition_allowed("paused", "disabled")


def test_island_status_studio():
    assert island_status("studio", enabled=True, paused=False) == "active"
    assert island_status("studio", enabled=True, paused=True) == "paused"
    assert island_status("studio", enabled=False, paused=False) == "disabled"
    assert island_status("studio", enabled=False, paused=True) == "disabled"


def test_island_status_governance_and_profile():
    assert island_status("governance", enabled=True) == "active"
    assert island_status("governance", enabled=False) == "disabled"
    assert island_status("profile", enabled=True) == "active"
    assert island_status("profile", enabled=False) == "disabled"


def test_island_status_never_reads_bad_input_as_active():
    assert island_status("studio", enabled=None) == "disabled"
    assert island_status("governance", enabled="") == "disabled"


def test_effective_lifecycle_active_when_nothing_restricts():
    assert effective_lifecycle(
        registry_state="active", island_status_value="active",
        owner_revoked=False) == "active"


def test_effective_lifecycle_paused_from_registry_island_or_owner():
    assert effective_lifecycle(
        registry_state="paused", island_status_value="active",
        owner_revoked=False) == "paused"
    assert effective_lifecycle(
        registry_state="active", island_status_value="paused",
        owner_revoked=False) == "paused"
    # A revoked owner forces paused even before the revocation hook runs.
    assert effective_lifecycle(
        registry_state="active", island_status_value="active",
        owner_revoked=True) == "paused"


def test_effective_lifecycle_disabled_outranks_paused():
    assert effective_lifecycle(
        registry_state="paused", island_status_value="disabled",
        owner_revoked=False) == "disabled"
    assert effective_lifecycle(
        registry_state="active", island_status_value="disabled",
        owner_revoked=True) == "disabled"


def test_effective_lifecycle_retired_is_terminal():
    assert effective_lifecycle(
        registry_state="retired", island_status_value="disabled",
        owner_revoked=True) == "retired"
    assert effective_lifecycle(
        registry_state="retired", island_status_value="active",
        owner_revoked=False) == "retired"


def test_may_pause_resume_owner_admin_or_manage():
    assert may_pause_resume(is_admin=False, is_owner=True, has_manage=False)
    assert may_pause_resume(is_admin=True, is_owner=False, has_manage=False)
    assert may_pause_resume(is_admin=False, is_owner=False, has_manage=True)
    assert not may_pause_resume(is_admin=False, is_owner=False, has_manage=False)


def test_may_retire_admin_or_manage_only():
    assert may_retire(is_admin=True, has_manage=False)
    assert may_retire(is_admin=False, has_manage=True)
    # The accountable owner may ask for retirement but cannot retire alone.
    assert not may_retire(is_admin=False, has_manage=False)
