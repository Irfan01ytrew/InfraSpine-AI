// Step Q control-room API client: the read-only overview (slice 1) and the
// approval-gated controls (slice 2). Every control is two calls —
// request, then execute after a different manager approves it in the
// approvals inbox — because no control runs without a matching, unexpired,
// single-use, exact-action approval. This client never invents an approval
// id and never calls execute without one the backend issued.
import { describeError, request } from "../api";
import { workflowApi } from "../workflows/api";
import type { Scope } from "../workflows/types";

export type { Scope };

export { describeError };


export interface LatestRun {
  id: string;
  status: string;
  failure_code: string | null;
  llm_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface AgentEntry {
  id: string;
  name: string;
  owner_id: string;
  enabled: boolean;
  paused: boolean;
  latest_run: LatestRun | null;
}

export interface RunSummary {
  id: string;
  workflow_id: string;
  workflow_name: string;
  status: string;
  phase: string;
  origin: string;
  origin_ref: string | null;
  blocked_reason: string | null;
  failure_code: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface BlockerEntry {
  kind: string;
  run_id: string;
  origin: string;
  reason: string | null;
  created_at: string;
}

export interface ControlRoomOverview {
  scope_id: string;
  worker: { engine_on: boolean };
  runs_by_status: Record<string, number>;
  runs_by_origin: Record<string, number>;
  agents: AgentEntry[];
  recent_runs: RunSummary[];
  blockers: BlockerEntry[];
}

export interface ApprovalRequestOut {
  id: string;
  action_type: string;
  action_hash: string;
  status: string;
  expires_at: string;
}

export type RunCommand = "pause" | "resume";
export type AgentCommand = "pause" | "resume" | "assign";


export interface SpendRow {
  month: string;
  department: string;
  currency: string;
  amount: string;
  count: number;
}

export interface Spend {
  months: string[];
  from: string;
  statuses: string[];
  departments: string[];
  currencies: string[];
  rows: SpendRow[];
}

export interface FailedRun {
  id: string;
  workflow_id: string;
  workflow_name: string;
  failure_code: string | null;
  blocked_reason: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface ActiveAlert {
  rule_key: string;
  severity: "critical" | "warning";
  title: string;
  detail: string;
  evaluated_at: string;
}

export interface ControlRoomCharts {
  scope_id: string;
  generated_at: string;
  spend: Spend;
  invoices_by_status: { total: number; by_status: Record<string, number> };
  runs: {
    total: number;
    by_status: Record<string, number>;
    failed_last_hour: number;
    recent_failed: FailedRun[];
    last_activity_at: string | null;
  };
  health: {
    worker_enabled: boolean;
    last_run_activity_at: string | null;
    last_run_activity_age_seconds: number | null;
    ingestion_workers_online: number;
    llm_quota_max_percent: number | null;
  };
  active_alerts: ActiveAlert[];
  alert_summary: { critical: number; warning: number; total: number };
}

export interface AlertRecipient {
  channel: "whatsapp" | "email";
  address: string;
}

export interface EffectiveRule {
  key: string;
  title: string;
  severity: "critical" | "warning";
  threshold: number;
  cooldown_seconds: number;
  enabled: boolean;
  breaks_quiet_hours: boolean;
  channels: string[];
  default_threshold: number;
  default_cooldown_seconds: number;
  default_enabled: boolean;
  default_breaks_quiet_hours: boolean;
  default_channels: string[];
}

export interface AlertConfig {
  scope_id: string;
  enabled: boolean;
  quiet_start: string;
  quiet_end: string;
  recipients: AlertRecipient[];
  rules: Record<string, Record<string, unknown>>;
  effective_rules: EffectiveRule[];
}

export interface AlertDelivery {
  id: string;
  rule_key: string;
  severity: "critical" | "warning";
  title: string;
  detail: string;
  channel: string;
  recipient: string | null;
  status: "pending" | "sending" | "sent" | "failed" | "skipped";
  evaluated_at: string;
  sent_at: string | null;
  error: string | null;
}

const root = "/api/control-room";
const scopePath = (scopeId: string) => `${root}/scopes/${encodeURIComponent(scopeId)}`;

export const controlRoomApi = {
  scopes: () => workflowApi.scopes(),
  members: (scopeId: string) => workflowApi.members(scopeId),
  overview: (scopeId: string, limit = 10) =>
    request<ControlRoomOverview>("GET", `${scopePath(scopeId)}/overview?limit=${limit}`),
  requestRunControl: (scopeId: string, runId: string, command: RunCommand) =>
    request<ApprovalRequestOut>("POST", `${scopePath(scopeId)}/runs/${encodeURIComponent(runId)}/control-request`, { command }),
  executeRunControl: (scopeId: string, runId: string, command: RunCommand, approvalId: string) =>
    request<RunSummary>("POST", `${scopePath(scopeId)}/runs/${encodeURIComponent(runId)}/control`, { command, approval_id: approvalId }),
  requestAgentControl: (scopeId: string, agentId: string, command: AgentCommand, assigneeId?: string) =>
    request<ApprovalRequestOut>("POST", `${scopePath(scopeId)}/agents/${encodeURIComponent(agentId)}/control-request`, {
      command,
      ...(assigneeId ? { assignee_id: assigneeId } : {}),
    }),
  executeAgentControl: (scopeId: string, agentId: string, command: AgentCommand, approvalId: string, assigneeId?: string) =>
    request<AgentEntry>("POST", `${scopePath(scopeId)}/agents/${encodeURIComponent(agentId)}/control`, {
      command,
      approval_id: approvalId,
      ...(assigneeId ? { assignee_id: assigneeId } : {}),
    }),
  /** Workflow scopes the caller may view (same list as the workflow engine). */
  charts: (scopeId: string) =>
    request<ControlRoomCharts>("GET", `${root}/scopes/${encodeURIComponent(scopeId)}/charts`),
  /** Alert configuration for a scope (admin). */
  config: (scopeId: string) =>
    request<AlertConfig>("GET", `${root}/scopes/${encodeURIComponent(scopeId)}/config`),
  updateConfig: (scopeId: string, payload: {
    enabled: boolean;
    quiet_start: string;
    quiet_end: string;
    recipients: AlertRecipient[];
    rules: Record<string, Record<string, unknown>>;
  }) =>
    request<AlertConfig>("PUT", `${root}/scopes/${encodeURIComponent(scopeId)}/config`, payload),
  /** Recent alert deliveries for a scope, newest first. */
  deliveries: (scopeId: string, limit = 50) =>
    request<{ deliveries: AlertDelivery[] }>(
      "GET",
      `${root}/scopes/${encodeURIComponent(scopeId)}/deliveries?limit=${limit}`,
    ),
};
