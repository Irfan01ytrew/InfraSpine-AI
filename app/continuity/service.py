"""Continuity transactions. Routes commit once; no provider, shell or network effects."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.continuity import intelligence, journal
from app.continuity.access import node_for, nodes_for, scope_for
from app.continuity.models import Assessment, Event, GrantRequest, Link, Member, Node, Plan, Run, Scenario, Task, Tenant
from app.continuity.schemas import MemberInput
from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import AccessGroup, AccessGroupMember, AutomationRun, AutomationTask, AutomationWorkflow, User
from app.services import automation, audit


def public(row):
    return {column.name: (str(value) if isinstance(value, uuid.UUID) else value.isoformat() if isinstance(value, datetime) else value)
            for column in row.__table__.columns if (value := getattr(row, column.name)) is not None}


def require_evidence_time(evidence):
    if any(item.observed_at > datetime.now(UTC) for item in evidence):
        raise InvalidInputError("Evidence observations cannot be in the future.")


async def create_tenant(db, user, data, ctx):
    if user.role != "admin":
        raise PermissionDeniedError("An administrator must provision a continuity workspace.")
    row = Tenant(name=data.name)
    db.add(row)
    await db.flush()
    db.add(Member(tenant_id=row.id, user_id=user.id, role="owner", all_departments=True))
    await db.flush()
    scope = await scope_for(db, row.id, user, write=True)
    await journal.record(db, scope, ctx, "workspace_created", row.id)
    return {**public(row), "role": "owner", "all_departments": True}


async def list_tenants(db, user):
    rows = (await db.execute(select(Tenant, Member).join(Member).where(Member.user_id == user.id, Member.active.is_(True)).order_by(Tenant.name))).all()
    return [{"id": str(t.id), "name": t.name, "role": m.role, "all_departments": m.all_departments} for t, m in rows]


async def propose_grant(db, scope, data, ctx):
    scope.require("owner")
    if not scope.all_departments:
        raise PermissionDeniedError("Workspace-wide ownership is required to request membership changes.")
    if data.user_id == scope.user_id:
        raise PermissionDeniedError("You cannot change your own continuity membership.")
    if not await db.scalar(select(User.id).where(User.id == data.user_id, User.is_active.is_(True))):
        raise NotFoundError("Active user not found.")
    if data.role == "owner" and not data.all_departments:
        raise InvalidInputError("Workspace owners must have all-department scope.")
    if not data.all_departments and not data.departments:
        raise InvalidInputError("Choose at least one department.")
    for department in data.departments:
        if (await node_for(db, scope, department)).kind != "department":
            raise InvalidInputError("Scope entries must be departments.")
    payload = data.model_dump(mode="json")
    row = GrantRequest(tenant_id=scope.tenant_id, data=payload, digest=journal.digest(payload), requested_by=scope.user_id,
                       expires_at=datetime.now(UTC) + timedelta(hours=1))
    db.add(row)
    await db.flush()
    await journal.record(db, scope, ctx, "membership_requested", row.id, {"digest": row.digest})
    return public(row)


async def decide_grant(db, user, grant_id, data, ctx):
    if user.role != "admin":
        raise PermissionDeniedError("A second platform administrator must approve continuity membership changes.")
    row = await db.get(GrantRequest, grant_id)
    if row is None:
        raise NotFoundError()
    await db.scalar(select(Tenant).where(Tenant.id == row.tenant_id).with_for_update())
    await db.refresh(row)
    if row.requested_by == user.id or row.data["user_id"] == str(user.id):
        raise PermissionDeniedError("Requester and beneficiary cannot approve a membership change.")
    if row.status != "pending" or row.expires_at <= datetime.now(UTC) or row.digest != data.digest:
        raise ConflictError("Membership decision is stale, expired or does not match the requested action.")
    requester = await db.get(User, row.requested_by)
    requester_scope = await scope_for(db, row.tenant_id, requester)
    requester_scope.require("owner")
    payload = MemberInput.model_validate(row.data)
    if data.approved:
        target = await db.get(User, payload.user_id)
        if target is None or not target.is_active:
            raise ConflictError("The proposed member is no longer active.")
        member = await db.get(Member, (row.tenant_id, payload.user_id))
        if member is None:
            member = Member(tenant_id=row.tenant_id, user_id=payload.user_id)
            db.add(member)
        member.role, member.all_departments, member.active = payload.role, payload.all_departments, payload.active
        member.departments = [str(d) for d in payload.departments]
    row.status, row.decided_by = ("approved" if data.approved else "rejected"), user.id
    from dataclasses import replace
    await journal.record(db, replace(requester_scope, user_id=user.id), ctx, "membership_decided", row.id, {"digest": row.digest, "decision": row.status, "reason": data.reason})
    return public(row)


async def create_node(db, scope, data, ctx):
    scope.require("owner", "planner")
    require_evidence_time(data.evidence)
    if data.kind == "department":
        if not scope.all_departments:
            raise PermissionDeniedError("Workspace scope is required to add a department.")
        if data.department_id:
            raise InvalidInputError("A department is its own scope boundary.")
    elif not scope.permits(data.department_id):
        raise PermissionDeniedError()
    if data.department_id and (await node_for(db, scope, data.department_id)).kind != "department":
        raise InvalidInputError("Department reference must identify a department.")
    if data.access_group_id:
        actor = await db.get(User, scope.user_id)
        await automation._group_access(db, actor, data.access_group_id, manage=True)
    if data.user_id:
        member = await db.get(Member, (scope.tenant_id, data.user_id))
        user = await db.get(User, data.user_id)
        if not member or not member.active or not user or not user.is_active or not (member.all_departments or str(data.department_id) in member.departments):
            raise InvalidInputError("Employee identity must be an active continuity member authorized for this department.")
    if await db.scalar(select(func.count()).select_from(Node).where(Node.tenant_id == scope.tenant_id)) >= 2000:
        raise InvalidInputError("This workspace has reached the 2,000-resource analysis bound.")
    row = Node(id=uuid.uuid4(), tenant_id=scope.tenant_id, department_id=data.department_id, external_key=data.external_key,
               access_group_id=data.access_group_id, user_id=data.user_id, kind=data.kind, name=data.name, data=data.model_dump(mode="json"))
    if data.kind == "department":
        row.department_id = row.id
    db.add(row)
    await db.flush()
    await journal.record(db, scope, ctx, "resource_created", row.id, {"digest": journal.digest(row.data)})
    return public(row)


async def create_link(db, scope, data, ctx):
    scope.require("owner", "planner")
    require_evidence_time(data.evidence)
    source, target = await node_for(db, scope, data.source_id), await node_for(db, scope, data.target_id)
    if source.id == target.id:
        raise InvalidInputError("A resource cannot depend on itself.")
    if data.kind in {"owns", "backup_for", "approves"} and source.kind != "employee":
        raise InvalidInputError("Ownership, backup and approval relationships require an employee source.")
    if data.kind == "documents" and source.kind != "knowledge":
        raise InvalidInputError("Documentation relationships require a knowledge source.")
    row = Link(tenant_id=scope.tenant_id, source_id=source.id, target_id=target.id, kind=data.kind, data=data.model_dump(mode="json"))
    db.add(row)
    await db.flush()
    await journal.record(db, scope, ctx, "dependency_created", row.id, {"digest": journal.digest(row.data)})
    return public(row)


async def graph(db, scope):
    nodes = await nodes_for(db, scope)
    ids = [n.id for n in nodes]
    links = list(await db.scalars(select(Link).where(Link.tenant_id == scope.tenant_id, Link.source_id.in_(ids), Link.target_id.in_(ids)).order_by(Link.id)))
    # DISTINCT ON selects the latest immutable assessment per resource, with a stable tie-breaker.
    assessments = list(await db.scalars(select(Assessment).where(Assessment.tenant_id == scope.tenant_id, Assessment.node_id.in_(ids))
                                       .distinct(Assessment.node_id).order_by(Assessment.node_id, Assessment.created_at.desc(), Assessment.id.desc())))
    return nodes, links, assessments


async def create_bia(db, scope, data, ctx):
    scope.require("owner", "planner")
    require_evidence_time(data.evidence)
    node = await node_for(db, scope, data.node_id)
    if node.kind not in {"process", "service", "system"}:
        raise InvalidInputError("BIA must assess a process, service or system.")
    payload = data.model_dump(mode="json")
    payload["calculation"] = intelligence.bia_score(payload)
    row = Assessment(tenant_id=scope.tenant_id, node_id=node.id, data=payload, created_by=scope.user_id)
    db.add(row)
    await db.flush()
    await journal.record(db, scope, ctx, "bia_recorded", row.id, {"digest": journal.digest(payload)})
    return public(row)


async def create_scenario(db, scope, data, ctx, source_key=None):
    scope.require("owner", "planner")
    require_evidence_time(data.evidence)
    for key in data.disrupted_ids:
        await node_for(db, scope, key)
    if data.source_module == "07":
        source_key = "07:" + str(data.source_event_id)
    # Otherwise a server-side caller may pass its own source_key (e.g. drill
    # seeds use "drill:<kind>"). The API never sets one: ScenarioInput has
    # no source_key field, so clients cannot forge it. The key must be set
    # here, at insert time — bc_scenarios is immutable, any later UPDATE is
    # rejected by the database trigger.
    payload = data.model_dump(mode="json")
    fingerprint = journal.digest(payload)
    if source_key:
        old = await db.scalar(select(Scenario).where(Scenario.tenant_id == scope.tenant_id, Scenario.source_key == source_key))
        if old:
            await scenario_for(db, scope, old.id)
            if old.data["input_digest"] != fingerprint:
                raise ConflictError("Incident event was already recorded with different content.")
            return public(old)
    nodes, links, assessments = await graph(db, scope)
    result = intelligence.analyze(nodes, links, assessments, data.disrupted_ids, data.duration_minutes, not scope.all_departments)
    row = Scenario(tenant_id=scope.tenant_id, name=data.name, kind=data.kind, source_key=source_key,
                   created_by=scope.user_id, data={"input": payload, "input_digest": fingerprint, "analysis": result,
                                                 "source_authentication": "authenticated user attestation"})
    db.add(row)
    await db.flush()
    await journal.record(db, scope, ctx, "scenario_recorded", row.id, {"digest": journal.digest(row.data)})
    return public(row)


async def scenario_for(db, scope, scenario_id):
    row = await db.scalar(select(Scenario).where(Scenario.tenant_id == scope.tenant_id, Scenario.id == scenario_id))
    if not row:
        raise NotFoundError()
    for item in row.data["analysis"]["affected"]:
        await node_for(db, scope, uuid.UUID(item["id"]))
    return row


async def validate_owner(db, scope, owner_id, resource):
    owner = await node_for(db, scope, owner_id)
    if owner.kind != "employee" or not owner.data.get("available") or not owner.user_id:
        raise InvalidInputError("Task owner must be an available employee bound to an active user.")
    member = await db.get(Member, (scope.tenant_id, owner.user_id))
    user = await db.get(User, owner.user_id)
    if not member or not member.active or not user or not user.is_active or not (member.all_departments or str(resource.department_id) in member.departments):
        raise PermissionDeniedError("Task owner no longer has authorized department membership.")
    return owner


async def workflow_binding(db, scope, workflow_id, resource, owner):
    workflow = await db.get(AutomationWorkflow, workflow_id)
    department = await node_for(db, scope, resource.department_id) if resource.department_id else None
    actor = await db.get(User, scope.user_id)
    if not department or not department.access_group_id or not workflow or not workflow.enabled or workflow.department_group_id != department.access_group_id:
        raise InvalidInputError("Workflow must belong to the resource's bound department.")
    await automation._group_access(db, actor, workflow.department_group_id)
    # Continuity coordinates internal human tasks only. Access and infrastructure adapters remain in their owning modules.
    if workflow.action_type != "create_task" or workflow.action_config.get("assignee_user_id") != str(owner.user_id):
        raise InvalidInputError("Bind an internal-task workflow assigned to the approved continuity owner.")
    await automation._validate_assignee(db, workflow.department_group_id, owner.user_id)
    return workflow, journal.digest({"id": str(workflow.id), "version": workflow.version, "department": str(workflow.department_group_id),
                                     "action": workflow.action_type, "config": workflow.action_config})


async def create_plan(db, scope, data, ctx):
    scope.require("owner", "planner")
    require_evidence_time(data.evidence)
    now = datetime.now(UTC)
    if data.review_due_at <= now or data.review_due_at > now + timedelta(days=730):
        raise InvalidInputError("Schedule a review within the next two years.")
    if not scope.permits(data.department_id):
        raise PermissionDeniedError()
    if data.department_id and (await node_for(db, scope, data.department_id)).kind != "department":
        raise InvalidInputError("Choose a department scope.")
    payload = data.model_dump(mode="json")
    payload["workflow_bindings"], payload["objective_snapshots"] = {}, {}
    for step in data.steps:
        resource = await node_for(db, scope, step.resource_id)
        if data.department_id and resource.department_id != data.department_id:
            raise InvalidInputError("Every resource must belong to the plan's department.")
        owner = await validate_owner(db, scope, step.owner_id, resource)
        if data.kind == "handover":
            backup = await db.scalar(select(Link).where(Link.tenant_id == scope.tenant_id, Link.source_id == owner.id, Link.target_id == resource.id, Link.kind == "backup_for"))
            if not backup or backup.data["basis"] != "verified_fact" or not {s.casefold() for s in resource.data.get("required_skills", [])} <= {s.casefold() for s in owner.data.get("skills", [])}:
                raise InvalidInputError("Handover owner needs an evidenced backup relationship and the recorded required skills.")
        if step.workflow_id:
            _, fingerprint = await workflow_binding(db, scope, step.workflow_id, resource, owner)
            payload["workflow_bindings"][step.key] = fingerprint
        latest = await db.scalar(select(Assessment).where(Assessment.tenant_id == scope.tenant_id, Assessment.node_id == resource.id)
                                 .order_by(Assessment.created_at.desc(), Assessment.id.desc()).limit(1))
        if latest:
            payload["objective_snapshots"][str(resource.id)] = {"assessment_id": str(latest.id), **latest.data}
    for member in set(data.crisis_team + data.contact_tree):
        if (await node_for(db, scope, member)).kind != "employee":
            raise InvalidInputError("Crisis teams and contact trees reference employee resources.")
    family, version = uuid.uuid4(), 1
    if data.previous_version_id:
        previous = await plan_for(db, scope, data.previous_version_id)
        family = previous.family_id
        latest_version = await db.scalar(select(func.max(Plan.version)).where(Plan.tenant_id == scope.tenant_id, Plan.family_id == family))
        if previous.version != latest_version:
            raise ConflictError("Revise the latest plan version.")
        version = latest_version + 1
    row = Plan(tenant_id=scope.tenant_id, family_id=family, version=version, department_id=data.department_id, title=data.title,
               data=payload, digest=journal.digest(payload), requested_by=scope.user_id, approval_expires_at=now + timedelta(hours=24), review_due_at=data.review_due_at)
    db.add(row)
    await db.flush()
    await journal.record(db, scope, ctx, "plan_requested", row.id, {"digest": row.digest, "version": version})
    return public(row)


async def plan_for(db, scope, plan_id):
    row = await db.scalar(select(Plan).where(Plan.tenant_id == scope.tenant_id, Plan.id == plan_id))
    if row is None or not scope.permits(row.department_id):
        raise NotFoundError()
    refs = {step[key] for step in row.data["steps"] for key in ("owner_id", "resource_id")}
    refs.update(row.data["crisis_team"] + row.data["contact_tree"])
    for key in refs:
        await node_for(db, scope, uuid.UUID(key))
    return row


def validate_decision(scope, row, data):
    scope.require("owner", "approver")
    if scope.user_id == row.requested_by:
        raise PermissionDeniedError("The requester cannot approve their own action.")
    if row.status != "pending" or row.approval_expires_at <= datetime.now(UTC) or data.digest != row.digest:
        raise ConflictError("The action is stale, expired, already decided or does not match this approval.")


async def decide_plan(db, scope, plan_id, data, ctx):
    row = await plan_for(db, scope, plan_id)
    validate_decision(scope, row, data)
    if journal.digest(row.data) != row.digest:
        raise ConflictError("Plan content does not match its approval digest.")
    row.status = "approved" if data.approved else "rejected"
    row.decided_by, row.decision_reason = scope.user_id, data.reason
    await journal.record(db, scope, ctx, "plan_decided", row.id, {"decision": row.status, "digest": row.digest, "reason": data.reason})
    return public(row)


async def activate(db, scope, data, ctx):
    scope.require("owner", "planner")
    plan = await plan_for(db, scope, data.plan_id)
    fingerprint = journal.digest({**data.model_dump(mode="json"), "plan_digest": plan.digest, "requester": str(scope.user_id)})
    existing = await db.scalar(select(Run).where(Run.tenant_id == scope.tenant_id, Run.idempotency_key == data.idempotency_key))
    if existing:
        if existing.digest != fingerprint:
            raise ConflictError("Activation key has already been used for a different action.")
        return public(existing)
    now = datetime.now(UTC)
    if plan.status != "approved" or plan.review_due_at <= now:
        raise ConflictError("An approved plan with a current review is required.")
    if data.disruption_at > now:
        raise InvalidInputError("Disruption time cannot be in the future.")
    if data.scenario_id:
        scenario = await scenario_for(db, scope, data.scenario_id)
        if data.mode == "incident" and not scenario.source_key:
            raise InvalidInputError("An incident activation requires an incident escalation, not a simulation.")
        # No task can assign an employee who is disrupted in this activation.
        affected = {item["id"] for item in scenario.data["analysis"]["affected"]}
        if any(step["owner_id"] in affected for step in plan.data["steps"]):
            raise ConflictError("A proposed recovery owner is unavailable in this scenario.")
    elif data.mode == "incident":
        raise InvalidInputError("An incident activation requires its escalation record.")
    row = Run(tenant_id=scope.tenant_id, plan_id=plan.id, scenario_id=data.scenario_id, idempotency_key=data.idempotency_key,
              digest=fingerprint, mode=data.mode, requested_by=scope.user_id, disruption_at=data.disruption_at, approval_expires_at=now + timedelta(hours=1))
    db.add(row)
    await db.flush()
    await journal.record(db, scope, ctx, "activation_requested", row.id, {"digest": fingerprint, "mode": row.mode})
    return public(row)


async def run_for(db, scope, run_id):
    row = await db.scalar(select(Run).where(Run.tenant_id == scope.tenant_id, Run.id == run_id))
    if row is None:
        raise NotFoundError()
    plan = await plan_for(db, scope, row.plan_id)
    if row.scenario_id:
        await scenario_for(db, scope, row.scenario_id)
    return row, plan


async def decide_activation(db, scope, run_id, data, ctx, cipher=None):
    row, plan = await run_for(db, scope, run_id)
    validate_decision(scope, row, data)
    if plan.status != "approved" or plan.review_due_at <= datetime.now(UTC):
        raise ConflictError("The plan requires review before activation.")
    row.status = "active" if data.approved else "rejected"
    row.decided_by, row.decision_reason = scope.user_id, data.reason
    if data.approved:
        row.activated_at = datetime.now(UTC)
        for step in plan.data["steps"]:
            resource = await node_for(db, scope, uuid.UUID(step["resource_id"]))
            await validate_owner(db, scope, uuid.UUID(step["owner_id"]), resource)
            db.add(Task(tenant_id=scope.tenant_id, run_id=row.id, step_key=step["key"]))
    await journal.record(db, scope, ctx, "activation_decided", row.id, {"decision": row.status, "reason": data.reason, "digest": row.digest})
    if data.approved and cipher is not None:
        # Mirror to the unified runtime in the same transaction; a mirror
        # failure never breaks the activation (the engine sweep retries).
        from app.continuity.unified import mirror_activation

        await mirror_activation(db, cipher, row, plan)
    return public(row)


async def task_for(db, scope, task_id):
    task = await db.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == scope.tenant_id))
    if not task:
        raise NotFoundError()
    run, plan = await run_for(db, scope, task.run_id)
    step = next(s for s in plan.data["steps"] if s["key"] == task.step_key)
    return task, run, plan, step


async def prerequisites(db, scope, run, step):
    if run.status != "active":
        raise ConflictError("This activation is not active.")
    finished = set(await db.scalars(select(Task.step_key).where(Task.tenant_id == scope.tenant_id, Task.run_id == run.id, Task.status == "verified")))
    if not set(step["depends_on"]) <= finished:
        raise ConflictError("Preceding recovery steps must be verified first.")


async def dispatch(db, scope, task_id, ctx):
    scope.require("owner", "planner", "executor")
    task, run, plan, step = await task_for(db, scope, task_id)
    await prerequisites(db, scope, run, step)
    if task.workflow_run_id:
        return public(task)
    if task.status != "pending" or not step["workflow_id"]:
        raise ConflictError("This step needs a bound workflow in a new approved plan version before dispatch.")
    resource = await node_for(db, scope, uuid.UUID(step["resource_id"]))
    owner = await validate_owner(db, scope, uuid.UUID(step["owner_id"]), resource)
    workflow, fingerprint = await workflow_binding(db, scope, uuid.UUID(step["workflow_id"]), resource, owner)
    if fingerprint != plan.data["workflow_bindings"].get(step["key"]):
        raise ConflictError("The workflow action no longer matches the approved plan.")
    # Creation and the link commit together. A worker cannot observe a partial dispatch.
    # Module 05's separate second-person approval is preserved, never fabricated here.
    external = AutomationRun(id=uuid.uuid4(), workflow_id=workflow.id, requester_user_id=scope.user_id,
                             department_group_id=workflow.department_group_id, idempotency_key="continuity:" + str(task.id), status="pending_approval")
    db.add(external)
    await db.flush()
    await automation.notify_approvers(db, run_id=external.id, department_group_id=external.department_group_id, requester_user_id=scope.user_id)
    task.workflow_run_id, task.status = external.id, "dispatched"
    await journal.record(db, scope, ctx, "workflow_dispatched", task.id, {"workflow_run_id": str(external.id), "plan_digest": plan.digest})
    return public(task)


async def submit_evidence(db, scope, task_id, data, ctx):
    task, run, plan, step = await task_for(db, scope, task_id)
    await prerequisites(db, scope, run, step)
    owner = await node_for(db, scope, uuid.UUID(step["owner_id"]))
    if owner.user_id != scope.user_id:
        raise PermissionDeniedError("Only the approved task owner may submit completion evidence.")
    resource = await node_for(db, scope, uuid.UUID(step["resource_id"]))
    await validate_owner(db, scope, owner.id, resource)
    if task.status not in {"pending", "dispatched"}:
        raise ConflictError("Completion evidence was already submitted.")
    if step["workflow_id"]:
        if not task.workflow_run_id:
            raise ConflictError("Execute the approved workflow before submitting completion.")
        external = await db.scalar(select(AutomationTask).where(AutomationTask.run_id == task.workflow_run_id, AutomationTask.assignee_user_id == owner.user_id, AutomationTask.status == "completed"))
        if not external:
            raise ConflictError("The assigned workflow task has not been completed.")
    require_evidence_time(data.evidence)
    if not run.disruption_at <= data.recovered_at <= datetime.now(UTC):
        raise InvalidInputError("Recovery time must fall between disruption and now.")
    if data.restored_point_at and data.restored_point_at > data.recovered_at:
        raise InvalidInputError("Restored data cannot be newer than the reported recovery.")
    task.evidence = data.model_dump(mode="json")
    task.submitted_by, task.status = scope.user_id, "evidence_submitted"
    await journal.record(db, scope, ctx, "evidence_submitted", task.id, {"digest": journal.digest(task.evidence)})
    return public(task)


async def verify_task(db, scope, task_id, data, ctx):
    scope.require("owner", "approver")
    task, run, _, step = await task_for(db, scope, task_id)
    await prerequisites(db, scope, run, step)
    if task.status != "evidence_submitted":
        raise ConflictError("Completion evidence is required before verification.")
    if task.submitted_by == scope.user_id:
        raise PermissionDeniedError("The submitter cannot verify their own completion.")
    task.status, task.verified_by, task.verified_at = "verified", scope.user_id, datetime.now(UTC)
    await journal.record(db, scope, ctx, "completion_verified", task.id, {"reason": data.reason, "evidence_digest": journal.digest(task.evidence)})
    return public(task)


async def transition(db, scope, run_id, action, data, ctx):
    scope.require("owner", "approver")
    run, plan = await run_for(db, scope, run_id)
    if action == "suspend" and run.status == "active":
        run.status = "suspended"
    elif action == "resume" and run.status == "suspended":
        if plan.review_due_at <= datetime.now(UTC):
            raise ConflictError("The plan requires review before resuming.")
        run.status = "active"
    else:
        raise ConflictError("That transition is not available from this state.")
    await journal.record(db, scope, ctx, "activation_" + action, run.id, {"reason": data.reason})
    return public(run)


async def close_run(db, scope, run_id, data, ctx):
    scope.require("owner", "approver")
    run, plan = await run_for(db, scope, run_id)
    if run.status != "active":
        raise ConflictError("Only an active run can close.")
    tasks = list(await db.scalars(select(Task).where(Task.tenant_id == scope.tenant_id, Task.run_id == run.id)))
    if len(tasks) != len(plan.data["steps"]) or any(t.status != "verified" for t in tasks):
        raise ConflictError("Every recovery step needs independent verification before closure.")
    require_evidence_time(data.evidence)
    run.status, run.closed_at, run.review = "closed", datetime.now(UTC), data.model_dump(mode="json")
    await journal.record(db, scope, ctx, "activation_closed", run.id, {"review_digest": journal.digest(run.review)})
    return public(run)


async def report(db, scope, run_id):
    run, plan = await run_for(db, scope, run_id)
    tasks = list(await db.scalars(select(Task).where(Task.tenant_id == scope.tenant_id, Task.run_id == run.id).order_by(Task.step_key)))
    now = datetime.now(UTC)
    results = []
    for task in tasks:
        step = next(s for s in plan.data["steps"] if s["key"] == task.step_key)
        objective = plan.data["objective_snapshots"].get(step["resource_id"])
        evidence = task.evidence
        observed = None
        if evidence and task.status == "verified":
            recovered = datetime.fromisoformat(evidence["recovered_at"].replace("Z", "+00:00"))
            elapsed = round((recovered - run.disruption_at).total_seconds() / 60, 2)
            point = datetime.fromisoformat(evidence["restored_point_at"].replace("Z", "+00:00")) if evidence.get("restored_point_at") else None
            lost = max(0, round((run.disruption_at - point).total_seconds() / 60, 2)) if point else None
            observed = {"recovery_minutes": elapsed, "data_loss_minutes": lost, "service_percent": evidence["service_percent"],
                        "rto_met": elapsed <= objective["rto_minutes"] if objective else None,
                        "rpo_met": lost <= objective["rpo_minutes"] if objective and lost is not None else None,
                        "minimum_service_met": evidence["service_percent"] >= objective["minimum_service_percent"] if objective else None,
                        "basis": "Independently reviewed, human-submitted evidence", "drill": run.mode == "drill"}
        elapsed_now = max(0, (now - run.disruption_at).total_seconds() / 60)
        results.append({**public(task), "step": step, "objectives": objective, "observed": observed,
                        "overdue": task.status != "verified" and elapsed_now > step["due_minutes"],
                        "rto_at_risk": task.status != "verified" and objective is not None and elapsed_now > objective["rto_minutes"]})
    return {"run": public(run), "plan": public(plan), "tasks": results, "generated_at": now.isoformat(),
            "verified_steps": sum(t.status == "verified" for t in tasks), "total_steps": len(plan.data["steps"]),
            "limitations": "Drills and task attestations do not establish production resilience. RPO remains unknown without a restored-data timestamp. External execution is owned by Module 05."}


async def dashboard(db, scope):
    nodes, links, assessments = await graph(db, scope)
    plans, scenarios, runs = [], [], []
    for row in await db.scalars(select(Plan).where(Plan.tenant_id == scope.tenant_id).order_by(Plan.created_at.desc()).limit(300)):
        try:
            await plan_for(db, scope, row.id)
            plans.append(public(row))
        except NotFoundError:
            pass
    for row in await db.scalars(select(Scenario).where(Scenario.tenant_id == scope.tenant_id).order_by(Scenario.created_at.desc()).limit(100)):
        try:
            await scenario_for(db, scope, row.id)
            scenarios.append(public(row))
        except NotFoundError:
            pass
    for row in await db.scalars(select(Run).where(Run.tenant_id == scope.tenant_id).order_by(Run.created_at.desc()).limit(100)):
        try:
            await run_for(db, scope, row.id)
            runs.append(public(row))
        except NotFoundError:
            pass
    critical = [n for n in nodes if n.data["critical"] and n.kind in {"process", "service", "system"}]
    assessed = {a.node_id for a in assessments}
    covered = {s["resource_id"] for p in plans if p["status"] == "approved" and datetime.fromisoformat(p["review_due_at"]) > datetime.now(UTC) for s in p["data"]["steps"]}
    gaps = intelligence.analyze(nodes, links, assessments, [n.id for n in critical], 1, not scope.all_departments)["gaps"] if critical else []
    return {"nodes": [public(n) for n in nodes], "links": [public(l) for l in links], "assessments": [public(a) for a in assessments],
            "plans": plans, "scenarios": scenarios, "runs": runs, "gaps": gaps,
            "metrics": {"critical_resources": len(critical), "bia_assessed": sum(n.id in assessed for n in critical),
                        "plan_covered": sum(str(n.id) in covered for n in critical),
                        "plan_coverage_percent": round(sum(str(n.id) in covered for n in critical) / len(critical) * 100, 1) if critical else None,
                        "pending_approvals": sum(p["status"] == "pending" for p in plans) + sum(r["status"] == "pending" for r in runs),
                        "verified_drills": sum(r["mode"] == "drill" and r["status"] == "closed" for r in runs),
                        "active_recoveries": sum(r["status"] == "active" for r in runs)},
            "scope": {"role": scope.role, "all_departments": scope.all_departments},
            "metric_note": "Coverage measures approved, current plans for recorded critical resources. It is not a production resilience score. History windows: 300 plans, 100 scenarios and 100 activations.",
            "generated_at": datetime.now(UTC).isoformat()}
