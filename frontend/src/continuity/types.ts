export type Evidence = { reference: string; note: string; observed_at: string };
export type Workspace = { id: string; name: string; role: string; all_departments: boolean };
export type Resource = { id: string; name: string; kind: string; department_id?: string; user_id?: string; access_group_id?: string;
  data: { critical: boolean; available: boolean; site: string; description: string; alternative: string; skills: string[]; required_skills: string[]; knowledge_refs: string[]; basis: string; evidence: Evidence[] } };
export type Dependency = { id: string; source_id: string; target_id: string; kind: string; data: { basis: string; evidence: Evidence[] } };
export type Objectives = { rto_minutes: number; rpo_minutes: number; mtpd_minutes: number; minimum_service_percent: number };
export type Assessment = { id: string; node_id: string; created_at: string; data: Objectives & { operational: number; financial: number; regulatory: number; customer: number; likelihood: number; rationale: string; basis: string; alternative: string; calculation: { impact_score: number; risk_score: number; formula: string }; evidence: Evidence[] } };
export type Gap = { node_id: string; code: string; detail: string };
export type Impact = { id: string; name: string; kind: string; department_id?: string; site: string; direct: boolean; path_link_ids: string[]; backup_candidates: string[]; objectives: Objectives | null; score: { risk_score: number } | null; forecast: { rto_exceeded: boolean; mtpd_exceeded: boolean } | null };
export type Scenario = { id: string; name: string; kind: string; created_at: string; source_key?: string; data: { analysis: { affected: Impact[]; gaps: Gap[]; recovery_waves: string[][]; blocked_by_dependency_cycle: string[]; scope_note: string; scope_limited: boolean }; input: { duration_minutes: number; disrupted_ids: string[] } } };
export type Step = { key: string; title: string; instructions: string; owner_id: string; resource_id: string; depends_on: string[]; due_minutes: number; workflow_id: string | null; kind: string };
export type PlanData = { title: string; department_id: string | null; kind: string; objectives: string; steps: Step[]; crisis_team: string[]; contact_tree: string[]; drp_references: string[]; evidence: Evidence[]; review_due_at: string; previous_version_id?: string | null };
export type Plan = { id: string; title: string; family_id: string; version: number; status: string; requested_by: string; digest: string; review_due_at: string; created_at: string; approval_expires_at: string; decision_reason?: string; data: PlanData };
export type Activation = { id: string; plan_id: string; scenario_id?: string; status: string; mode: string; requested_by: string; digest: string; disruption_at: string; activated_at?: string; closed_at?: string; approval_expires_at: string; review?: { summary: string; corrective_actions: string[]; evidence: Evidence[] } };
export type Task = { id: string; step_key: string; status: string; workflow_run_id?: string; step: Step; evidence?: { outcome: string; evidence: Evidence[]; recovered_at: string; restored_point_at?: string; service_percent: number }; submitted_by?: string; verified_at?: string; objectives: Objectives | null; observed: { recovery_minutes: number; data_loss_minutes: number | null; service_percent: number; rto_met: boolean | null; rpo_met: boolean | null; minimum_service_met: boolean | null; basis: string; drill: boolean } | null; overdue: boolean; rto_at_risk: boolean };
export type Report = { run: Activation; plan: Plan; tasks: Task[]; verified_steps: number; total_steps: number; limitations: string; generated_at: string };
export type Workflow = { id: string; name: string; version: number; department_group_id: string; assignee_user_id: string };
export type Dashboard = { nodes: Resource[]; links: Dependency[]; assessments: Assessment[]; scenarios: Scenario[]; plans: Plan[]; runs: Activation[]; gaps: Gap[]; metrics: { critical_resources: number; bia_assessed: number; plan_covered: number; plan_coverage_percent: number | null; pending_approvals: number; verified_drills: number; active_recoveries: number }; metric_note: string; generated_at: string; scope: { role: string; all_departments: boolean } };
export type Draft = { title: string; kind: string; steps: Step[]; unresolved_resource_ids: string[]; limitations: string };

/* ---------------------------------------------------------------- Step AJ */
export type AjSeverity = "critical" | "high" | "medium" | "low";
export type AjOverallStatus = "ok" | "attention" | "critical";
export type AjRisk = { id: string; title: string; severity: AjSeverity; category: string; source: "gap" | "incident" | "alert" | "prediction"; affected: string[]; has_plan: boolean };
export type AjIncident = { id: string; title: string; severity: AjSeverity; status: string; overdue: boolean };
export type AjRecoveryItem = { id: string; title: string; status: string; verified_steps: number; total_steps: number; progress_percent: number | null; at_risk: boolean };
/** Step G health roll-up, passed through unchanged (or null until the Step G merge supplies it). */
export type AjHealth = { status: string; [key: string]: unknown } | null;
export type AjDashboardSummary = {
  overall_status: AjOverallStatus;
  generated_at: string;
  risks: { total: number; by_severity: Partial<Record<AjSeverity, number>>; uncovered_critical: number; top: AjRisk[] };
  incidents: { total_active: number; by_severity: Partial<Record<AjSeverity, number>>; overdue: number; top: AjIncident[] };
  approvals: { pending: number; expiring_soon: number; own_requests: number };
  recovery: { active: number; closed: number; average_progress_percent: number | null; at_risk: number; items: AjRecoveryItem[] };
  health: AjHealth;
  highlights: string[];
};
export type HandoverCandidate = { node_id: string; user_id: string | null; eligible: boolean; gaps: { code: string; detail: string }[]; missing_permissions: string[]; missing_skills: string[]; backup_ok: boolean };
export type HandoverResource = { resource_id: string; resource_name: string; resource_kind: string; scope_id: string | null; scope_bound: boolean; permission_check: string; permission_note: string | null; required_permissions: string[]; candidates: HandoverCandidate[]; eligible_candidates: string[] };
export type HandoverCheck = { draft: Draft; resources: HandoverResource[]; eligible: boolean; unresolved_resource_ids: string[] };
export type HandoverGrant = { id: string; tenant_id: string; scenario_id: string | null; plan_id: string | null; resource_id: string; candidate_node_id: string; user_id: string; scope_id: string | null; permissions: string[]; status: string; valid_from: string | null; valid_to: string | null; decision_expires_at: string; requested_by: string; decided_by: string | null; reason: string | null; digest: string; created_at: string };
export type GraphMergeStatus = { continuity_tenant_id: string; company_graph_tenant_id: string; bc_nodes: number; bc_nodes_linked: number; bc_nodes_unlinked: { id: string; kind: string; name: string }[]; cg_records: number; cg_records_linked: number; cg_records_unlinked: { id: string; kind: string; name: string }[]; links: number; stale_sources: { id: string; name: string; kind: string; last_synced_at: string | null }[] };
export type DrillSeedResult = { created: Scenario[]; existing: Scenario[]; skipped: { kind: string; reason: string }[] };
export type DrillDifference = { code: string; resource_id: string; detail: string };
export type DrillComparison = { run_id: string; scenario_id: string | null; matches: boolean; differences: DrillDifference[]; affected: number; recovered: number; simulated: number };
