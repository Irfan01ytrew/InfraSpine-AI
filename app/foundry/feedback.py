"""Step AM (Self-Evolving Software Foundry), slice 5 — feedback → improve loop.

User feedback attaches to one spec version and produces a *new* spec
revision. The revision re-enters the pipeline at slice 1 (re-validate),
then slice 2 (re-generate), slice 3 (re-test + re-preview) and slice 4
(re-approve) — a live app is never patched in place.

Pure functions only (stdlib). Feedback text is untrusted data: it is stored
as a note and can never, by itself, change a spec. Only *structured*
change requests (add/remove a page, endpoint or table; rename; change the
purpose) alter the spec, and the resulting spec is re-validated with slice
1 — an unsafe or inconsistent revision is refused outright instead of
being built.

Safety, enforced structurally:
* Feedback containing anything that looks like a secret or a destructive
  instruction is rejected with a named reason (slice 1 scanners); it is
  never applied and never stored as a change.
* A revision is built only from structured changes. Free text with no
  structured change produces no revision (`nothing_to_revise`).
* Every revision is re-validated. If the new spec is invalid (dangling
  reference, duplicate name, payment endpoint, ...), the revision is
  refused — the previous build keeps serving.
* The live build is untouched by construction: `revise_from_feedback()`
  returns a new spec dict and a `Revision` whose `live_build_fingerprint`
  is the previous build. Nothing here mutates the input spec, the previous
  build, or any deployed record.
* Feedback attaches to exactly one build fingerprint. Feedback for an
  unknown build is refused (`feedback_attaches_to_unknown_build`).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from app.foundry.spec import (
    SpecValidation,
    find_destructive_patterns,
    find_secret_patterns,
    spec_fingerprint,
    validate_spec,
)

__all__ = [
    "FeedbackChange",
    "FeedbackItem",
    "Revision",
    "apply_changes_to_spec",
    "revise_from_feedback",
    "scan_feedback_text",
    "validate_feedback_item",
]

FEEDBACK_KINDS = ("bug", "change_request", "praise", "question")

CHANGE_ACTIONS = (
    "add_page",
    "add_endpoint",
    "add_table",
    "remove_page",
    "remove_endpoint",
    "remove_table",
    "rename_app",
    "change_purpose",
)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FeedbackChange:
    """One structured change request. Only these fields may alter a spec.

    `columns` is a tuple of (name, type) pairs for `add_table`.
    """

    action: str
    name: str | None = None
    path: str | None = None
    method: str | None = None
    table: str | None = None
    columns: tuple[tuple[str, str], ...] = ()
    endpoints: tuple[str, ...] = ()
    new_name: str | None = None
    purpose: str | None = None


@dataclass(frozen=True)
class FeedbackItem:
    """One piece of user feedback on one build of one app.

    `note` is free text: stored for a human to read, never executed and
    never applied to the spec. `changes` is what the revision is built from.
    """

    slug: str
    build_fingerprint: str
    kind: str
    note: str = ""
    changes: tuple[FeedbackChange, ...] = ()
    author: str | None = None


@dataclass(frozen=True)
class Revision:
    """A new spec revision produced from feedback.

    `new_spec` must still pass slices 2–4 (generate, test + preview,
    approve) before anything deploys. `live_build_fingerprint` is the
    build the live app keeps serving until the new build is approved.
    """

    slug: str
    previous_fingerprint: str
    new_fingerprint: str
    previous_version: int
    new_version: int
    new_spec: dict
    validation: SpecValidation
    feedback_count: int
    status: str = "ready_for_test"  # must re-test + re-preview + re-approve
    live_build_fingerprint: str = ""


# ---------------------------------------------------------------------------
# Feedback text is untrusted: scan it, never obey it
# ---------------------------------------------------------------------------

def scan_feedback_text(note: str) -> tuple[str, ...]:
    """Named reasons a feedback note must be refused. Empty = acceptable
    as a human-readable note (it still never changes the spec)."""
    reasons: list[str] = []
    if find_secret_patterns(note or ""):
        reasons.append("feedback_contains_secret")
    if find_destructive_patterns(note or ""):
        reasons.append("feedback_contains_destructive_instruction")
    return tuple(reasons)


def validate_feedback_item(item: FeedbackItem, *, known_build_fingerprint: str) -> tuple[str, ...]:
    """Named reasons a feedback item cannot be used for a revision.
    Empty = usable."""
    reasons: list[str] = []
    if item.kind not in FEEDBACK_KINDS:
        reasons.append(f"invalid_feedback_kind:{item.kind}")
    if item.build_fingerprint != known_build_fingerprint:
        reasons.append("feedback_attaches_to_unknown_build")
    if not item.note.strip() and not item.changes:
        reasons.append("feedback_empty")
    reasons.extend(scan_feedback_text(item.note))
    for change in item.changes:
        if change.action not in CHANGE_ACTIONS:
            reasons.append(f"invalid_change_action:{change.action}")
    return tuple(reasons)


# ---------------------------------------------------------------------------
# Structured changes -> new spec dict (never mutates the input)
# ---------------------------------------------------------------------------

def apply_changes_to_spec(spec: dict, changes) -> dict:
    """Apply structured changes to a *copy* of the spec and bump its
    version. Raises ValueError on a change that cannot apply (removing
    something that is not there, adding a duplicate name). The caller must
    re-validate the result with slice 1 before building from it."""
    new_spec = copy.deepcopy(spec)
    new_spec.setdefault("pages", [])
    new_spec.setdefault("endpoints", [])
    new_spec.setdefault("tables", [])
    for change in changes:
        if change.action == "add_page":
            if any(p.get("path") == change.path for p in new_spec["pages"]):
                raise ValueError(f"duplicate_page_path:{change.path}")
            new_spec["pages"].append({"name": change.name, "path": change.path,
                                      "endpoints": list(change.endpoints)})
        elif change.action == "add_endpoint":
            if any(e.get("name") == change.name for e in new_spec["endpoints"]):
                raise ValueError(f"duplicate_endpoint:{change.name}")
            new_spec["endpoints"].append({"name": change.name, "method": change.method,
                                          "path": change.path, "table": change.table})
        elif change.action == "add_table":
            if any(t.get("name") == change.name for t in new_spec["tables"]):
                raise ValueError(f"duplicate_table:{change.name}")
            new_spec["tables"].append(
                {"name": change.name,
                 "columns": [{"name": c[0], "type": c[1]} for c in change.columns]})
        elif change.action == "remove_page":
            before = len(new_spec["pages"])
            new_spec["pages"] = [p for p in new_spec["pages"] if p.get("path") != change.path]
            if len(new_spec["pages"]) == before:
                raise ValueError(f"unknown_page_path:{change.path}")
        elif change.action == "remove_endpoint":
            before = len(new_spec["endpoints"])
            new_spec["endpoints"] = [e for e in new_spec["endpoints"]
                                     if e.get("name") != change.name]
            if len(new_spec["endpoints"]) == before:
                raise ValueError(f"unknown_endpoint:{change.name}")
        elif change.action == "remove_table":
            before = len(new_spec["tables"])
            new_spec["tables"] = [t for t in new_spec["tables"] if t.get("name") != change.name]
            if len(new_spec["tables"]) == before:
                raise ValueError(f"unknown_table:{change.name}")
        elif change.action == "rename_app":
            new_spec["name"] = change.new_name
        elif change.action == "change_purpose":
            new_spec["purpose"] = change.purpose
        else:  # pragma: no cover - validate_feedback_item rejects these first
            raise ValueError(f"invalid_change_action:{change.action}")
    new_spec["version"] = int(new_spec.get("version") or 1) + 1
    return new_spec


# ---------------------------------------------------------------------------
# Feedback -> revision (re-validated, never live-patched)
# ---------------------------------------------------------------------------

def revise_from_feedback(spec: dict, feedback_items, *, slug: str,
                         known_build_fingerprint: str,
                         known_spec_fingerprint: str | None = None) -> Revision:
    """Build a new spec revision from feedback items, in order.

    Every item is validated first (unknown build, bad kind, secret or
    destructive text, invalid change action). All structured changes are
    applied, then the new spec is re-validated with slice 1 — an invalid
    revision is refused. Items with no structured changes are counted but
    change nothing; if no item carries a change, there is nothing to
    revise and ValueError is raised.

    The build fingerprint and the spec fingerprint are different values:
    the build fingerprint is the SHA-256 of the generated file set
    (slice 2), the spec fingerprint is the SHA-256 of the spec itself
    (slice 1). The items attach to the *build* (checked against
    `known_build_fingerprint`), while the spec being revised must match
    the build's recorded spec fingerprint (`known_spec_fingerprint`).
    Callers that only know the spec fingerprint may pass it as
    `known_build_fingerprint` and omit `known_spec_fingerprint`.
    """
    previous_fingerprint = spec_fingerprint(spec)
    expected_spec = (known_spec_fingerprint if known_spec_fingerprint is not None
                     else known_build_fingerprint)
    if expected_spec != previous_fingerprint:
        raise ValueError("feedback_attaches_to_unknown_build: the spec given "
                         "does not match the build the feedback attaches to.")
    changes: list[FeedbackChange] = []
    for item in feedback_items:
        reasons = validate_feedback_item(item, known_build_fingerprint=known_build_fingerprint)
        if reasons:
            raise ValueError("invalid_feedback: " + "; ".join(reasons))
        changes.extend(item.changes)
    if not changes:
        raise ValueError("nothing_to_revise: feedback has no structured changes.")
    new_spec = apply_changes_to_spec(spec, changes)
    validation = validate_spec(new_spec)
    if not validation.valid:
        raise ValueError("revision_invalid: "
                         + "; ".join(f"{i.code} ({i.field})" for i in validation.issues))
    new_fingerprint = spec_fingerprint(new_spec)
    previous_version = int(spec.get("version") or 1)
    return Revision(
        slug=slug,
        previous_fingerprint=previous_fingerprint,
        new_fingerprint=new_fingerprint,
        previous_version=previous_version,
        new_version=int(new_spec.get("version") or 1),
        new_spec=new_spec,
        validation=validation,
        feedback_count=len(feedback_items),
        live_build_fingerprint=previous_fingerprint,
    )
