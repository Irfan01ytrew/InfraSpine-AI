import type { DisruptionKind, Overview, PlanProgress, Simulation } from "./types";
import { request } from "../api";

const scope = (id: string) => `/api/resilience/scopes/${encodeURIComponent(id)}`;

export const resilienceApi = {
  overview: (id: string, signal?: AbortSignal) =>
    request<Overview>("GET", `${scope(id)}/overview`, undefined, signal),
  simulate: (id: string, kind: DisruptionKind, target_ids: string[], duration_days: number) =>
    request<Simulation>("POST", `${scope(id)}/simulate`, { kind, target_ids, duration_days }),
  plan: (id: string, planId: string) =>
    request<PlanProgress>("GET", `${scope(id)}/plans/${encodeURIComponent(planId)}`),
  exportUrl: (id: string) => `${scope(id)}/audit/export?format=csv`,
};
