import { request } from "../api";

export type Incident = {
  id: string; title: string; status: string; severity: string; source: string; category: string;
  owner_id: string; target_id: string; assignee_id: string | null; version: number; generation: number;
  occurrences: number; recovery_count: number; last_seen: string; opened_at: string; sla_due: string;
  verified_at: string | null; diagnosis: Record<string, unknown>;
};
export type Target = { id: string; name: string; adapter: string; resource: string; owner_id: string; enabled: boolean; healthy: boolean | null; last_checked: string | null; check_interval: number; threshold: number; sla_seconds: number };
export type Playbook = { id: string; name: string; adapter: string; action: string; risk: string; preapproved: boolean; enabled: boolean; max_attempts: number; verify_samples: number };
export type Command = { id: string; playbook_id: string; status: string; requester_id: string; action_digest: string; approval_expires: string | null; attempts: number; successes: number; last_error: string | null; created_at: string };
export type Timeline = { sequence: number; kind: string; at: string; actor: string; data: Record<string, unknown>; digest: string };
export type Detail = { incident: Incident; timeline: Timeline[]; commands: Command[]; related: Incident[]; audit_valid: boolean };
export type Config = { role: string; user_id: string; adapters: { id: string; actions: string[]; idempotent: boolean }[]; playbooks: Playbook[]; members: { user_id: string; role: string; active: boolean }[] };
export type Dashboard = {
  incidents: Incident[]; total: number; page_size: number; offset: number; targets: Target[]; as_of: string;
  metrics: { active: number; overdue: number; recurring: number; mttr_seconds: number | null; recovery_success_percent: number | null; by_state: Record<string, number>; commands: Record<string, number> };
};
export type Workspace = { id: string; name: string; role: string };
export const root = (tenant: string) => `/api/recovery/${encodeURIComponent(tenant)}`;
export const recovery = {
  workspaces: () => request<{ workspaces: Workspace[] }>("GET", "/api/recovery/workspaces"),
  createWorkspace: (name: string) => request<Workspace>("POST", "/api/recovery/workspaces", { name }),
  dashboard: (tenant: string, query: string, signal: AbortSignal) => request<Dashboard>("GET", `${root(tenant)}/dashboard?${query}`, undefined, signal),
  config: (tenant: string, signal: AbortSignal) => request<Config>("GET", `${root(tenant)}/configuration`, undefined, signal),
  detail: (tenant: string, id: string, signal: AbortSignal) => request<Detail>("GET", `${root(tenant)}/incidents/${id}`, undefined, signal),
  post: (tenant: string, path: string, body?: unknown) => request("POST", `${root(tenant)}/${path}`, body),
  toggle: (tenant: string, type: string, id: string, enabled: boolean) => request("PATCH", `${root(tenant)}/${type}/${id}`, { enabled }),
};
