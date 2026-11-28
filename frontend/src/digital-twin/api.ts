import { request } from "../api";
import type { PredictAbsenceResult, TwinOverview, WhatIfResult } from "./types";

const base = "/api/digital-twin";

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
