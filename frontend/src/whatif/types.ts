export interface ScenarioTemplate {
  template_id: string;
  kind: string;
  name: string;
  description: string;
  horizon_days: number;
}

export interface SimStep {
  entity_id: string;
  entity_name: string;
  state: string;
  action: string;
  owner: string;
  note: string;
}

export interface SimulationResult {
  scenario: { kind: string; name: string; template_id: string | null; seed_ids: string[]; seed_names: string[]; horizon_days: number };
  affected: unknown[];
  failed: unknown[];
  degraded: unknown[];
  exposed: unknown[];
  money_at_risk: { totals: Record<string, number>; batch_count: number };
  recovery_plan: { waves: SimStep[][]; blocked_by_dependency_cycle: unknown[]; uncovered: unknown[] };
  rehearsal: { dry_run: boolean; steps: unknown[]; side_effects: string[]; note: string } | null;
  dry_run: boolean;
  assumptions: string[];
  truncated: boolean;
}

export interface RehearsalStep {
  number: number;
  wave: number;
  entity_id: string;
  entity_name: string;
  action: string;
  owner: string;
  note: string;
  practice_only: boolean;
  resource_id?: string;
  status: "pending" | "done" | "skipped";
  checked_off_by: string | null;
  checked_off_by_name: string | null;
  checked_off_at: string | null;
  finding: string | null;
  skipped_reason: string | null;
}

export interface Rehearsal {
  id: string;
  tenant_id: string;
  title: string;
  scenario: { kind: string; name: string; template_id: string | null; seed_ids: string[]; seed_names: string[]; horizon_days: number };
  status: "in_progress" | "completed";
  dry_run: boolean;
  side_effects: string[];
  practice_only: boolean;
  steps: RehearsalStep[];
  started_by: string;
  started_by_name: string | null;
  started_at: string;
  completed_at: string | null;
  completed_by: string | null;
  completed_by_name: string | null;
  summary: string | null;
  findings: string[];
}

export interface DrillComparisonStep {
  key: string;
  entity_name?: string;
  title?: string;
  owner?: string;
}

export interface DrillComparison {
  matched: { simulated: DrillComparisonStep; drill: DrillComparisonStep; owner_matches: boolean }[];
  missing: DrillComparisonStep[];
  extra: DrillComparisonStep[];
  owner_mismatches: { simulated: DrillComparisonStep; drill: DrillComparisonStep; owner_matches: boolean }[];
  out_of_order: string[];
  duplicate_drill_keys: string[];
  summary: {
    simulated_count: number;
    drill_count: number;
    matched_count: number;
    missing_count: number;
    extra_count: number;
    owner_mismatch_count: number;
    out_of_order_count: number;
    coverage: number | null;
    owner_match_rate: number | null;
    drill_verified_count: number;
    drill_verified_rate: number | null;
  };
  findings: string[];
  scenario: { kind: string | null; name: string | null; template_id: string | null; seed_ids: string[]; seed_names: string[]; horizon_days: number | null };
  run: { id: string | null; mode: string | null; status: string | null; verified_steps: number | null; total_steps: number | null };
}
