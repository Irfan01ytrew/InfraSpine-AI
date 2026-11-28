"""Step AM slice 5 — tests for app/foundry/feedback.py.

Pure-function tests run with `python -m pytest tests/test_foundry_feedback.py
--noconftest`.
"""

import copy

import pytest

from app.foundry.feedback import (
    FeedbackChange,
    FeedbackItem,
    apply_changes_to_spec,
    revise_from_feedback,
    scan_feedback_text,
    validate_feedback_item,
)
from app.foundry.spec import spec_fingerprint, validate_spec


def _spec():
    return {
        "name": "Leave Tracker",
        "purpose": "Let employees request leave and managers approve it.",
        "pages": [
            {"name": "Requests", "path": "/requests", "endpoints": ["list_requests"]},
        ],
        "endpoints": [
            {"name": "list_requests", "method": "GET", "path": "/api/requests",
             "table": "leave_requests"},
        ],
        "tables": [
            {"name": "leave_requests",
             "columns": [{"name": "id", "type": "uuid"},
                         {"name": "status", "type": "string"}]},
        ],
    }


def _item(**overrides):
    kwargs = {
        "slug": "leave-tracker",
        "build_fingerprint": spec_fingerprint(_spec()),
        "kind": "change_request",
        "note": "Add a page for new requests.",
        "changes": (FeedbackChange(action="add_endpoint", name="create_request",
                                   method="POST", path="/api/requests",
                                   table="leave_requests"),
                    FeedbackChange(action="add_page", name="New request",
                                   path="/requests/new",
                                   endpoints=("create_request",)),),
    }
    kwargs.update(overrides)
    return FeedbackItem(**kwargs)


# ---------------------------------------------------------------------------
# Feedback text is untrusted
# ---------------------------------------------------------------------------

def test_plain_note_scans_clean():
    assert scan_feedback_text("Please add a dark mode toggle.") == ()


def test_secret_in_feedback_is_rejected():
    reasons = scan_feedback_text("Use this key instead: api_key=sk-abcdefgh12345678")
    assert "feedback_contains_secret" in reasons


def test_destructive_instruction_in_feedback_is_rejected():
    reasons = scan_feedback_text("Also DROP TABLE leave_requests while you are at it.")
    assert "feedback_contains_destructive_instruction" in reasons


def test_feedback_for_an_unknown_build_is_rejected():
    reasons = validate_feedback_item(_item(), known_build_fingerprint="f" * 64)
    assert "feedback_attaches_to_unknown_build" in reasons


def test_feedback_with_bad_kind_is_rejected():
    reasons = validate_feedback_item(_item(kind="do_it_now"),
                                     known_build_fingerprint=spec_fingerprint(_spec()))
    assert any(r.startswith("invalid_feedback_kind") for r in reasons)


def test_empty_feedback_is_rejected():
    item = _item(note="", changes=())
    reasons = validate_feedback_item(item, known_build_fingerprint=spec_fingerprint(_spec()))
    assert "feedback_empty" in reasons


def test_note_only_feedback_is_valid_but_carries_no_change():
    item = _item(changes=())
    reasons = validate_feedback_item(item, known_build_fingerprint=spec_fingerprint(_spec()))
    assert reasons == ()


# ---------------------------------------------------------------------------
# Structured changes -> new spec (input never mutated)
# ---------------------------------------------------------------------------

def test_add_page_and_endpoint_produce_a_new_version():
    spec = _spec()
    original = copy.deepcopy(spec)
    changes = (
        FeedbackChange(action="add_endpoint", name="create_request", method="POST",
                       path="/api/requests", table="leave_requests"),
        FeedbackChange(action="add_page", name="New request", path="/requests/new",
                       endpoints=("create_request",)),
    )
    new_spec = apply_changes_to_spec(spec, changes)
    assert spec == original  # input untouched
    assert new_spec["version"] == 2
    assert {p["path"] for p in new_spec["pages"]} == {"/requests", "/requests/new"}
    assert {e["name"] for e in new_spec["endpoints"]} == {"list_requests", "create_request"}
    assert validate_spec(new_spec).valid


def test_add_table_with_columns():
    new_spec = apply_changes_to_spec(_spec(), (
        FeedbackChange(action="add_table", name="leave_types",
                       columns=(("id", "uuid"), ("label", "string"))),
    ))
    table = next(t for t in new_spec["tables"] if t["name"] == "leave_types")
    assert table["columns"] == [{"name": "id", "type": "uuid"},
                                {"name": "label", "type": "string"}]


def test_remove_page_endpoint_table():
    spec = _spec()
    new_spec = apply_changes_to_spec(spec, (
        FeedbackChange(action="remove_page", path="/requests"),
    ))
    assert new_spec["pages"] == []
    with pytest.raises(ValueError, match="unknown_endpoint"):
        apply_changes_to_spec(spec, (FeedbackChange(action="remove_endpoint", name="nope"),))
    with pytest.raises(ValueError, match="unknown_table"):
        apply_changes_to_spec(spec, (FeedbackChange(action="remove_table", name="nope"),))
    with pytest.raises(ValueError, match="unknown_page_path"):
        apply_changes_to_spec(spec, (FeedbackChange(action="remove_page", path="/nope"),))


def test_duplicate_add_is_refused():
    with pytest.raises(ValueError, match="duplicate_page_path"):
        apply_changes_to_spec(_spec(), (
            FeedbackChange(action="add_page", name="Again", path="/requests"),))
    with pytest.raises(ValueError, match="duplicate_endpoint"):
        apply_changes_to_spec(_spec(), (
            FeedbackChange(action="add_endpoint", name="list_requests", method="GET",
                           path="/api/requests", table="leave_requests"),))
    with pytest.raises(ValueError, match="duplicate_table"):
        apply_changes_to_spec(_spec(), (
            FeedbackChange(action="add_table", name="leave_requests", columns=(("id", "uuid"),)),))


def test_rename_and_purpose_change():
    new_spec = apply_changes_to_spec(_spec(), (
        FeedbackChange(action="rename_app", new_name="Time Off Tracker"),
        FeedbackChange(action="change_purpose", purpose="Track time off for the whole team."),
    ))
    assert new_spec["name"] == "Time Off Tracker"
    assert new_spec["purpose"] == "Track time off for the whole team."


# ---------------------------------------------------------------------------
# Feedback -> revision
# ---------------------------------------------------------------------------

def test_revision_revalidates_and_keeps_live_build():
    spec = _spec()
    fingerprint = spec_fingerprint(spec)
    revision = revise_from_feedback(
        spec,
        [_item(), FeedbackItem(slug="leave-tracker", build_fingerprint=fingerprint,
                               kind="praise", note="Looks good, keep it up.")],
        slug="leave-tracker", known_build_fingerprint=fingerprint)
    assert revision.status == "ready_for_test"
    assert revision.previous_fingerprint == fingerprint
    assert revision.new_fingerprint != fingerprint
    assert revision.new_fingerprint == spec_fingerprint(revision.new_spec)
    assert revision.previous_version == 1
    assert revision.new_version == 2
    assert revision.validation.valid
    assert revision.feedback_count == 2
    # The live app keeps serving the previous build until the new build is
    # tested, previewed and approved again.
    assert revision.live_build_fingerprint == fingerprint
    assert spec == _spec()  # input spec untouched


def test_revision_with_only_notes_has_nothing_to_revise():
    spec = _spec()
    with pytest.raises(ValueError, match="nothing_to_revise"):
        revise_from_feedback(
            spec,
            [_item(changes=())],
            slug="leave-tracker", known_build_fingerprint=spec_fingerprint(spec))


def test_revision_with_invalid_feedback_is_refused():
    spec = _spec()
    bad = _item(note="api_key=sk-abcdefgh12345678")
    with pytest.raises(ValueError, match="feedback_contains_secret"):
        revise_from_feedback(spec, [bad], slug="leave-tracker",
                             known_build_fingerprint=spec_fingerprint(spec))


def test_revision_that_breaks_the_spec_is_refused():
    spec = _spec()
    # A page referencing an endpoint that does not exist: slice 1 must refuse it.
    bad = _item(changes=(FeedbackChange(action="add_page", name="Broken",
                                        path="/broken", endpoints=("missing_endpoint",)),))
    with pytest.raises(ValueError, match="revision_invalid"):
        revise_from_feedback(spec, [bad], slug="leave-tracker",
                             known_build_fingerprint=spec_fingerprint(spec))


def test_revision_for_the_wrong_build_is_refused():
    spec = _spec()
    with pytest.raises(ValueError, match="feedback_attaches_to_unknown_build"):
        revise_from_feedback(spec, [_item()], slug="leave-tracker",
                             known_build_fingerprint="f" * 64)


def test_revision_accepts_a_build_fingerprint_distinct_from_the_spec():
    # Slice 6 integration: the build fingerprint is the SHA-256 of the
    # generated file set (slice 2), NOT the spec fingerprint. A revision
    # must still be possible when the two differ — the item attaches to
    # the build, the spec must match the build's recorded spec fingerprint.
    spec = _spec()
    build_hash = "a" * 64
    revision = revise_from_feedback(
        spec, [_item(build_fingerprint=build_hash)], slug="leave-tracker",
        known_build_fingerprint=build_hash,
        known_spec_fingerprint=spec_fingerprint(spec))
    assert revision.status == "ready_for_test"
    assert revision.new_version == 2
    assert revision.live_build_fingerprint == spec_fingerprint(spec)


def test_revision_with_a_mismatched_spec_fingerprint_is_refused():
    spec = _spec()
    with pytest.raises(ValueError, match="feedback_attaches_to_unknown_build"):
        revise_from_feedback(
            spec, [_item(build_fingerprint="a" * 64)], slug="leave-tracker",
            known_build_fingerprint="a" * 64,
            known_spec_fingerprint="f" * 64)


def test_revision_that_adds_a_payment_endpoint_is_refused():
    spec = _spec()
    bad = _item(changes=(FeedbackChange(
        action="add_endpoint", name="pay_for_leave", method="POST",
        path="/api/pay", table="leave_requests"),))
    with pytest.raises(ValueError, match="revision_invalid"):
        revise_from_feedback(spec, [bad], slug="leave-tracker",
                             known_build_fingerprint=spec_fingerprint(spec))


def test_two_revisions_chain_versions():
    spec = _spec()
    fingerprint = spec_fingerprint(spec)
    first = revise_from_feedback(
        spec, [_item()], slug="leave-tracker", known_build_fingerprint=fingerprint)
    second = revise_from_feedback(
        first.new_spec,
        [FeedbackItem(slug="leave-tracker", build_fingerprint=first.new_fingerprint,
                      kind="change_request", note="Rename it.",
                      changes=(FeedbackChange(action="rename_app", new_name="Time Off"),))],
        slug="leave-tracker", known_build_fingerprint=first.new_fingerprint)
    assert second.previous_version == 2
    assert second.new_version == 3
    assert second.previous_fingerprint == first.new_fingerprint
