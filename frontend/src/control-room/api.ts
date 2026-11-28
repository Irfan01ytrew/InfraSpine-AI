import { request } from "../api";
import { workflowApi } from "../workflows/api";
import type { Scope } from "../workflows/types";

export type { Scope };

export interface SpendRow {
  month: string;
  department: string;
  currency: string;
  amount: string;
  count: number;
}

export interface Spend {
  months: string[];
  from: string;
  statuses: string[];
  departments: string[];
  currencies: string[];
  rows: SpendRow[];
}

export interface FailedRun {
  id: string;
  workflow_id: string;
  workflow_name: string;
  failure_code: string | null;
  blocked_reason: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface ActiveAlert {
  rule_key: string;
  severity: "critical" | "warning";
  title: string;
  detail: string;
  evaluated_at: string;
}

export interface ControlRoomCharts {
  scope_id: string;
  generated_at: string;
  spend: Spend;
  invoices_by_status: { total: number; by_status: Record<string, number> };
  runs: {
    total: number;
    by_status: Record<string, number>;
    failed_last_hour: number;
    recent_failed: FailedRun[];
    last_activity_at: string | null;
  };
  health: {
    worker_enabled: boolean;
    last_run_activity_at: string | null;
    last_run_activity_age_seconds: number | null;
    ingestion_workers_online: number;
    llm_quota_max_percent: number | null;
  };
  active_alerts: ActiveAlert[];
  alert_summary: { critical: number; warning: number; total: number };
}

export interface AlertRecipient {
  channel: "whatsapp" | "email";
  address: string;
}

export interface EffectiveRule {
  key: string;
  title: string;
  severity: "critical" | "warning";
  threshold: number;
  cooldown_seconds: number;
  enabled: boolean;
  breaks_quiet_hours: boolean;
  channels: string[];
  default_threshold: number;
  default_cooldown_seconds: number;
  default_enabled: boolean;
  default_breaks_quiet_hours: boolean;
  default_channels: string[];
}

export interface AlertConfig {
  scope_id: string;
  enabled: boolean;
  quiet_start: string;
  quiet_end: string;
  recipients: AlertRecipient[];
  rules: Record<string, Record<string, unknown>>;
  effective_rules: EffectiveRule[];
}

export interface AlertDelivery {
  id: string;
  rule_key: string;
  severity: "critical" | "warning";
  title: string;
  detail: string;
  channel: string;
  recipient: string | null;
  status: "pending" | "sending" | "sent" | "failed" | "skipped";
  evaluated_at: string;
  sent_at: string | null;
  error: string | null;
}

const root = "/api/control-room";

export const controlRoomApi = {
  /** Workflow scopes the caller may view (same list as the workflow engine). */
  scopes: () => workflowApi.scopes(),
  charts: (scopeId: string) =>
    request<ControlRoomCharts>("GET", `${root}/scopes/${encodeURIComponent(scopeId)}/charts`),
  /** Alert configuration for a scope (admin). */
  config: (scopeId: string) =>
    request<AlertConfig>("GET", `${root}/scopes/${encodeURIComponent(scopeId)}/config`),
  updateConfig: (scopeId: string, payload: {
    enabled: boolean;
    quiet_start: string;
    quiet_end: string;
    recipients: AlertRecipient[];
    rules: Record<string, Record<string, unknown>>;
  }) =>
    request<AlertConfig>("PUT", `${root}/scopes/${encodeURIComponent(scopeId)}/config`, payload),
  /** Recent alert deliveries for a scope, newest first. */
  deliveries: (scopeId: string, limit = 50) =>
    request<{ deliveries: AlertDelivery[] }>(
      "GET",
      `${root}/scopes/${encodeURIComponent(scopeId)}/deliveries?limit=${limit}`,
    ),
};
