import { request } from "../api";
import type { Change, Graph, GraphEvent, GraphRecord, Impact, Member, Quality, QueryResult, RecordInput, Source, Tenant } from "./types";

const base = "/api/company-graph";
export const graphApi = {
  tenants: () => request<{items: Tenant[]}>("GET", `${base}/tenants`),
  createTenant: (name: string) => request<Tenant>("POST", `${base}/tenants`, {name}),
  catalog: (t: string) => request<{entity_types: string[]; relationship_types: string[]}>("GET", `${base}/${t}/catalog`),
  map: (t: string, params = new URLSearchParams(), signal?: AbortSignal) => request<Graph>("GET", `${base}/${t}/map?${params}`, undefined, signal),
  record: (t: string, id: string) => request<GraphRecord>("GET", `${base}/${t}/records/${id}`),
  neighbors: (t: string, id: string, depth = 1) => request<Graph>("GET", `${base}/${t}/neighbors/${id}?depth=${depth}`),
  propose: (t: string, record: RecordInput, record_id?: string, base_version = 0) => request<Change>("POST", `${base}/${t}/changes`, {record, record_id, base_version}),
  changes: (t: string, after?: string) => request<{items: Change[]; next_cursor: string | null}>("GET", `${base}/${t}/changes${after ? `?after=${after}` : ""}`),
  decide: (t: string, id: string, approve: boolean, reason: string) => request<Change>("POST", `${base}/${t}/changes/${id}/decision`, {approve, reason}),
  events: (t: string, subject?: string, after?: string) => request<{events: GraphEvent[]; next_cursor: string | null; has_more: boolean}>("GET", `${base}/${t}/events?${new URLSearchParams({...subject ? {subject_id: subject} : {}, ...after ? {after} : {}})}`),
  simulate: (t: string, failed_ids: string[], scenario: string, depth: number) => request<Impact>("POST", `${base}/${t}/simulate`, {failed_ids, scenario, depth}),
  quality: (t: string) => request<Quality>("GET", `${base}/${t}/quality`),
  query: (t: string, question: string) => request<QueryResult>("POST", `${base}/${t}/query`, {question}),
  export: (t: string) => request<Graph>("GET", `${base}/${t}/export`),
  sources: (t: string) => request<{items: Source[]}>("GET", `${base}/${t}/sources`),
  createSource: (t: string, name: string, kind: string, stale_after_days: number) => request<Source>("POST", `${base}/${t}/sources`, {name, kind, stale_after_days}),
  sync: (t: string, source: string, payload: unknown) => request<{proposal_ids: string[]; unchanged_ids: string[]; conflicts: {key: string; reason: string}[]}>("POST", `${base}/${t}/sources/${source}/sync`, payload),
  extract: (t: string, source: string, text: string, reference: string) => request<{items: unknown[]}>("POST", `${base}/${t}/sources/${source}/extract`, {text, reference}),
  members: (t: string) => request<{items: Member[]}>("GET", `${base}/${t}/members`),
  memberChange: (t: string, payload: unknown) => request<Change>("POST", `${base}/${t}/members/changes`, payload),
};

export function downloadJson(data: unknown, name: string) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: "application/json"}));
  const a = document.createElement("a"); a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
