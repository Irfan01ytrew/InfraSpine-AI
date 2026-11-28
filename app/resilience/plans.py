"""Step AH slice 4 — recovery plans as data, and their workflow spec.

A recovery plan is built from a :func:`app.resilience.impact.score_disruption`
analysis: the caller picks which of the analysis's ranked recovery options to
run, and :func:`build_plan` turns them into an ordered list of steps. The plan
itself is pure data — it approves, dispatches, grants, restores, switches or
pays nothing. Execution lives in :mod:`app.resilience.runtime`, where every
step runs as a unified-engine workflow run with an exact-action approval
before it and ``execute_once`` with read-back verification around it.

Approval policy (see the plan doc §7 — owner approval is still open for the
sensitive kinds, so the code uses the strictest existing permission):

* ``switch_supplier`` steps are **sensitive**: they need a platform approval
  whose required permission is ``platform.manage`` (installation
  administrators / the owner only).
* Every other approval-bound step (``use_backup``,
  ``reassign_to_team_member``) needs a platform approval whose required
  permission is ``workflow.manage`` (department managers).
* ``manual_workaround`` and ``escalate`` need no approval; they still run
  through the workflow as a person-performed step so the run records who did
  them and when.
"""

from __future__ import annotations

import hashlib
import uuid

STEP_DUTY = "workflow.manage"
SENSITIVE_APPROVAL_PERMISSION = "platform.manage"
STEP_APPROVAL_PERMISSION = "workflow.manage"

# Recovery-option kinds that switch an external party or restore data.
# They are sensitive: only the owner / installation administrators may
# approve them, and the proposer can never approve their own step
# (separation of duties is enforced by ``app.platform.approvals``).
SENSITIVE_KINDS = frozenset({"switch_supplier"})

APPROVAL_HANDLER = "resilience.step_approval"
EXECUTE_HANDLER = "resilience.step_execute"

# Node ids in the generated spec.
MAX_STEPS = 50


def _plan_id() -> str:
    return f"plan-{uuid.uuid4().hex[:12]}"


def build_plan(analysis, chosen, *, plan_id=None):
    """Build a recovery plan from a disruption analysis.

    ``analysis`` is the dict returned by
    :func:`app.resilience.impact.score_disruption`. ``chosen`` is a list of
    1-based ranks (the ``rank`` values the analysis assigned to its
    ``recovery_options``), in the order the steps should run. Returns a plan
    dict::

        {"id": ..., "event": {...}, "severity": "high",
         "money_at_risk": {...},
         "steps": [{"id": "s1", "kind": "use_backup", "action": "...",
                    "covers": [...], "owner": "p-2", "eta_days": 1,
                    "requires_approval": True, "sensitive": False,
                    "approval_permission": "workflow.manage",
                    "status": "pending"}]}

    Every step keeps the option's action text, owner and ETA verbatim so the
    workflow run shows exactly what was approved. A step that needs approval
    names the permission the approver must hold.
    """
    if not isinstance(analysis, dict):
        raise ValueError("Analysis must be a dict.")
    options = analysis.get("recovery_options")
    if not isinstance(options, list) or not options:
        raise ValueError("Analysis has no recovery options to build a plan from.")
    if not isinstance(chosen, list) or not chosen:
        raise ValueError("Choose at least one recovery option to run.")
    if len(chosen) > MAX_STEPS:
        raise ValueError(f"A plan holds at most {MAX_STEPS} steps.")
    by_rank = {}
    for option in options:
        if not isinstance(option, dict) or "rank" not in option:
            raise ValueError("Every recovery option needs a rank.")
        by_rank[option["rank"]] = option
    seen = set()
    steps = []
    for position, rank in enumerate(chosen, start=1):
        if isinstance(rank, bool) or not isinstance(rank, int):
            raise ValueError(f"Chosen ranks must be integers, got {rank!r}.")
        if rank in seen:
            raise ValueError(f"Recovery option rank {rank} was chosen twice.")
        seen.add(rank)
        option = by_rank.get(rank)
        if option is None:
            raise ValueError(f"No recovery option with rank {rank}.")
        kind = option.get("kind")
        if not kind:
            raise ValueError("Every recovery option needs a kind.")
        requires_approval = bool(option.get("requires_approval"))
        sensitive = kind in SENSITIVE_KINDS
        steps.append(
            {
                "id": f"s{position}",
                "kind": kind,
                "action": option.get("action") or kind,
                "covers": list(option.get("covers") or []),
                "owner": option.get("owner"),
                "eta_days": option.get("eta_days"),
                "requires_approval": requires_approval,
                "sensitive": sensitive,
                "approval_permission": (
                    SENSITIVE_APPROVAL_PERMISSION if sensitive else STEP_APPROVAL_PERMISSION
                )
                if requires_approval
                else None,
                "status": "pending",
            }
        )
    return {
        "id": plan_id or _plan_id(),
        "event": dict(analysis.get("event") or {}),
        "severity": analysis.get("severity"),
        "money_at_risk": dict(analysis.get("money_at_risk") or {}),
        "steps": steps,
    }


def plan_fingerprint(plan) -> str:
    """Deterministic fingerprint of a plan (ids, kinds, actions, covers).

    Two plans with the same steps have the same fingerprint; it is shown on
    the plan so an approver can tell whether the plan changed after review.
    """
    canonical = [
        {
            "id": step["id"],
            "kind": step["kind"],
            "action": step["action"],
            "covers": sorted(step["covers"]),
            "owner": step["owner"],
            "eta_days": step["eta_days"],
            "requires_approval": step["requires_approval"],
        }
        for step in plan["steps"]
    ]
    encoded = repr(sorted(canonical, key=lambda item: item["id"])).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def step_subject(plan_id, step_id) -> str:
    """The platform subject for one plan step (approval + execution ledger)."""
    return f"resilience-plan:{plan_id}:{step_id}"


def step_action_params(plan, step, *, run_id=None):
    """Canonical params for a step's exact action (approval + execution).

    No secrets, no amounts: the action is "perform this recorded recovery
    step", and the money figures stay in the plan, never in the action.
    """
    params = {
        "plan_id": plan["id"],
        "step_id": step["id"],
        "kind": step["kind"],
        "covers": sorted(step["covers"]),
    }
    if run_id is not None:
        params["run_id"] = str(run_id)
    return params


def plan_spec(plan) -> dict:
    """The unified-engine definition for a recovery plan.

    One workflow run per plan, steps in order: ``start`` → for each plan
    step, an approval node (only when the step needs approval) followed by
    an execute node → ``end``. The approval node is an auto action handled
    by ``resilience.step_approval`` (it polls the step's platform approval);
    the execute node is a person-performed action handled by
    ``resilience.step_execute`` (permission ``workflow.manage``) that runs
    the step through ``execute_once``. The definition is a code template
    pinned by digest per plan (``origin='system'``); the run carries
    ``origin='resilience'`` and ``origin_ref=<plan id>``.

    Step data lives in the node ``values`` so every step is self-describing
    in the version spec; the only run input is the plan id.
    """
    if not isinstance(plan, dict) or not plan.get("id"):
        raise ValueError("Plan must be a dict with a non-empty 'id'.")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Plan must have at least one step.")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"A plan holds at most {MAX_STEPS} steps.")
    nodes = [
        {"id": "start", "type": "start", "label": "Recovery plan started", "x": 80, "y": 160},
    ]
    edges = []
    previous = "start"
    y = 160
    for step in steps:
        step_id = step["id"]
        values = {
            "plan_id": plan["id"],
            "step_id": step_id,
            "kind": step["kind"],
            "action": step["action"],
            "covers": list(step["covers"]),
            "owner": step["owner"],
            "eta_days": step["eta_days"],
            "requires_approval": step["requires_approval"],
            "sensitive": step["sensitive"],
        }
        if step["requires_approval"]:
            approve_id = f"approve-{step_id}"
            nodes.append(
                {
                    "id": approve_id,
                    "type": "action",
                    "label": f"Approve: {step['action'][:80]}",
                    "x": 300,
                    "y": y,
                    "risk": "critical" if step["sensitive"] else "high",
                    "timeout_seconds": 120,
                    "max_attempts": 5,
                    "config": {"handler": APPROVAL_HANDLER, "auto": True, "values": values},
                }
            )
            edges.append({"source": previous, "target": approve_id})
            previous = approve_id
        execute_id = f"exec-{step_id}"
        nodes.append(
            {
                "id": execute_id,
                "type": "action",
                "label": f"Perform: {step['action'][:80]}",
                "x": 520,
                "y": y,
                "risk": "critical" if step["sensitive"] else "high",
                "timeout_seconds": 120,
                "max_attempts": 5,
                "config": {
                    "handler": EXECUTE_HANDLER,
                    "permission": STEP_DUTY,
                    "values": values,
                },
            }
        )
        edges.append({"source": previous, "target": execute_id})
        previous = execute_id
        y += 120
    nodes.append({"id": "end", "type": "end", "label": "Recovery plan complete", "x": 740, "y": 160})
    edges.append({"source": previous, "target": "end"})
    return {
        "source": "template",
        "input_form": [{"name": "plan_id", "label": "Recovery plan", "type": "text", "required": True}],
        "nodes": nodes,
        "edges": edges,
    }


def _wf_status_to_plan_status(status):
    """Map a workflow step status onto a plan step status."""
    return {
        "completed": "done",
        "failed": "blocked",
        "cancelled": "skipped",
        "skipped": "skipped",
        "compensated": "skipped",
        "human": "in_progress",
        "action": "in_progress",
        "approval": "in_progress",
        "waiting": "in_progress",
        "ready": "pending",
        "blocked": "pending",
        "running": "in_progress",
        "timer": "pending",
        "event": "pending",
        "child": "in_progress",
        "dispatched": "in_progress",
    }.get(status, "pending")


def plan_progress_from_steps(plan, step_rows):
    """Progress of a plan from its workflow run's step rows.

    ``step_rows`` are plain dicts with ``node_id`` / ``status`` /
    ``acting_user_id`` / ``verified_at`` (as produced by the runtime's
    ``plan_progress``). The approval node of a step does not count as a
    step: progress is measured on the ``exec-<step>`` nodes only, reusing
    :func:`app.resilience.impact.recovery_progress` for the counts so the
    numbers match the slice-1 bookkeeping. A step whose execute node is
    still waiting for its approval shows as ``pending`` with
    ``waiting_for_approval: True``.
    """
    from app.resilience.impact import recovery_progress

    by_step = {step["id"]: step for step in plan["steps"]}
    rows = {}
    approval_waiting = set()
    for row in step_rows or []:
        node_id = row.get("node_id") or ""
        if node_id.startswith("exec-"):
            rows[node_id[5:]] = row
        elif node_id.startswith("approve-"):
            if row.get("status") in {"waiting", "approval", "action", "ready"}:
                approval_waiting.add(node_id[8:])
    detail = []
    bookkeeping = []
    for step in plan["steps"]:
        row = rows.get(step["id"])
        status = _wf_status_to_plan_status(row["status"]) if row else "pending"
        bookkeeping.append({"id": step["id"], "status": status})
        detail.append(
            {
                "step_id": step["id"],
                "kind": step["kind"],
                "action": step["action"],
                "covers": list(step["covers"]),
                "owner": step["owner"],
                "eta_days": step["eta_days"],
                "requires_approval": step["requires_approval"],
                "sensitive": step["sensitive"],
                "status": status,
                "waiting_for_approval": step["id"] in approval_waiting and status == "pending",
                "acting_user_id": (row or {}).get("acting_user_id"),
                "verified_at": (row or {}).get("verified_at"),
                "node_id": f"exec-{step['id']}",
            }
        )
    progress = recovery_progress({"steps": bookkeeping})
    return {
        "plan_id": plan["id"],
        "fingerprint": plan_fingerprint(plan),
        "total_steps": progress["total_steps"],
        "counts": progress["counts"],
        "percent_complete": progress["percent_complete"],
        "blocked_steps": progress["blocked_steps"],
        "next_step": progress["next_step"],
        "complete": progress["complete"],
        "steps": detail,
    }
