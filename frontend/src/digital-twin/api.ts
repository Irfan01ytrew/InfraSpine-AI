import { request } from "../api";
import type { PredictAbsenceResult, TwinOverview, WhatIfResult } from "./types";
import type { TwinFinding, TwinHealth, TwinHealthAlert, TwinProposal, TwinTrust, TwinVersion } from "./types";

const base = "/api/digital-twin";
const path = (tenantId: string, suffix: string) => `${base}/${encodeURIComponent(tenantId)}${suffix}`;

export const twinApi = {
  overview: (tenantId: string) =>
    request<TwinOverview>("GET", `${base}/${encodeURIComponent(tenantId)}/overview`),
  whatIf: (
    tenantId: string,
    payload: {
      template_id?: string;
      kind?: string;
      seed_ids: string[];
      horizon_days?: number;
      rehearse?: boolean;
    },
  ) => request<WhatIfResult>("POST", `${base}/${encodeURIComponent(tenantId)}/what-if`, payload),
  predictAbsence: (
    tenantId: string,
    payload: { absent_ids: string[]; horizon_days?: number; depth?: number },
  ) =>
    request<PredictAbsenceResult>(
      "POST",
      `${base}/${encodeURIComponent(tenantId)}/predict-absence`,
      payload,
    ),
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

/** Scenario templates the twin UI offers. Seed kinds are twin kinds;
 * the workspace maps them onto graph kinds (person -> employee,
 * team -> department) when listing seed candidates. */
export const TWIN_TEMPLATES = [
  {
    id: "key_employee_leaves",
    label: "Key employee leaves",
    description: "A person is unavailable (absence / resignation).",
    seedKinds: ["person"],
    defaultHorizonDays: 7,
  },
  {
    id: "server_down",
    label: "Server down",
    description: "A system or service is unavailable (outage).",
    seedKinds: ["system", "service"],
    defaultHorizonDays: 1,
  },
  {
    id: "supplier_fails",
    label: "Supplier fails",
    description: "A supplier stops delivering.",
    seedKinds: ["supplier"],
    defaultHorizonDays: 14,
  },
  {
    id: "cyber_incident",
    label: "Cyber incident",
    description: "A system is compromised; connected data may be exposed.",
    seedKinds: ["system", "service"],
    defaultHorizonDays: 3,
  },
  {
    id: "site_unavailable",
    label: "Site unavailable",
    description: "A site (office / warehouse) cannot be used.",
    seedKinds: ["site"],
    defaultHorizonDays: 7,
  },
] as const;

export const TWIN_KIND_TO_GRAPH_KINDS: Record<string, string[]> = {
  person: ["employee"],
  team: ["department"],
  system: ["system"],
  service: ["service"],
  supplier: ["supplier"],
  site: ["site"],
  data: ["data"],
  process: ["process"],
};
