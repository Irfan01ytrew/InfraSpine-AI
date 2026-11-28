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

const root = "/api/control-room";

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
};
