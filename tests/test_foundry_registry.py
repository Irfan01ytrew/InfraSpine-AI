"""Step AM slice 6 — tests for app/foundry/registry.py.

Pure-function tests run with `python -m pytest tests/test_foundry_registry.py
--noconftest`.
"""

import pytest

from app.foundry.registry import (
    AppRecord,
    BuildRecord,
    FeedbackRecord,
    app_summary,
    empty_registry,
    get_app_detail,
    list_apps,
    record_deploy_state,
    record_feedback,
    register_build,
)


def _build(slug="leave-tracker", fingerprint="a" * 64, version=1, gate="preview_ready",
           deploy_state="none"):
    return BuildRecord(slug=slug, app_name="Leave Tracker", spec_version=version,
                       spec_fingerprint="s" * 64, build_fingerprint=fingerprint,
                       gate=gate, preview_token="tok", preview_expires_in_seconds=3600,
                       health="passed", tests_passed=6, tests_failed=0,
                       deploy_state=deploy_state)


def test_empty_registry_lists_nothing():
    assert list_apps(empty_registry()) == ()
    assert get_app_detail(empty_registry(), "leave-tracker") is None


def test_register_build_creates_app_and_summary():
    registry = register_build(empty_registry(), _build())
    (summary,) = list_apps(registry)
    assert summary.slug == "leave-tracker"
    assert summary.builds == 1
    assert summary.latest_version == 1
    assert summary.latest_gate == "preview_ready"
    assert summary.latest_deploy_state == "none"
    assert summary.feedback_count == 0
    detail = get_app_detail(registry, "leave-tracker")
    assert detail is not None
    assert detail.latest_build.build_fingerprint == "a" * 64


def test_second_build_is_a_new_version_of_the_same_app():
    registry = register_build(empty_registry(), _build())
    registry = register_build(registry, _build(fingerprint="b" * 64, version=2))
    (summary,) = list_apps(registry)
    assert summary.builds == 2
    assert summary.latest_version == 2
    assert get_app_detail(registry, "leave-tracker").latest_build.build_fingerprint == "b" * 64


def test_duplicate_build_fingerprint_is_refused():
    registry = register_build(empty_registry(), _build())
    with pytest.raises(ValueError, match="already registered"):
        register_build(registry, _build())


def test_app_name_mismatch_is_refused():
    registry = register_build(empty_registry(), _build())
    other = BuildRecord(slug="leave-tracker", app_name="Something Else", spec_version=2,
                        spec_fingerprint="s" * 64, build_fingerprint="b" * 64)
    with pytest.raises(ValueError, match="name mismatch"):
        register_build(registry, other)


def test_invalid_gate_or_deploy_state_is_refused():
    with pytest.raises(ValueError, match="invalid gate state"):
        register_build(empty_registry(), _build(gate="almost_ready"))
    with pytest.raises(ValueError, match="invalid deploy state"):
        register_build(empty_registry(), _build(deploy_state="flying"))


def test_record_deploy_state_moves_one_build_only():
    registry = register_build(empty_registry(), _build())
    registry = register_build(registry, _build(fingerprint="b" * 64, version=2))
    registry = record_deploy_state(registry, "leave-tracker", "a" * 64, "requested",
                                   approval_id="approval-1")
    detail = get_app_detail(registry, "leave-tracker")
    assert detail.builds[0].deploy_state == "requested"
    assert detail.builds[0].approval_id == "approval-1"
    assert detail.builds[1].deploy_state == "none"


def test_record_deploy_state_refuses_unknown_app_build_or_state():
    registry = register_build(empty_registry(), _build())
    with pytest.raises(ValueError, match="unknown app"):
        record_deploy_state(registry, "nope", "a" * 64, "requested")
    with pytest.raises(ValueError, match="unknown build"):
        record_deploy_state(registry, "leave-tracker", "f" * 64, "requested")
    with pytest.raises(ValueError, match="invalid deploy state"):
        record_deploy_state(registry, "leave-tracker", "a" * 64, "flying")


def test_record_feedback_attaches_and_counts():
    registry = register_build(empty_registry(), _build())
    feedback = FeedbackRecord(build_fingerprint="a" * 64, kind="change_request",
                              note="Add a page.", changes=("add_page",), author="u1")
    registry = record_feedback(registry, "leave-tracker", feedback)
    detail = get_app_detail(registry, "leave-tracker")
    assert len(detail.feedback) == 1
    assert detail.feedback[0].note == "Add a page."
    assert detail.latest_build.feedback_count == 1
    (summary,) = list_apps(registry)
    assert summary.feedback_count == 1


def test_record_feedback_refuses_unknown_build():
    registry = register_build(empty_registry(), _build())
    with pytest.raises(ValueError, match="unknown build"):
        record_feedback(registry, "leave-tracker",
                        FeedbackRecord(build_fingerprint="f" * 64, kind="bug", note="x"))


def test_registry_operations_do_not_mutate_the_input():
    registry = register_build(empty_registry(), _build())
    updated = register_build(registry, _build(fingerprint="b" * 64, version=2))
    assert len(get_app_detail(registry, "leave-tracker").builds) == 1
    assert len(get_app_detail(updated, "leave-tracker").builds) == 2


def test_list_apps_is_sorted_by_slug():
    registry = empty_registry()
    registry = register_build(registry, _build(slug="zebra", fingerprint="a" * 64))
    registry = register_build(registry, _build(slug="apple", fingerprint="b" * 64))
    assert [s.slug for s in list_apps(registry)] == ["apple", "zebra"]


def test_app_summary_without_builds():
    summary = app_summary(AppRecord(slug="empty", name="Empty", scope_id="s1"))
    assert summary.builds == 0
    assert summary.latest_version is None
    assert summary.latest_gate is None
