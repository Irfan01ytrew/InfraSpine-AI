export const TEMPLATES: string[];
export const CADENCES: string[];
export const CHANNELS: string[];
export const WEEKDAYS: string[];

export function templateLabel(template: string): string;
export function channelLabel(channel: string): string;
export function channelLabels(channels: string[] | undefined): string[];
export function cadenceLabel(cadence: string): string;
export function weekdayLabel(weekday: number): string;
export function deliveryStatusLabel(status: string): string;

export function scheduleSummary(schedule: {
  template: string;
  cadence: string;
  time_of_day?: string;
  timezone?: string;
  weekday?: number;
  channels?: string[];
  enabled?: boolean;
}): string;

export function validateScheduleForm(form: {
  template: string;
  cadence: string;
  time_of_day: string;
  weekday: number;
  channels: string[];
  recipients: string[];
  quiet_start: string;
  quiet_end: string;
  overdue_days: number;
}): Record<string, string>;

export function deliveryRowLabel(delivery: {
  channel: string;
  status: string;
  sent_at?: string | null;
  created_at?: string | null;
}): string;
