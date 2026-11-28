import { request } from "../api";

export type SelfHealingIncident = {
  id: string;
  key: string;
  kind: string;
  severity: string;
  component: string;
  summary: string;
  evidence: string;
  status: string;
  attempts: number;
  last_attempt_at: string | null;
  first_seen_at: string;
  updated_at: string;
};

export type SelfHealingEscalation = {
  id: string;
  incident_key: string;
  kind: string;
  severity: string;
  component: string;
  summary: string;
  evidence: string;
  attempts: number;
  channel: string;
  delivered_at: string;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
};

export type SelfHealingTestRun = {
  id: string;
  suite: string;
  status: string;
  passed: number;
  failed: number;
  errors: number;
  skipped: number;
  duration_seconds: number;
  failure_names: string[];
  started_at: string;
  finished_at: string | null;
};

export type ContinuityPlanOption = { id: string; title: string; status: string; version: number };
export type ContinuityOption = {
  tenant_id: string;
  tenant_name: string;
  role: string;
  plans: ContinuityPlanOption[];
};

export type SelfHealingOverview = {
  incidents: { total: number; by_severity: Record<string, number>; worst: string | null };
  open_incident_count: number;
  unacknowledged_escalations: number;
  latest_probes: Record<string, { status: string; age_seconds: number; recorded_at: string }>;
};

export type FilingResult = {
  outcome: string;
  event_id: string | null;
  incident_id: string | null;
  duplicate: boolean;
};

export const sh = {
  overview: () => request<SelfHealingOverview>("GET", "/api/self-healing/overview"),
  incidents: () => request<{ items: SelfHealingIncident[]; total: number }>("GET", "/api/self-healing/incidents?limit=50"),
  escalations: () =>
    request<{ items: SelfHealingEscalation[]; total: number }>("GET", "/api/self-healing/escalations?limit=50"),
  acknowledge: (key: string) =>
    request<SelfHealingEscalation>("POST", `/api/self-healing/escalations/${encodeURIComponent(key)}/acknowledge`),
  testRuns: () =>
    request<{ items: SelfHealingTestRun[]; total: number }>("GET", "/api/self-healing/test-runs?limit=20"),
  continuityOptions: () =>
    request<{ items: ContinuityOption[] }>("GET", "/api/self-healing/continuity-options"),
  fileEvent: (tenantId: string, incidentKey: string) =>
    request<FilingResult>("POST", "/api/self-healing/recovery/file-event", {
      tenant_id: tenantId,
      incident_key: incidentKey,
    }),
};
