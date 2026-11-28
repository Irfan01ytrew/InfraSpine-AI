/**
 * Types for the Step L Predictive Continuity API
 * (`/api/predictive-continuity/{tenant_id}/...`). Field names match
 * the server serializers in `app/predictive_continuity/records.py`,
 * `proposals_records.py` and `reconciliation_records.py` exactly.
 * Money the server redacts for non-owners arrives as `null` and is
 * rendered as hidden — the page never reconstructs it.
 */

export type ForecastTrust = "trustworthy" | "review" | "stale" | "unknown";

export interface MoneyAtRisk {
  totals: Record<string, number>;
  batch_count: number;
  payment_count: number;
}

export interface ForecastLiveWorkItem {
  id: string;
  name: string;
  kind: string;
  status: string | null;
}

export interface ForecastProcess {
  n: number;
  id: string;
  name: string;
  owner_ids: string[];
  owner_names: string[];
  dependencies: { id: string; name: string; kind: string }[];
  distance: number;
  confidence: number;
  money_at_risk: MoneyAtRisk;
  due_status: "due_within_horizon" | "at_risk_later" | "no_deadline";
  due_at: string | null;
  reason: string | null;
  covered_by: { person_id: string; name: string }[] | null;
  live_work_items: ForecastLiveWorkItem[];
}

export interface ForecastDict {
  unavailable: { id: string; name: string; kind: string }[];
  horizon_days: number;
  horizon_end: string;
  forecast_at: string;
  fingerprint: string;
  processes: ForecastProcess[];
  money_at_risk: MoneyAtRisk;
  money_at_risk_due: MoneyAtRisk;
  counts: {
    processes: number;
    processes_due_within_horizon: number;
    affected: number;
    uncovered: number;
  };
}

export interface ForecastRecordView {
  id: string;
  tenant_id: string;
  root_id: string;
  root_kind: string;
  horizon_days: number;
  fingerprint: string;
  as_of: string | null;
  superseded: boolean;
  drift_score: number | null;
  trust: ForecastTrust;
  created_at: string | null;
  forecast: ForecastDict;
}

export interface ForecastFinding {
  source: string;
  code: string;
  resource_id: string;
  severity: "critical" | "warning" | "info";
  fingerprint: string;
  money: MoneyAtRisk | null;
  detail: string;
}

export type ProposalStatus =
  | "draft"
  | "pending_approval"
  | "approved"
  | "rejected"
  | "expired"
  | "cancelled"
  | "dispatched"
  | "verified"
  | "failed";

export interface ForecastProposal {
  id: string;
  tenant_id: string;
  source: string;
  finding_fingerprint: string;
  finding: ForecastFinding;
  money_redacted: boolean;
  proposal_fingerprint: string;
  action_type: string;
  action: Record<string, unknown>;
  action_hash: string;
  approver_class: string | null;
  required_permission: string;
  duty: string;
  status: ProposalStatus;
  approval_id: string | null;
  execution_id: string | null;
  external_ref: string | null;
  proposed_by: string;
  decided_by: string | null;
  created_at: string;
}

export interface ForecastReconciliation {
  id: string;
  tenant_id: string;
  forecast_record_id: string;
  drill_run_id: string;
  drill_kind: string | null;
  forecast_fingerprint: string;
  matches: boolean;
  worst_severity: string;
  findings: ForecastFinding[];
  counts: Record<string, number>;
  adjusted_drift_score: number | null;
  money_redacted: boolean;
  acknowledged: boolean;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  created_at: string;
}
