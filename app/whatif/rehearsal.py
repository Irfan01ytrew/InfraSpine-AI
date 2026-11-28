"""Rehearsal runner (Step AL slice 5).

A rehearsal practises a simulated recovery plan step by step without
touching any real system. Pure functions over plain dicts: no I/O, no
database, no network, no secrets.

Invariants enforced on every record, by construction and by
:func:`validate_rehearsal`:

- ``dry_run`` is always ``True`` and ``side_effects`` is always
  ``[]`` — a rehearsal cannot execute anything, by construction.
- Every step has ``practice_only: True``.
- A step is ``pending``, ``done`` (checked off by a named person,
  optionally with a finding) or ``skipped`` (with a reason) — never
  executed, never granted, never approved, never paid.
- A rehearsal completes only when no step is still pending; the
  completion records who completed it and an optional summary.

Functions never mutate their input: each transition returns a new
record (deep copy). The service layer persists the returned record.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime

from app.whatif.simulator import SCENARIO_KINDS

STEP_STATUSES = frozenset({"pending", "done", "skipped"})
RECORD_STATUSES = frozenset({"in_progress", "completed"})


def _now_iso(now=None):
    if now is not None:
        if not isinstance(now, str) or not now:
            raise ValueError("now must be a non-empty ISO timestamp string.")
        return now
    return datetime.now(UTC).isoformat()


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _steps_from_result(result):
    """Numbered practice steps for a simulation result.

    Prefers the result's own ``rehearsal`` block (``simulate(...,
    rehearse=True)``); otherwise rebuilds the same numbering from the
    recovery waves. Either way the steps are practice-only.
    """
    rehearsal = result.get("rehearsal") if isinstance(result, dict) else None
    if isinstance(rehearsal, dict) and isinstance(rehearsal.get("steps"), list):
        steps = rehearsal["steps"]
    else:
        plan = result.get("recovery_plan") if isinstance(result, dict) else None
        _require(isinstance(plan, dict), "Simulation result needs a 'recovery_plan' dict.")
        waves = plan.get("waves")
        _require(isinstance(waves, list), "Simulation recovery_plan needs a 'waves' list.")
        steps = []
        number = 0
        for wave_index, wave in enumerate(waves, start=1):
            _require(isinstance(wave, list), f"Recovery wave {wave_index} must be a list.")
            for step in wave:
                _require(isinstance(step, dict), "Recovery steps must be dicts.")
                number += 1
                steps.append({
                    "number": number,
                    "wave": wave_index,
                    "entity_id": step.get("entity_id"),
                    "entity_name": step.get("entity_name", step.get("entity_id")),
                    "action": step.get("action"),
                    "owner": step.get("owner"),
                    "note": step.get("note", ""),
                })
    normalized = []
    for index, step in enumerate(steps, start=1):
        _require(isinstance(step, dict), "Rehearsal steps must be dicts.")
        number = step.get("number", index)
        _require(isinstance(number, int) and number == index,
                 "Rehearsal steps must be numbered 1..n in order.")
        entity_id = step.get("entity_id")
        _require(isinstance(entity_id, str) and entity_id,
                 "Rehearsal step needs a non-empty string 'entity_id'.")
        action = step.get("action")
        _require(isinstance(action, str) and action,
                 "Rehearsal step needs a non-empty string 'action'.")
        normalized.append({
            "number": number,
            "wave": step.get("wave"),
            "entity_id": entity_id,
            "entity_name": step.get("entity_name", entity_id),
            "action": action,
            "owner": step.get("owner"),
            "note": step.get("note", ""),
            "practice_only": True,
            "status": "pending",
            "checked_off_by": None,
            "checked_off_by_name": None,
            "checked_off_at": None,
            "finding": None,
            "skipped_reason": None,
        })
    _require(normalized, "A rehearsal needs at least one step.")
    return normalized


def start_rehearsal(simulation_result, *, rehearsed_by, rehearsed_by_name=None,
                    title=None, now=None):
    """Start a rehearsal record for a simulation result.

    ``rehearsed_by`` is the user id (UUID string) of the person
    practising. The record is data only: it stores who practised,
    when, which steps, and what was found — never an execution.
    """
    _require(isinstance(simulation_result, dict), "Simulation result must be a dict.")
    scenario = simulation_result.get("scenario")
    _require(isinstance(scenario, dict), "Simulation result needs a 'scenario' dict.")
    kind = scenario.get("kind")
    _require(kind in SCENARIO_KINDS, f"Unknown scenario kind {kind!r}.")
    _require(isinstance(rehearsed_by, str) and rehearsed_by,
            "rehearsed_by must be a non-empty user id string.")
    if title is not None:
        _require(isinstance(title, str) and 3 <= len(title) <= 180,
                 "title must be 3-180 characters.")
    steps = _steps_from_result(simulation_result)
    return {
        "id": str(uuid.uuid4()),
        "tenant_id": None,
        "title": title or f"Rehearsal: {scenario.get('name', kind)}",
        "scenario": {
            "kind": kind,
            "name": scenario.get("name", kind),
            "template_id": scenario.get("template_id"),
            "seed_ids": list(scenario.get("seed_ids") or []),
            "seed_names": list(scenario.get("seed_names") or []),
            "horizon_days": scenario.get("horizon_days"),
        },
        "status": "in_progress",
        "dry_run": True,
        "side_effects": [],
        "practice_only": True,
        "steps": steps,
        "started_by": rehearsed_by,
        "started_by_name": rehearsed_by_name,
        "started_at": _now_iso(now),
        "completed_at": None,
        "completed_by": None,
        "completed_by_name": None,
        "summary": None,
        "findings": [],
    }


def _find_step(record, number):
    _require(isinstance(number, int), "step_number must be an integer.")
    for step in record["steps"]:
        if step["number"] == number:
            return step
    raise ValueError(f"No rehearsal step numbered {number}.")


def _require_open(record):
    _require(record.get("status") == "in_progress",
             "This rehearsal is already completed; its record is read-only.")


def check_off_step(record, number, *, by, by_name=None, finding=None, now=None):
    """Mark one step done (practised), optionally with a finding.

    Returns a new record; the input is not mutated. ``finding`` is a
    short note about what the practice showed (e.g. "backup person
    was on leave — plan needs a second backup").
    """
    _require(isinstance(record, dict), "Rehearsal record must be a dict.")
    validate_rehearsal(record)
    _require_open(record)
    _require(isinstance(by, str) and by, "by must be a non-empty user id string.")
    if finding is not None:
        _require(isinstance(finding, str) and 1 <= len(finding) <= 2000,
                 "finding must be 1-2000 characters.")
    updated = copy.deepcopy(record)
    step = _find_step(updated, number)
    _require(step["status"] == "pending",
             f"Step {number} is already {step['status']}; only pending steps can be checked off.")
    step["status"] = "done"
    step["checked_off_by"] = by
    step["checked_off_by_name"] = by_name
    step["checked_off_at"] = _now_iso(now)
    step["finding"] = finding
    if finding:
        updated["findings"] = (updated.get("findings") or []) + [finding]
    return updated


def skip_step(record, number, *, by, by_name=None, reason, now=None):
    """Mark one step skipped, with a mandatory reason.

    Skipping records that a step could not be practised (and why);
    it never executes the step.
    """
    _require(isinstance(record, dict), "Rehearsal record must be a dict.")
    validate_rehearsal(record)
    _require_open(record)
    _require(isinstance(by, str) and by, "by must be a non-empty user id string.")
    _require(isinstance(reason, str) and 5 <= len(reason) <= 1000,
             "Skipping a step needs a reason of 5-1000 characters.")
    updated = copy.deepcopy(record)
    step = _find_step(updated, number)
    _require(step["status"] == "pending",
             f"Step {number} is already {step['status']}; only pending steps can be skipped.")
    step["status"] = "skipped"
    step["checked_off_by"] = by
    step["checked_off_by_name"] = by_name
    step["checked_off_at"] = _now_iso(now)
    step["skipped_reason"] = reason
    return updated


def complete_rehearsal(record, *, by, by_name=None, summary=None, now=None):
    """Complete a rehearsal once no step is still pending.

    Returns a new record with status ``completed``. The record stays
    readable afterwards; its steps can no longer change.
    """
    _require(isinstance(record, dict), "Rehearsal record must be a dict.")
    validate_rehearsal(record)
    _require_open(record)
    _require(isinstance(by, str) and by, "by must be a non-empty user id string.")
    if summary is not None:
        _require(isinstance(summary, str) and 1 <= len(summary) <= 4000,
                 "summary must be 1-4000 characters.")
    pending = [s["number"] for s in record["steps"] if s["status"] == "pending"]
    _require(not pending,
             f"Cannot complete: step(s) {pending} are still pending. Check them off or skip them first.")
    updated = copy.deepcopy(record)
    updated["status"] = "completed"
    updated["completed_at"] = _now_iso(now)
    updated["completed_by"] = by
    updated["completed_by_name"] = by_name
    updated["summary"] = summary
    return updated


def validate_rehearsal(record):
    """Assert a rehearsal record's practice-only invariants.

    Raises ``ValueError`` on any violation: not a dry run, non-empty
    side effects, a step that is not practice-only, bad statuses,
    missing check-off data, or a completed record with pending steps.
    """
    _require(isinstance(record, dict), "Rehearsal record must be a dict.")
    _require(isinstance(record.get("id"), str) and record["id"], "Rehearsal needs a non-empty string 'id'.")
    _require(record.get("status") in RECORD_STATUSES,
             f"Rehearsal status must be one of {sorted(RECORD_STATUSES)}.")
    _require(record.get("dry_run") is True, "A rehearsal must be a dry run (dry_run must be True).")
    _require(record.get("side_effects") == [], "A rehearsal must have no side effects.")
    _require(record.get("practice_only") is True, "A rehearsal must be practice-only.")
    scenario = record.get("scenario")
    _require(isinstance(scenario, dict) and scenario.get("kind") in SCENARIO_KINDS,
             "Rehearsal needs a scenario with a known kind.")
    steps = record.get("steps")
    _require(isinstance(steps, list) and steps, "Rehearsal needs a non-empty 'steps' list.")
    for index, step in enumerate(steps, start=1):
        _require(isinstance(step, dict), "Rehearsal steps must be dicts.")
        _require(step.get("number") == index,
                 "Rehearsal steps must be numbered 1..n in order.")
        _require(step.get("practice_only") is True,
                 f"Step {index} must be practice-only.")
        _require(step.get("status") in STEP_STATUSES,
                 f"Step {index} status must be one of {sorted(STEP_STATUSES)}.")
        if step["status"] in ("done", "skipped"):
            _require(isinstance(step.get("checked_off_by"), str) and step["checked_off_by"],
                     f"Step {index} was {step['status']} but records no one who did it.")
            _require(isinstance(step.get("checked_off_at"), str) and step["checked_off_at"],
                     f"Step {index} was {step['status']} but records no time.")
        if step["status"] == "skipped":
            _require(isinstance(step.get("skipped_reason"), str) and step["skipped_reason"],
                     f"Step {index} was skipped but records no reason.")
    if record["status"] == "completed":
        pending = [s["number"] for s in steps if s["status"] == "pending"]
        _require(not pending, f"Completed rehearsal still has pending step(s): {pending}.")
        _require(record.get("completed_at"), "Completed rehearsal needs 'completed_at'.")
    else:
        _require(record.get("completed_at") is None,
                 "An in-progress rehearsal must not have 'completed_at'.")
    return True


def rehearsal_summary(record):
    """Counts + findings for one rehearsal record."""
    validate_rehearsal(record)
    steps = record["steps"]
    done = [s for s in steps if s["status"] == "done"]
    skipped = [s for s in steps if s["status"] == "skipped"]
    pending = [s for s in steps if s["status"] == "pending"]
    return {
        "id": record["id"],
        "title": record.get("title"),
        "status": record["status"],
        "total_steps": len(steps),
        "done_count": len(done),
        "skipped_count": len(skipped),
        "pending_count": len(pending),
        "skipped_reasons": [s["skipped_reason"] for s in skipped],
        "findings": list(record.get("findings") or []),
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
        "dry_run": True,
        "side_effects": [],
    }
