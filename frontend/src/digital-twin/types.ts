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
