export interface Tenant { id: string; name: string; role: "owner" | "approver" | "editor" | "viewer" }
export interface Evidence { source: string; reference: string; quote: string; observed_at: string }
export interface GraphRecord {
  id: string; record_type: "entity" | "relationship"; external_key: string; kind: string; name: string;
  source_id: string | null; target_id: string | null; department_id: string | null;
  visibility: "tenant" | "department" | "restricted"; readers: string[]; attributes: Record<string, unknown>;
  evidence: Evidence[]; confidence: number; inferred: boolean; retired: boolean;
  valid_from: string; valid_to: string | null; observed_at: string; version: number; updated_at: string;
  validation_status: "validated";
}
export type RecordInput = Omit<GraphRecord, "id" | "version" | "updated_at" | "validation_status">;
export interface Graph { entities: GraphRecord[]; relationships: GraphRecord[]; truncated: boolean; next_cursor?: string | null; scope: string; as_of: string; fingerprint?: string }
export interface Change { id: string; subject_id: string; change_type: "record" | "member"; payload: Record<string, unknown>; base_version: number; status: string; requested_by: string; expires_at: string; reason: string | null }
export interface Impact {
  scenario: string; failed_ids: string[]; truncated: boolean; assumptions: string[]; as_of: string;
  affected: { entity: GraphRecord; distance: number; confidence: number;
    path: { relationship_id: string; kind: string; source_id: string; target_id: string; evidence: Evidence[] }[];
    authorized_backups: { entity: GraphRecord; relationship_id: string; evidence: Evidence[]; confidence: number }[] }[];
  department_ids: string[];
}
export interface Quality {
  ownership_gaps: GraphRecord[]; missing_backups: GraphRecord[]; stale_records: GraphRecord[];
  conflicts: { kind: string; record_ids?: string[]; entity_id?: string; owner_ids?: string[] }[];
  resolution_candidates: string[][]; critical_entities: { entity: GraphRecord; affected_count: number; without_backup: number }[];
  entity_count: number; relationship_count: number; truncated: boolean; scope: string;
}
export interface GraphEvent { id: string; type: string; subject_id: string; record: GraphRecord; occurred_at: string; actor_id: string }
export interface Source { id: string; name: string; kind: string; stale_after_days: number; last_synced_at: string | null }
export interface Member { user_id: string; email: string; role: Tenant["role"]; departments: string[]; all_departments: boolean; active: boolean; version: number }
export interface QueryResult { answer: string; intent: string; choices?: GraphRecord[]; result?: Impact | Quality; relationships?: GraphRecord[]; entities?: GraphRecord[]; truncated: boolean }
