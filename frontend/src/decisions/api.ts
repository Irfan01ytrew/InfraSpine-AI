import { request } from "../api";
import type { Decision, DecisionInput, DecisionScope, Dependencies, ReviewDigest, ReviewInput } from "./types";

function decisionsRequest<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(body === undefined ? "GET" : "POST", "/api/decisions" + path, body, signal);
}

export const decisionsApi = {
  scopes: () => decisionsRequest<{ scopes: DecisionScope[] }>("/scopes"),
  list: (scopeId: string, params?: { kind?: string; status?: string; include_archived?: boolean; limit?: number }, signal?: AbortSignal) => {
    const query = new URLSearchParams();
    if (params?.kind) query.set("kind", params.kind);
    if (params?.status) query.set("status", params.status);
    if (params?.include_archived) query.set("include_archived", "true");
    if (params?.limit) query.set("limit", String(params.limit));
    const suffix = query.size ? `?${query.toString()}` : "";
    return decisionsRequest<{ decisions: Decision[] }>(`/scopes/${encodeURIComponent(scopeId)}${suffix}`, undefined, signal);
  },
  get: (scopeId: string, decisionId: string, signal?: AbortSignal) =>
    decisionsRequest<Decision>(`/scopes/${encodeURIComponent(scopeId)}/${encodeURIComponent(decisionId)}`, undefined, signal),
  create: (scopeId: string, body: DecisionInput) =>
    decisionsRequest<Decision>(`/scopes/${encodeURIComponent(scopeId)}`, body),
  why: (scopeId: string, subject: string, signal?: AbortSignal) =>
    decisionsRequest<{ subject: string; decisions: Decision[] }>(
      `/scopes/${encodeURIComponent(scopeId)}/why?subject=${encodeURIComponent(subject)}`, undefined, signal),
  dependencies: (scopeId: string, decisionId: string, signal?: AbortSignal) =>
    decisionsRequest<Dependencies>(`/scopes/${encodeURIComponent(scopeId)}/${encodeURIComponent(decisionId)}/dependencies`, undefined, signal),
  verify: (scopeId: string, decisionId: string) =>
    decisionsRequest<{ decision_id: string; fingerprint: string; valid: boolean }>(
      `/scopes/${encodeURIComponent(scopeId)}/${encodeURIComponent(decisionId)}/verify`),
  due: (scopeId: string, daysAhead = 7, signal?: AbortSignal) =>
    decisionsRequest<{ scope_id: string; as_of: string; days_ahead: number; due: Decision[] }>(
      `/scopes/${encodeURIComponent(scopeId)}/reviews/due?days_ahead=${daysAhead}`, undefined, signal),
  digest: (scopeId: string, daysAhead = 7, signal?: AbortSignal) =>
    decisionsRequest<ReviewDigest>(
      `/scopes/${encodeURIComponent(scopeId)}/reviews/digest?days_ahead=${daysAhead}`, undefined, signal),
  review: (scopeId: string, decisionId: string, body: ReviewInput) =>
    decisionsRequest<Decision>(`/scopes/${encodeURIComponent(scopeId)}/${encodeURIComponent(decisionId)}/review`, body),
  revoke: (scopeId: string, decisionId: string, body: { reason: string; approval_id?: string }) =>
    decisionsRequest<Decision>(`/scopes/${encodeURIComponent(scopeId)}/${encodeURIComponent(decisionId)}/revoke`, body),
  requestApproval: (scopeId: string, body: { action_type: string; decision_id: string; kind: string; superseded_id?: string; ttl_seconds?: number }) =>
    decisionsRequest<{ approval_id: string; action_hash: string; status: string; expires_at: string | null }>(
      `/scopes/${encodeURIComponent(scopeId)}/approvals`, body),
};
