"""Bounded continuity inputs. Evidence is data, never an instruction or an executable URL."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Evidence(Strict):
    reference: str = Field(min_length=3, max_length=400)
    note: str = Field(min_length=3, max_length=1000)
    observed_at: AwareDatetime


class Provenance(Strict):
    basis: Literal["configured", "verified_fact", "ai_estimate"] = "configured"
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def require_evidence(self):
        if self.basis == "verified_fact" and not self.evidence:
            raise ValueError("Verified facts require supporting evidence.")
        return self


class TenantInput(Strict):
    name: str = Field(min_length=2, max_length=160)


class MemberInput(Strict):
    user_id: UUID
    role: Literal["owner", "approver", "planner", "executor", "viewer"]
    departments: list[UUID] = Field(default_factory=list, max_length=100)
    all_departments: bool = False
    active: bool = True
    reason: str = Field(min_length=10, max_length=1000)


class NodeInput(Provenance):
    external_key: str = Field(min_length=1, max_length=180)
    kind: Literal["department", "site", "employee", "system", "process", "service", "supplier", "customer", "project", "approval", "knowledge", "contract"]
    name: str = Field(min_length=1, max_length=180)
    department_id: UUID | None = None
    access_group_id: UUID | None = None
    user_id: UUID | None = None
    site: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=4000)
    skills: list[str] = Field(default_factory=list, max_length=40)
    required_skills: list[str] = Field(default_factory=list, max_length=40)
    available: bool = True
    critical: bool = False
    alternative: str = Field(default="", max_length=2000)
    knowledge_refs: list[str] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def typed_fields(self):
        if self.access_group_id and self.kind != "department":
            raise ValueError("Only departments may bind a workflow access group.")
        if self.user_id and self.kind != "employee":
            raise ValueError("Only employees may bind a user identity.")
        if any(len(s) > 180 or not s.strip() for s in self.skills + self.required_skills + self.knowledge_refs):
            raise ValueError("Skill and knowledge references must contain 1–180 characters.")
        return self


class LinkInput(Provenance):
    source_id: UUID
    target_id: UUID
    kind: Literal["depends_on", "owns", "backup_for", "approves", "supplies", "documents", "serves"]


class BIAInput(Provenance):
    node_id: UUID
    operational: int = Field(ge=0, le=5)
    financial: int = Field(ge=0, le=5)
    regulatory: int = Field(ge=0, le=5)
    customer: int = Field(ge=0, le=5)
    likelihood: int = Field(ge=1, le=5)
    mtpd_minutes: int = Field(ge=1, le=5256000)
    rto_minutes: int = Field(ge=1, le=5256000)
    rpo_minutes: int = Field(ge=0, le=5256000)
    minimum_service_percent: int = Field(ge=1, le=100)
    rationale: str = Field(min_length=10, max_length=4000)
    alternative: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def objective_order(self):
        if self.rto_minutes > self.mtpd_minutes:
            raise ValueError("RTO must not exceed MTPD.")
        return self


class ScenarioInput(Strict):
    name: str = Field(min_length=3, max_length=180)
    kind: Literal["resignation", "absence", "authority_unavailable", "server_outage", "database_failure", "supplier_disruption", "site_unavailable", "communication_outage", "cyber_incident", "knowledge_loss", "workload_spike", "compound"]
    disrupted_ids: list[UUID] = Field(min_length=1, max_length=100)
    duration_minutes: int = Field(ge=1, le=5256000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)
    source_module: Literal["simulation", "07"] = "simulation"
    source_event_id: UUID | None = None

    @model_validator(mode="after")
    def incident_source(self):
        if self.source_module == "07" and (not self.source_event_id or not self.evidence):
            raise ValueError("Incident escalation requires its source event and evidence.")
        if self.source_module == "simulation" and self.source_event_id:
            raise ValueError("Simulation cannot claim an incident event.")
        return self


class Step(Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=3, max_length=180)
    instructions: str = Field(min_length=10, max_length=4000)
    owner_id: UUID
    resource_id: UUID
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    due_minutes: int = Field(ge=1, le=5256000)
    workflow_id: UUID | None = None
    kind: Literal["knowledge_transfer", "responsibility_review", "access_request", "recovery", "verification", "communication"]


class PlanInput(Strict):
    title: str = Field(min_length=3, max_length=180)
    department_id: UUID | None = None
    kind: Literal["bcp", "drp_reference", "handover"] = "bcp"
    objectives: str = Field(min_length=10, max_length=4000)
    steps: list[Step] = Field(min_length=1, max_length=100)
    crisis_team: list[UUID] = Field(default_factory=list, max_length=40)
    contact_tree: list[UUID] = Field(default_factory=list, max_length=40)
    drp_references: list[str] = Field(default_factory=list, max_length=20)
    evidence: list[Evidence] = Field(min_length=1, max_length=20)
    review_due_at: AwareDatetime
    previous_version_id: UUID | None = None

    @model_validator(mode="after")
    def graph(self):
        keys = {s.key for s in self.steps}
        if len(keys) != len(self.steps):
            raise ValueError("Step keys must be unique.")
        pending = {s.key: set(s.depends_on) for s in self.steps}
        if any(not deps <= keys or key in deps for key, deps in pending.items()):
            raise ValueError("Dependencies must reference other steps in this plan.")
        resolved = set()
        while pending:
            ready = {key for key, deps in pending.items() if deps <= resolved}
            if not ready:
                raise ValueError("Recovery sequencing cannot contain cycles.")
            resolved |= ready
            pending = {key: deps for key, deps in pending.items() if key not in ready}
        if any(not ref.strip() or len(ref) > 400 for ref in self.drp_references):
            raise ValueError("DRP references must contain 1–400 characters.")
        return self


class Decision(Strict):
    approved: bool
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=10, max_length=2000)


class Activation(Strict):
    plan_id: UUID
    scenario_id: UUID | None = None
    idempotency_key: str = Field(min_length=8, max_length=100)
    mode: Literal["drill", "incident"]
    disruption_at: AwareDatetime


class TaskEvidence(Strict):
    evidence: list[Evidence] = Field(min_length=1, max_length=20)
    outcome: str = Field(min_length=10, max_length=4000)
    recovered_at: AwareDatetime
    restored_point_at: AwareDatetime | None = None
    service_percent: int = Field(ge=0, le=100)


class Reason(Strict):
    reason: str = Field(min_length=10, max_length=2000)


class Review(Strict):
    summary: str = Field(min_length=20, max_length=4000)
    corrective_actions: list[str] = Field(min_length=1, max_length=40)
    evidence: list[Evidence] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def action_lengths(self):
        if any(len(a.strip()) < 5 or len(a) > 1000 for a in self.corrective_actions):
            raise ValueError("Corrective actions must contain 5–1000 characters.")
        return self
