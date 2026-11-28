/**
 * Thin client for the Step L Predictive Continuity API. Same
 * `request` helper every other page uses (HttpOnly session cookie;
 * no token is ever seen or stored here). All calls are tenant
 * scoped; the backend returns 404 for non-members and 403 for
 * members without `forecasts.read`, and redacts finding money for
 * non-owners.
 */
import { request } from "../api";
import type { ForecastProposal, ForecastRecordView, ForecastReconciliation } from "./types";

const base = "/api/predictive-continuity";

export const forecastApi = {
  forecasts: (tenantId: string, includeSuperseded = false, limit = 50) =>
    request<{ tenant_id: string; forecasts: ForecastRecordView[]; count: number }>(
      "GET",
      `${base}/${tenantId}/forecasts?include_superseded=${includeSuperseded}&limit=${limit}`,
    ),
  forecast: (tenantId: string, recordId: string) =>
    request<ForecastRecordView>("GET", `${base}/${tenantId}/forecasts/${recordId}`),
  proposals: (tenantId: string, limit = 30) =>
    request<{ tenant_id: string; proposals: ForecastProposal[]; count: number }>(
      "GET",
      `${base}/${tenantId}/proposals?limit=${limit}`,
    ),
  proposalCounts: (tenantId: string) =>
    request<{ tenant_id: string; counts: Record<string, number> }>(
      "GET",
      `${base}/${tenantId}/proposals/counts`,
    ),
  createProposal: (tenantId: string, body: {
    forecast_record_id: string;
    finding_code: string;
    resource_id: string;
    action_kind: string;
  }) =>
    request<{ tenant_id: string; proposal: ForecastProposal; created: boolean; approval_id: string }>(
      "POST",
      `${base}/${tenantId}/proposals`,
      body,
    ),
  decideProposal: (tenantId: string, proposalId: string, decision: "approve" | "reject", presentedHash: string, note = "") =>
    request<{ tenant_id: string; proposal: ForecastProposal }>(
      "POST",
      `${base}/${tenantId}/proposals/${proposalId}/decide`,
      { decision, presented_hash: presentedHash, note },
    ),
  executeProposal: (tenantId: string, proposalId: string) =>
    request<{ tenant_id: string; proposal: ForecastProposal }>(
      "POST",
      `${base}/${tenantId}/proposals/${proposalId}/execute`,
    ),
  cancelProposal: (tenantId: string, proposalId: string) =>
    request<{ tenant_id: string; proposal: ForecastProposal }>(
      "POST",
      `${base}/${tenantId}/proposals/${proposalId}/cancel`,
    ),
  reconciliations: (tenantId: string, limit = 30) =>
    request<{ tenant_id: string; reconciliations: ForecastReconciliation[]; count: number }>(
      "GET",
      `${base}/${tenantId}/reconciliations?limit=${limit}`,
    ),
  reconciliationCounts: (tenantId: string) =>
    request<{ tenant_id: string; counts: Record<string, number> }>(
      "GET",
      `${base}/${tenantId}/reconciliations/counts`,
    ),
  acknowledgeReconciliation: (tenantId: string, reconciliationId: string) =>
    request<{ tenant_id: string; reconciliation: ForecastReconciliation }>(
      "POST",
      `${base}/${tenantId}/reconciliations/${reconciliationId}/acknowledge`,
    ),
};
