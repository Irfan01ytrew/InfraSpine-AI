import { request } from "../api";

export type ReportTemplate = "daily_cash_summary" | "weekly_invoices" | "overdue_alert";
export type ReportCadence = "daily" | "weekly";
export type ReportChannel = "whatsapp" | "email" | "in_app";
export type DeliveryStatus = "pending" | "sending" | "sent" | "failed" | "retry" | "cancelled";

export interface ReportSchedule {
  id: string;
  template: ReportTemplate;
  cadence: ReportCadence;
  time_of_day: string;
  timezone: string;
  weekday: number;
  channels: ReportChannel[];
  recipients: string[];
  quiet_start: string;
  quiet_end: string;
  overdue_days: number;
  enabled: boolean;
  last_sent_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ReportDelivery {
  id: string;
  schedule_id: string;
  period_start: string;
  channel: ReportChannel;
  recipient_user_id: string;
  status: DeliveryStatus;
  attempts: number;
  next_attempt_at: string | null;
  last_error_code: string | null;
  provider_ref: string | null;
  sent_at: string | null;
  created_at: string;
}

export interface ReportPreference {
  id: string;
  user_id: string;
  template: ReportTemplate;
  channels: ReportChannel[];
  cadence: ReportCadence;
  enabled: boolean;
  quiet_start: string | null;
  quiet_end: string | null;
}

export interface ScheduleInput {
  template: ReportTemplate;
  cadence: ReportCadence;
  time_of_day: string;
  timezone: string;
  weekday: number;
  channels: ReportChannel[];
  recipients: string[];
  quiet_start: string;
  quiet_end: string;
  overdue_days: number;
  enabled: boolean;
}

export interface PreferenceInput {
  template: ReportTemplate;
  channels: ReportChannel[];
  cadence: ReportCadence;
  enabled: boolean;
  quiet_start: string | null;
  quiet_end: string | null;
}

const root = "/api/reports";
const call = <T,>(method: string, path: string, body?: unknown) => request<T>(method, root + path, body);

export const reportsApi = {
  schedules: () => call<{ schedules: ReportSchedule[] }>("GET", "/schedules"),
  createSchedule: (input: ScheduleInput) =>
    call<ReportSchedule>("POST", "/schedules", input),
  updateSchedule: (id: string, input: Partial<ScheduleInput>) =>
    call<ReportSchedule>("PATCH", `/schedules/${encodeURIComponent(id)}`, input),
  deleteSchedule: (id: string) =>
    call<{ deleted: boolean; id: string }>("DELETE", `/schedules/${encodeURIComponent(id)}`),
  deliveries: (status?: string) => {
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    return call<{ deliveries: ReportDelivery[] }>("GET", `/deliveries${query}`);
  },
  preferences: () => call<{ preferences: ReportPreference[] }>("GET", "/preferences"),
  upsertPreference: (template: ReportTemplate, input: PreferenceInput) =>
    call<ReportPreference>("PUT", `/preferences/${encodeURIComponent(template)}`, input),
};
