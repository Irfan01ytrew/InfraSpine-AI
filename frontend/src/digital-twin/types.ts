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
