export type DecisionKind = "policy" | "finance" | "operations" | "supplier" | "people" | "technology" | "other";
export type DecisionStatus = "active" | "superseded" | "revoked" | "expired";

export interface Evidence {
  kind: string;
  ref: string;
  label: string;
}

export interface Decision {
  id: string;
  scope_id: string;
  title: string;
  rationale: string;
  kind: DecisionKind;
  status: DecisionStatus;
  decided_by: string;
  decided_at: string | null;
  alternatives_considered: string[];
  evidence: Evidence[];
  hidden_evidence: number;
  supersedes: string[];
  depends_on: string[];
  review_at: string | null;
  superseded_by: string | null;
  superseded_at: string | null;
  needs_review: boolean;
  fingerprint: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface DecisionScope {
  id: string;
  kind: string;
  name: string;
}

export interface ReviewDigest {
  scope_id: string;
  as_of: string;
  days_ahead: number;
  overdue_count: number;
  due_soon_count: number;
  total_active: number;
  needs_review_count: number;
  due: Decision[];
}

export interface Dependencies {
  decision: Decision;
  depends_on: Decision[];
  depended_on_by: Decision[];
  supersedes: Decision[];
  superseded_by: Decision | null;
}

export type ReviewOutcome = "confirm" | "expire" | "supersede" | "revoke";

export interface ReviewInput {
  outcome: ReviewOutcome;
  reason: string;
  review_at?: string;
  title?: string;
  rationale?: string;
  kind?: DecisionKind;
  decided_by?: string;
  decided_at?: string;
  alternatives_considered?: string[];
  evidence?: Evidence[];
  depends_on?: string[];
  decision_id?: string;
  approval_id?: string;
}

export interface DecisionInput {
  title: string;
  rationale: string;
  kind?: DecisionKind;
  decided_by?: string;
  decided_at?: string;
  alternatives_considered?: string[];
  evidence?: Evidence[];
  depends_on?: string[];
  review_at?: string;
  decision_id?: string;
  approval_id?: string;
}
