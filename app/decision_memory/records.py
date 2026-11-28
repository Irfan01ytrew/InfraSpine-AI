"""Decision / organizational memory — pure record model (Step M, slice 1).

Stdlib only: no imports from ``app.platform``, no DB, no network. A decision
record is inert data. Recording, citing or superseding a decision never
grants anything, never changes an approval, never moves money and never
changes a payee — anything privileged (supersede, revoke, sensitive kinds)
is enforced by the service layer (slice 2) through ``app/platform``
approvals. This module only validates shapes and graphs, computes a
content fingerprint, and filters evidence for a reader.

Design notes (from the Step M plan):

- A decision is org-scoped and append-only: it is never edited or
  deleted; it is superseded by a new decision and history stays readable.
  ``supersede_record`` returns a *new* record; the old one is untouched.
- Evidence is stored as references (kind + id + label), never as copied
  text, so a reader who may not read the evidence never learns its
  contents from the decision. ``visible_evidence`` drops what the reader
  may not read (deny by default) and counts what was hidden.
- Rationale is untrusted text. Instruction-like language in it is
  flagged advisory-only by ``flag_suspicious`` — it is never executed,
  never interpreted as an instruction, and never changes what an approval
  covers. A flagged record is still a valid record.
- ``decision_fingerprint`` is a deterministic SHA-256 over the canonical
  record, so a decision can be referenced exactly and a later reader can
  prove the record they see is the one that was decided.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime

__all__ = [
    "DECISION_KINDS",
    "DECISION_STATUSES",
    "EVIDENCE_KINDS",
    "DecisionRecord",
    "EvidenceRef",
    "decision_fingerprint",
    "flag_suspicious",
    "supersede_record",
    "validate_decision",
    "validate_graph",
    "visible_evidence",
]

DECISION_KINDS = (
    "policy",
    "finance",
    "operations",
    "supplier",
    "people",
    "technology",
    "other",
)

DECISION_STATUSES = ("active", "superseded", "revoked", "expired")

EVIDENCE_KINDS = (
    "document",
    "governance_proposal",
    "governance_decision",
    "simulation",
    "twin_snapshot",
    "continuity_drill",
    "recovery_plan",
    "audit_event",
    "external",
)

MAX_TITLE_CHARS = 200
MAX_RATIONALE_CHARS = 4000
MAX_ALTERNATIVE_CHARS = 500
MAX_ALTERNATIVES = 20
MAX_EVIDENCE = 50
MAX_LABEL_CHARS = 200
MAX_REF_CHARS = 512
MAX_LINKS = 20

# Instruction-like language that must never be treated as an instruction.
# Advisory only: a match sets ``needs_review``, it never blocks recording
# and never changes what the record means. Phrases are matched as
# substrings, case-insensitively, on word boundaries where they matter.
_SUSPICIOUS_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior|earlier)\s+instructions", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|earlier)\s+instructions", re.I),
    re.compile(r"approve\s+all\b", re.I),
    re.compile(r"approve\s+every\b", re.I),
    re.compile(r"skip\s+approval\b", re.I),
    re.compile(r"bypass\s+(approval|permission|access|review)\b", re.I),
    re.compile(r"grant\s+(me|us|admin|access|permission)\b", re.I),
    re.compile(r"override\s+the\s+(approval|policy|permission|rules)\b", re.I),
    re.compile(r"\byou\s+are\s+now\b", re.I),
    re.compile(r"new\s+system\s+prompt\b", re.I),
    re.compile(r"reveal\s+(the\s+)?(password|secret|api\s*key|token)\b", re.I),
    re.compile(r"delete\s+all\b", re.I),
    re.compile(r"pay\s+(everyone|anyone|all)\b", re.I),
)


@dataclass(frozen=True)
class EvidenceRef:
    """One piece of evidence for a decision: a reference, never copied text.

    ``kind`` names the source system, ``ref`` is that system's id for the
    item (a document id, a proposal id, a simulation id, a URL for
    ``external``), and ``label`` is a short human-readable description
    written by the decider. The label must not quote restricted content —
    the service layer (slice 2) enforces that at record time; this module
    only bounds its length.
    """

    kind: str
    ref: str
    label: str


@dataclass(frozen=True)
class DecisionRecord:
    """One organizational decision, org-scoped and append-only."""

    id: str
    scope_id: str
    title: str
    rationale: str
    kind: str = "other"
    status: str = "active"
    decided_by: str = ""
    decided_at: datetime | None = None
    alternatives_considered: tuple[str, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    supersedes: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    review_at: date | datetime | None = None
    superseded_by: str | None = None
    superseded_at: datetime | None = None
    needs_review: bool = False


def _is_date(value: object) -> bool:
    return isinstance(value, (date, datetime))


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def validate_evidence(ref: EvidenceRef) -> list[str]:
    """Validate one evidence reference; empty list means valid."""
    errors: list[str] = []
    if not isinstance(ref, EvidenceRef):
        return ["evidence entry is not an EvidenceRef"]
    if ref.kind not in EVIDENCE_KINDS:
        errors.append(f"evidence kind {ref.kind!r} is not one of {sorted(EVIDENCE_KINDS)}")
    if not isinstance(ref.ref, str) or not ref.ref.strip() or len(ref.ref) > MAX_REF_CHARS:
        errors.append("evidence ref must be a non-empty string of at most "
                      f"{MAX_REF_CHARS} characters")
    if not isinstance(ref.label, str) or not ref.label.strip() or len(ref.label) > MAX_LABEL_CHARS:
        errors.append("evidence label must be a non-empty string of at most "
                      f"{MAX_LABEL_CHARS} characters")
    return errors


def validate_decision(record: DecisionRecord) -> list[str]:
    """Validate one decision record in isolation; empty list means valid.

    Graph rules (refs resolving, cycles, status of linked decisions) live
    in ``validate_graph`` — this function only checks the record itself.
    """
    errors: list[str] = []
    if not isinstance(record, DecisionRecord):
        return ["not a DecisionRecord"]
    if not isinstance(record.id, str) or not record.id.strip() or len(record.id) > 64:
        errors.append("id must be a non-empty string of at most 64 characters")
    if not isinstance(record.scope_id, str) or not record.scope_id.strip():
        errors.append("scope_id must be a non-empty string")
    if not isinstance(record.title, str) or not 1 <= len(record.title.strip()) <= MAX_TITLE_CHARS:
        errors.append(f"title must be 1–{MAX_TITLE_CHARS} characters")
    if not isinstance(record.rationale, str) or not 1 <= len(record.rationale.strip()) <= MAX_RATIONALE_CHARS:
        errors.append(f"rationale must be 1–{MAX_RATIONALE_CHARS} characters")
    if record.kind not in DECISION_KINDS:
        errors.append(f"kind {record.kind!r} is not one of {sorted(DECISION_KINDS)}")
    if record.status not in DECISION_STATUSES:
        errors.append(f"status {record.status!r} is not one of {sorted(DECISION_STATUSES)}")
    if not isinstance(record.decided_by, str) or not record.decided_by.strip():
        errors.append("decided_by must be a non-empty string")
    if not isinstance(record.decided_at, datetime):
        errors.append("decided_at must be a datetime")
    elif record.decided_at.tzinfo is None:
        errors.append("decided_at must be timezone-aware")
    if not isinstance(record.alternatives_considered, (tuple, list)):
        errors.append("alternatives_considered must be a list of strings")
    else:
        if len(record.alternatives_considered) > MAX_ALTERNATIVES:
            errors.append(f"at most {MAX_ALTERNATIVES} alternatives may be listed")
        for alt in record.alternatives_considered:
            if not isinstance(alt, str) or not 1 <= len(alt.strip()) <= MAX_ALTERNATIVE_CHARS:
                errors.append(f"each alternative must be 1–{MAX_ALTERNATIVE_CHARS} characters")
                break
    if not isinstance(record.evidence, (tuple, list)):
        errors.append("evidence must be a list of EvidenceRef")
    else:
        if len(record.evidence) > MAX_EVIDENCE:
            errors.append(f"at most {MAX_EVIDENCE} evidence references may be listed")
        seen: set[tuple[str, str]] = set()
        for ref in record.evidence:
            errors.extend(validate_evidence(ref))
            if isinstance(ref, EvidenceRef):
                key = (ref.kind, ref.ref)
                if key in seen:
                    errors.append(f"duplicate evidence reference {key}")
                seen.add(key)
    for field_name in ("supersedes", "depends_on"):
        links = getattr(record, field_name)
        if not isinstance(links, (tuple, list)):
            errors.append(f"{field_name} must be a list of decision ids")
            continue
        if len(links) > MAX_LINKS:
            errors.append(f"at most {MAX_LINKS} {field_name} links may be listed")
        if len(set(links)) != len(links):
            errors.append(f"{field_name} contains duplicate ids")
        for link in links:
            if not isinstance(link, str) or not link.strip():
                errors.append(f"{field_name} entries must be non-empty strings")
                break
    if record.id in set(record.supersedes or ()):  # type: ignore[arg-type]
        errors.append("a decision cannot supersede itself")
    if record.id in set(record.depends_on or ()):  # type: ignore[arg-type]
        errors.append("a decision cannot depend on itself")
    if set(record.supersedes or ()) & set(record.depends_on or ()):  # type: ignore[arg-type]
        errors.append("a decision id cannot be both superseded and depended on")
    if record.review_at is not None:
        if not _is_date(record.review_at):
            errors.append("review_at must be a date or datetime")
        elif isinstance(record.decided_at, datetime) and _as_date(record.review_at) < record.decided_at.date():
            errors.append("review_at must not be before the decision date")
    if record.status == "superseded":
        if not record.superseded_by:
            errors.append("a superseded decision must name its superseder")
        if not isinstance(record.superseded_at, datetime):
            errors.append("a superseded decision must have a superseded_at datetime")
        elif record.superseded_at.tzinfo is None:
            errors.append("superseded_at must be timezone-aware")
        elif isinstance(record.decided_at, datetime) and record.superseded_at < record.decided_at:
            errors.append("superseded_at must not be before decided_at")
    else:
        if record.superseded_by is not None:
            errors.append(f"a {record.status} decision must not name a superseder")
        if record.superseded_at is not None:
            errors.append(f"a {record.status} decision must not have a superseded_at")
    if not isinstance(record.needs_review, bool):
        errors.append("needs_review must be a boolean")
    return errors


def flag_suspicious(record: DecisionRecord) -> bool:
    """True when the title or rationale contains instruction-like language.

    Advisory only: a flagged record is still a valid, inert record. The
    flag exists so reviewers (slice 5) can spot rationale text that tries
    to read as an instruction — it is never executed and never changes
    what an approval covers.
    """
    text = f"{record.title}\n{record.rationale}"
    return any(pattern.search(text) for pattern in _SUSPICIOUS_PATTERNS)


def _canonical(record: DecisionRecord) -> dict:
    """Canonical JSON-able form of a record for fingerprinting."""
    return {
        "id": record.id,
        "scope_id": record.scope_id,
        "title": record.title,
        "rationale": record.rationale,
        "kind": record.kind,
        "status": record.status,
        "decided_by": record.decided_by,
        "decided_at": record.decided_at.isoformat() if record.decided_at else None,
        "alternatives_considered": sorted(record.alternatives_considered),
        "evidence": sorted(
            ({"kind": ref.kind, "ref": ref.ref, "label": ref.label} for ref in record.evidence),
            key=lambda item: (item["kind"], item["ref"]),
        ),
        "supersedes": sorted(record.supersedes),
        "depends_on": sorted(record.depends_on),
        "review_at": record.review_at.isoformat() if _is_date(record.review_at) else None,
        "superseded_by": record.superseded_by,
        "superseded_at": record.superseded_at.isoformat() if record.superseded_at else None,
        "needs_review": record.needs_review,
    }


def decision_fingerprint(record: DecisionRecord) -> str:
    """Deterministic SHA-256 hex digest of the canonical record.

    Identical records (regardless of list ordering) share a fingerprint;
    any change to any field changes it. A later reader can prove the
    record they see is the one that was decided.
    """
    payload = json.dumps(_canonical(record), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def visible_evidence(
    record: DecisionRecord,
    readable_refs: set[tuple[str, str]],
) -> tuple[list[EvidenceRef], int]:
    """Evidence the reader may see, plus how many references were hidden.

    ``readable_refs`` is the set of ``(kind, ref)`` pairs the reader is
    allowed to read. Anything not in the set is dropped — deny by default,
    including ``external`` references. The hidden count lets the UI say
    "2 sources hidden" without revealing what they are.
    """
    visible = [ref for ref in record.evidence if (ref.kind, ref.ref) in readable_refs]
    return visible, len(record.evidence) - len(visible)


def supersede_record(
    record: DecisionRecord,
    *,
    superseding_id: str,
    at: datetime,
) -> DecisionRecord:
    """Return the record as superseded by ``superseding_id`` at ``at``.

    Only an active decision can be superseded. The input record is
    untouched (records are frozen); the caller owns the approval check
    and the creation of the superseding record.
    """
    if record.status != "active":
        raise ValueError(f"only an active decision can be superseded, got {record.status!r}")
    if not isinstance(superseding_id, str) or not superseding_id.strip():
        raise ValueError("superseding_id must be a non-empty string")
    if superseding_id == record.id:
        raise ValueError("a decision cannot supersede itself")
    if not isinstance(at, datetime) or at.tzinfo is None:
        raise ValueError("at must be a timezone-aware datetime")
    if record.decided_at is not None and at < record.decided_at:
        raise ValueError("superseded_at must not be before decided_at")
    return replace(record, status="superseded", superseded_by=superseding_id, superseded_at=at)


def validate_graph(records: list[DecisionRecord]) -> list[str]:
    """Validate a whole set of decision records; empty list means valid.

    Checks, in addition to each record's own validation: ids are unique;
    every supersedes/depends_on link resolves to a record in the same
    scope; the combined link graph has no cycles; each decision is
    superseded by at most one other decision; a supersedes edge points
    at a superseded record naming this record as its superseder; a
    depends_on edge from an active record points at an active record.
    Edges out of a superseded or revoked record are history, not live
    dependencies, so their targets need not still be active — otherwise
    superseding a decision that a revoked decision once depended on
    could never validate.
    """
    errors: list[str] = []
    by_id: dict[str, DecisionRecord] = {}
    for record in records:
        for error in validate_decision(record):
            errors.append(f"{record.id}: {error}")
        if record.id in by_id:
            errors.append(f"duplicate decision id {record.id!r}")
        by_id.setdefault(record.id, record)

    superseded_by_count: dict[str, int] = {}
    for record in records:
        for target in record.supersedes:
            superseded_by_count[target] = superseded_by_count.get(target, 0) + 1
    for target, count in superseded_by_count.items():
        if count > 1:
            errors.append(f"decision {target!r} is superseded by {count} decisions")

    for record in records:
        for field_name, targets in (("supersedes", record.supersedes), ("depends_on", record.depends_on)):
            for target in targets:
                other = by_id.get(target)
                if other is None:
                    errors.append(f"{record.id}: {field_name} target {target!r} does not exist")
                elif other.scope_id != record.scope_id:
                    errors.append(
                        f"{record.id}: {field_name} target {target!r} is in a different scope"
                    )
                elif field_name == "supersedes":
                    if other.status != "superseded" or other.superseded_by != record.id:
                        errors.append(
                            f"{record.id}: supersedes target {target!r} must be superseded "
                            f"by this decision"
                        )
                elif record.status == "active" and other.status != "active":
                    errors.append(
                        f"{record.id}: depends_on target {target!r} must be active, "
                        f"got {other.status!r}"
                    )

    # Cycle check over the combined supersedes + depends_on graph.
    graph: dict[str, list[str]] = {
        record.id: list(record.supersedes) + list(record.depends_on) for record in records
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, path: list[str]) -> None:
        if node in visited or node not in graph:
            return
        if node in visiting:
            errors.append(f"dependency cycle: {' -> '.join(path + [node])}")
            return
        visiting.add(node)
        for nxt in graph[node]:
            visit(nxt, path + [node])
        visiting.discard(node)
        visited.add(node)

    for record in records:
        visit(record.id, [])
    return errors


def new_decision_id() -> str:
    """Generate a fresh decision id (uuid4 hex)."""
    return uuid.uuid4().hex
