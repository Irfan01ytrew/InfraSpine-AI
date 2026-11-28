export type RiskLevel = "critical" | "high" | "medium" | "low";

export type RankedRisk = {
  node_id: string;
  name: string;
  kind: string;
  risk_score: number;
  risk_level: RiskLevel;
  factors: string[];
};

export type PlanStepProgress = {
  step_id: string;
  kind: string;
  action: string;
  covers: string[];
  owner: string | null;
  eta_days: number | null;
  requires_approval: boolean;
  sensitive: boolean;
  status: string;
  waiting_for_approval: boolean;
  acting_user_id: string | null;
  verified_at: string | null;
  node_id: string;
};

export type PlanProgress = {
  plan_id: string;
  fingerprint: string;
  total_steps: number;
  counts: Record<string, number>;
  percent_complete: number;
  blocked_steps: string[];
  next_step: string | null;
  complete: boolean;
  steps: PlanStepProgress[];
  run_id: string;
  run_status: string;
  started_at: string | null;
  finished_at: string | null;
  failure_code: string | null;
};

export type RecoveryPlan = {
  plan_id: string;
  run_id: string;
  status: string;
  created_at: string | null;
  progress: PlanProgress;
};

export type AuditEntry = {
  created_at: string;
  actor_email: string | null;
  action: string;
  target: string | null;
  outcome: string;
  details: Record<string, unknown>;
};

export type Overview = {
  scope_id: string;
  scope_name: string;
  as_of: string;
  freshness: string | null;
  sources: Record<string, number>;
  skipped: { record_id: string; reason: string }[];
  notes: string[];
  conflicts: unknown[];
  entities: number;
  relationships: number;
  ranked: RankedRisk[];
  plans: RecoveryPlan[];
  recent_audit: AuditEntry[];
};

export type DisruptionKind =
  | "person_absence"
  | "team_absence"
  | "server_outage"
  | "cyber_incident"
  | "supplier_failure";

export type Simulation = {
  scope_id: string;
  scope_name: string;
  as_of: string;
  freshness: string | null;
  sources: Record<string, number>;
  skipped: { record_id: string; reason: string }[];
  notes: string[];
  conflicts: unknown[];
  analysis: {
    event: { kind: string; target_ids: string[]; duration_days: number };
    disrupted_ids: string[];
    severity: string;
    affected: {
      id: string;
      name: string;
      kind: string;
      distance: number;
      confidence: number;
      path: string[];
      covered_by: string[];
    }[];
    affected_count: number;
    uncovered: Record<string, { reason: string }>;
    money_at_risk: Record<string, number>;
    money_counts: Record<string, number>;
    approval_bottleneck: string[];
    business_impact: string[];
    recovery_options: {
      rank: number;
      kind: string;
      action: string;
      covers: string[];
      owner: string | null;
      eta_days: number | null;
      requires_approval: boolean;
    }[];
    truncated: boolean;
    assumptions: string[];
    as_of: string | null;
  };
};
