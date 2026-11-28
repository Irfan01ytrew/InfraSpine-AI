import { request } from "../api";
import type { TwinFinding, TwinHealth, TwinHealthAlert, TwinProposal, TwinTrust, TwinVersion } from "./types";

const base = "/api/digital-twin";
const path = (tenantId: string, suffix: string) => `${base}/${encodeURIComponent(tenantId)}${suffix}`;

export const twinApi = {
  versions: (tenantId: string) =>
    request<{ tenant_id: string; versions: TwinVersion[]; count: number }>("GET", path(tenantId, "/versions")),
  health: (tenantId: string) =>
    request<{ tenant_id: string; health: TwinHealth }>("GET", path(tenantId, "/health")),
  healthHistory: (tenantId: string) =>
    request<{ tenant_id: string; history: TwinHealth[]; count: number }>("GET", path(tenantId, "/health/history")),
  healthAlerts: (tenantId: string) =>
    request<{ tenant_id: string; alerts: TwinHealthAlert[]; count: number }>("GET", path(tenantId, "/health/alerts")),
  trust: (tenantId: string) =>
    request<{ tenant_id: string; trust: TwinTrust }>("GET", path(tenantId, "/trust")),
  trustHistory: (tenantId: string) =>
    request<{ tenant_id: string; history: TwinTrust[]; count: number }>("GET", path(tenantId, "/trust/history")),
  findings: (tenantId: string) =>
    request<{ tenant_id: string; findings: TwinFinding[]; count: number }>("GET", path(tenantId, "/reconciliation/findings")),
  findingCounts: (tenantId: string) =>
    request<{ tenant_id: string; counts: Record<string, number> }>("GET", path(tenantId, "/reconciliation/counts")),
  acknowledgeFinding: (tenantId: string, findingId: string) =>
    request<{ tenant_id: string; finding: TwinFinding }>(
      "POST", path(tenantId, `/reconciliation/findings/${encodeURIComponent(findingId)}/acknowledge`), {}),
  proposals: (tenantId: string) =>
    request<{ tenant_id: string; proposals: TwinProposal[]; count: number }>("GET", path(tenantId, "/proposals")),
  proposalCounts: (tenantId: string) =>
    request<{ tenant_id: string; counts: Record<string, number> }>("GET", path(tenantId, "/proposals/counts")),
  decideProposal: (tenantId: string, proposalId: string, decision: "approve" | "reject", presentedHash: string, note?: string) =>
    request<{ tenant_id: string; proposal: TwinProposal }>(
      "POST", path(tenantId, `/proposals/${encodeURIComponent(proposalId)}/decide`),
      { decision, presented_hash: presentedHash, note: note ?? "" }),
  executeProposal: (tenantId: string, proposalId: string) =>
    request<{ tenant_id: string; proposal: TwinProposal; status: string }>(
      "POST", path(tenantId, `/proposals/${encodeURIComponent(proposalId)}/execute`), {}),
  cancelProposal: (tenantId: string, proposalId: string) =>
    request<{ tenant_id: string; proposal: TwinProposal }>(
      "POST", path(tenantId, `/proposals/${encodeURIComponent(proposalId)}/cancel`), {}),
};
