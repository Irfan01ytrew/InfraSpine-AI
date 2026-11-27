import { request } from "../api";
import type { Baseline, Job, Model, ModelSummary, Organization, Process, Simulation, Source } from "./types";

const root = "/api/process-intelligence/v1";
export const pathFor = (tenant: string, process: string) => `${root}/${encodeURIComponent(tenant)}/processes/${encodeURIComponent(process)}`;
export const processApi = {
  organizations: (signal?: AbortSignal) => request<{ items: Organization[] }>("GET", `${root}/organizations`, undefined, signal),
  createOrganization: (name: string) => request<Organization>("POST", `${root}/organizations`, { name }),
  processes: (tenant: string, signal?: AbortSignal) => request<{ items: Process[] }>("GET", `${root}/${tenant}/processes`, undefined, signal),
  createProcess: (tenant: string, name: string) => request<Process>("POST", `${root}/${tenant}/processes`, { name }),
  sources: (path: string, signal?: AbortSignal) => request<{ items: Source[] }>("GET", `${path}/sources`, undefined, signal),
  models: (path: string, signal?: AbortSignal) => request<{ items: ModelSummary[] }>("GET", `${path}/models`, undefined, signal),
  model: (path: string, id: string, signal?: AbortSignal) => request<Model>("GET", `${path}/models/${id}`, undefined, signal),
  analyze: (path: string) => request<Model>("POST", `${path}/analyze`),
  jobs: (path: string) => request<Job[]>("GET", `${path}/jobs`),
  baselines: (path: string, signal?: AbortSignal) => request<{ items: Baseline[] }>("GET", `${path}/baselines`, undefined, signal),
  simulate: (path: string, body: { model_id: string; actor: string; absence_hours: number; replacement_capacity: number }) => request<Simulation>("POST", `${path}/simulate`, body),
  async export(path: string, model: string, format: "json" | "csv") {
    const response = await fetch(`${path}/models/${model}/export?format=${format}`, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error("Export is unavailable. Refresh your source permissions and try again.");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a"); link.href = url; link.download = `process-${model}.${format}`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
};

export function hours(value: number | null) { return value === null ? "—" : `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })}h`; }
export function date(value: string) { return new Date(value).toLocaleString(); }
