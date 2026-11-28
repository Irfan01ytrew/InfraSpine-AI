import { request } from "../api";
import type {
  AgentReport,
  AgentSkill,
  Budget,
  DelegationAudit,
  DelegationPolicy,
  LifecycleEvent,
  OrgQuota,
  OrgReport,
  RegistryAgent,
  Skill,
  SpendReport,
} from "./types";

function workforceRequest<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(body === undefined ? "GET" : "POST", "/api/workforce" + path, body, signal);
}

function workforcePut<T>(path: string, body: unknown): Promise<T> {
  return request<T>("PUT", "/api/workforce" + path, body);
}

function workforceDelete(path: string): Promise<void> {
  return request<void>("DELETE", "/api/workforce" + path);
}

export const workforceApi = {
  agents: (signal?: AbortSignal) => workforceRequest<RegistryAgent[]>("/agents", undefined, signal),
  agent: (kind: string, id: string) =>
    workforceRequest<RegistryAgent>(`/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`),
  history: (kind: string, id: string) =>
    workforceRequest<LifecycleEvent[]>(`/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/history`),
  pause: (kind: string, id: string, reason: string) =>
    workforceRequest<RegistryAgent>(`/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/pause`, { reason }),
  resume: (kind: string, id: string, reason: string) =>
    workforceRequest<RegistryAgent>(`/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/resume`, { reason }),
  retire: (kind: string, id: string, reason: string) =>
    workforceRequest<RegistryAgent>(`/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/retire`, { reason }),
  applyOwnerRevocation: (ownerId: string, reason: string) =>
    workforceRequest<{ agents_paused: number; agents_already_paused: number; agents_retired_skipped: number; skills_frozen: number }>(
      "/owner-revocations", { owner_id: ownerId, reason }),
  skills: () => workforceRequest<Skill[]>("/skills"),
  createSkill: (name: string, description = "", category: string | null = null) =>
    workforceRequest<Skill>("/skills", { name, description, category }),
  agentSkills: (kind: string, id: string) =>
    workforceRequest<AgentSkill[]>(`/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/skills`),
  attachSkill: (kind: string, id: string, skillId: string) =>
    workforceRequest<AgentSkill>(`/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/skills`, { skill_id: skillId }),
  verifySkill: (kind: string, id: string, skillId: string) =>
    workforceRequest<AgentSkill>(`/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/skills/${encodeURIComponent(skillId)}/verify`, {}),
  revokeSkill: (kind: string, id: string, skillId: string) =>
    workforceRequest<AgentSkill>(`/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/skills/${encodeURIComponent(skillId)}/revoke`, {}),
  removeSkill: (kind: string, id: string, skillId: string) =>
    workforceDelete(`/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/skills/${encodeURIComponent(skillId)}`),
  budget: (kind: string, id: string) =>
    workforceRequest<Budget>(`/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/budget`),
  setBudget: (kind: string, id: string, budget: {
    daily_spend_limit_usd?: number | null;
    monthly_spend_limit_usd?: number | null;
    daily_call_limit?: number | null;
    monthly_call_limit?: number | null;
  }) => workforcePut<Budget>(`/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/budget`, budget),
  clearBudget: (kind: string, id: string) =>
    workforceDelete(`/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/budget`),
  checkStart: (kind: string, id: string) =>
    workforceRequest<{ allowed: boolean; reason: string }>(`/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/budget/check`),
  orgQuota: () => workforceRequest<OrgQuota>("/org-quota"),
  setOrgQuota: (quota: { max_agents?: number | null; monthly_spend_limit_usd?: number | null }) =>
    workforcePut<OrgQuota>("/org-quota", quota),
  policies: () => workforceRequest<DelegationPolicy[]>("/delegation-policies"),
  createPolicy: (rules: DelegationPolicy["rules"], maxDepth: number) =>
    workforceRequest<DelegationPolicy>("/delegation-policies", { rules, max_depth: maxDepth }),
  delegations: () => workforceRequest<DelegationAudit[]>("/delegations"),
  orgReport: (days = 30, signal?: AbortSignal) =>
    workforceRequest<OrgReport>(`/report/org?days=${days}`, undefined, signal),
  spendReport: (days = 30) => workforceRequest<SpendReport>(`/report/spend?days=${days}`),
  agentReport: (kind: string, id: string, days = 30) =>
    workforceRequest<AgentReport>(`/report/agents/${encodeURIComponent(kind)}/${encodeURIComponent(id)}?days=${days}`),
};
