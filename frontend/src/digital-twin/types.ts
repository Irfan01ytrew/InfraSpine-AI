export type TwinSource = {
  name: string;
  kind: string;
  status: "fresh" | "stale" | "unknown";
  last_synced_at: string | null;
  stale_after_days: number | null;
};

export type MoneyAtRisk = {
  totals: Record<string, number>;
  batch_count: number;
  payment_count: number;
};

export type TwinOverview = {
  tenant_id: string;
  fingerprint: string;
  as_of: string;
  entity_count: number;
  relationship_count: number;
  entities_by_kind: Record<string, number>;
  money_at_risk: MoneyAtRisk;
  sources: TwinSource[];
  stale_sources: string[];
  truncated: boolean;
};

export type TwinEntity = {
  id: string;
  kind: string;
  name: string;
  attributes: Record<string, unknown>;
};

export type RecoveryOption = {
  action: string;
  owner: string;
  owner_id: string | null;
  target: string;
  rank: number;
  note: string;
};

export type WhatIfAffected = {
  entity: TwinEntity;
  state: "failed" | "degraded" | "exposed";
  severity: string;
  distance: number;
  confidence: number;
  path: {
    relationship_id: string | null;
    kind: string;
    source_id: string;
    target_id: string;
    evidence: unknown[];
  }[];
  recovery_options: RecoveryOption[];
  covered_by: { person_id: string; name: string }[];
};

export type RecoveryWaveStep = {
  entity_id: string;
  entity_name: string;
  state: string;
  action: string;
  owner: string;
  note: string;
};

export type WhatIfSimulation = {
  scenario: {
    kind: string;
    name: string;
    template_id: string | null;
    seed_ids: string[];
    seed_names: string[];
    horizon_days: number;
  };
  affected: WhatIfAffected[];
  failed: string[];
  degraded: string[];
  exposed: string[];
  money_at_risk: MoneyAtRisk;
  recovery_plan: {
    waves: RecoveryWaveStep[][];
    blocked_by_dependency_cycle: TwinEntity[];
    uncovered: { entity: TwinEntity; reason: string }[];
  };
  rehearsal: {
    dry_run: boolean;
    steps: {
      number: number;
      wave: number;
      entity_id: string;
      entity_name: string;
      action: string;
      owner: string;
      note: string;
      practice_only: boolean;
    }[];
    side_effects: unknown[];
    note: string;
  } | null;
  truncated: boolean;
  dry_run: boolean;
  fingerprint: string;
  assumptions: string[];
  as_of: string | null;
};

export type WhatIfResult = {
  tenant_id: string;
  snapshot_fingerprint: string;
  snapshot_as_of: string;
  sources: TwinSource[];
  stale_sources: string[];
  simulation: WhatIfSimulation;
};

export type AbsenceAffected = {
  entity: TwinEntity;
  distance: number;
  confidence: number;
  path: WhatIfAffected["path"];
  covered_by: { person_id: string; name: string }[];
};

export type AbsencePrediction = {
  absent_ids: string[];
  absent_names: string[];
  absent_person_ids: string[];
  horizon_days: number;
  affected: AbsenceAffected[];
  uncovered: { entity: TwinEntity; reason: string }[];
  money_at_risk: MoneyAtRisk;
  recovery_waves: TwinEntity[][];
  blocked_by_dependency_cycle: TwinEntity[];
  truncated: boolean;
  assumptions: string[];
  as_of: string | null;
  fingerprint: string;
  risk_score: number;
  factors: string[];
  predicted_at: string;
};

export type PredictAbsenceResult = {
  tenant_id: string;
  snapshot_fingerprint: string;
  snapshot_as_of: string;
  sources: TwinSource[];
  stale_sources: string[];
  prediction: AbsencePrediction;
};

export type TwinVersion = {
  id: string; seq: number; tenant_id: string; fingerprint: string; as_of: string; created_at: string;
  drift_score: number | null; trust: string | null; entity_count: number | null; relationship_count: number | null;
  stale_sources: string[]; source_freshness: { name: string; stale?: boolean; status?: string }[];
  is_first_version: boolean;
};
export type TwinHealth = {
  fingerprint: string; as_of: string | null; health_score: number; band: string; trust: string | null;
  drift_score: number | null; is_first_version: boolean;
  components: { drift: number; money: number; freshness: number };
  stale_sources: string[]; stale_source_count: number;
};
export type TwinTrustSource = { source: string; status: string; weight: number; known_source: boolean; score: number | null };
export type TwinTrust = {
  score: number | null; band: string; coverage: number; sources: TwinTrustSource[];
  stale_sources: string[]; drift_score: number | null; drift_trust: string | null;
  fingerprint?: string; as_of?: string;
};
export type TwinFinding = {
  id: string; tenant_id: string; drill_run_id: string; scenario_kind: string; simulation_fingerprint: string;
  code: string; resource_id: string; severity: string; source: string; detail: string;
  money_difference: Record<string, number> | null; money_redacted: boolean; fingerprint: string;
  acknowledged: boolean; acknowledged_at: string | null;
};
export type TwinProposalFinding = {
  source: string; code: string; resource_id: string; severity: string; fingerprint: string;
  money: Record<string, number> | null; detail: string;
};
export type TwinProposal = {
  id: string; tenant_id: string; source: string; finding_fingerprint: string; finding: TwinProposalFinding;
  money_redacted: boolean; proposal_fingerprint: string; action_type: string; action: Record<string, unknown>;
  action_hash: string; approver_class: string; required_permission: string; duty: string; status: string;
  approval_id: string | null; execution_id: string | null; external_ref: string | null;
  proposed_by: string; decided_by: string | null; created_at: string;
};
export type TwinHealthAlert = {
  id: string; tenant_id: string; rule_id: string; severity: string; message: string;
  health_score: number; drift_score: number | null; evaluated_at: string;
};
