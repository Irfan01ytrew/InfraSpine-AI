import { request } from "../api";
import type { DrillComparison, Rehearsal, ScenarioTemplate, SimulationResult } from "./types";

const base = "/api/whatif";

export const whatIfApi = {
  scenarios: () => request<{ scenarios: ScenarioTemplate[] }>("GET", `${base}/scenarios`),
  simulate: (t: string, payload: { template_id?: string; kind?: string; seed_ids: string[]; horizon_days?: number; depth?: number; rehearse?: boolean }) =>
    request<SimulationResult>("POST", `${base}/${t}/simulate`, payload),
  startRehearsal: (t: string, payload: { template_id?: string; kind?: string; seed_ids: string[]; horizon_days?: number; depth?: number; title?: string }) =>
    request<Rehearsal>("POST", `${base}/${t}/rehearsals`, payload),
  rehearsals: (t: string) => request<{ rehearsals: Rehearsal[] }>("GET", `${base}/${t}/rehearsals`),
  rehearsal: (t: string, id: string) => request<Rehearsal>("GET", `${base}/${t}/rehearsals/${id}`),
  checkOff: (t: string, id: string, payload: { step_number: number; finding?: string }) =>
    request<Rehearsal>("POST", `${base}/${t}/rehearsals/${id}/check-off`, payload),
  skip: (t: string, id: string, payload: { step_number: number; reason: string }) =>
    request<Rehearsal>("POST", `${base}/${t}/rehearsals/${id}/skip`, payload),
  complete: (t: string, id: string, payload: { summary?: string }) =>
    request<Rehearsal>("POST", `${base}/${t}/rehearsals/${id}/complete`, payload),
  compareDrill: (t: string, runId: string, payload: { continuity_tenant_id: string; template_id?: string; kind?: string; seed_ids: string[]; horizon_days?: number; depth?: number }) =>
    request<DrillComparison>("POST", `${base}/${t}/drills/${runId}/compare`, payload),
};
