/** Pure view helpers for scheduled reports (Step AA, slice 5). No I/O, no React. */

export const TEMPLATES = ["daily_cash_summary", "weekly_invoices", "overdue_alert"];
export const CADENCES = ["daily", "weekly"];
export const CHANNELS = ["whatsapp", "email", "in_app"];
export const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

const TEMPLATE_LABELS = {
  daily_cash_summary: "Daily cash summary",
  weekly_invoices: "Weekly invoices",
  overdue_alert: "Overdue alert",
};

const CHANNEL_LABELS = {
  whatsapp: "WhatsApp",
  email: "Email",
  in_app: "In-app",
};

const STATUS_LABELS = {
  pending: "Pending",
  sending: "Sending",
  sent: "Sent",
  failed: "Failed",
  retry: "Retrying",
  cancelled: "Cancelled",
};

export function templateLabel(template) {
  return TEMPLATE_LABELS[template] ?? String(template ?? "");
}

export function channelLabel(channel) {
  return CHANNEL_LABELS[channel] ?? String(channel ?? "");
}

export function channelLabels(channels) {
  return (channels ?? []).map(channelLabel);
}

export function cadenceLabel(cadence) {
  if (cadence === "weekly") return "Weekly";
  if (cadence === "daily") return "Daily";
  return String(cadence ?? "");
}

export function weekdayLabel(weekday) {
  return WEEKDAYS[weekday] ?? "";
}

export function deliveryStatusLabel(status) {
  return STATUS_LABELS[status] ?? String(status ?? "");
}

export function scheduleSummary(schedule) {
  const parts = [
    templateLabel(schedule.template),
    `${cadenceLabel(schedule.cadence)} at ${schedule.time_of_day ?? ""} ${schedule.timezone ?? ""}`.trim(),
  ];
  if (schedule.cadence === "weekly") parts.push(`on ${weekdayLabel(schedule.weekday)}`);
  parts.push(channelLabels(schedule.channels).join(", "));
  if (schedule.enabled === false) parts.push("disabled");
  return parts.filter(Boolean).join(" · ");
}

function isTime(value) {
  return typeof value === "string" && /^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$/.test(value);
}

/** Validate the schedule create form. Returns `{}` when valid, else field errors. */
export function validateScheduleForm(form) {
  const errors = {};
  if (!TEMPLATES.includes(form.template)) errors.template = "Choose a report template.";
  if (!CADENCES.includes(form.cadence)) errors.cadence = "Choose daily or weekly.";
  if (!isTime(form.time_of_day)) errors.time_of_day = "Enter a time such as 08:00.";
  if (form.cadence === "weekly" && !(Number.isInteger(form.weekday) && form.weekday >= 0 && form.weekday <= 6)) {
    errors.weekday = "Choose a weekday for the weekly report.";
  }
  if (!Array.isArray(form.channels) || form.channels.length === 0) {
    errors.channels = "Choose at least one channel.";
  } else if (form.channels.some((channel) => !CHANNELS.includes(channel))) {
    errors.channels = "Unknown channel.";
  }
  if (!Array.isArray(form.recipients) || form.recipients.length === 0) {
    errors.recipients = "Choose at least one recipient.";
  }
  if (!isTime(form.quiet_start) || !isTime(form.quiet_end)) {
    errors.quiet_hours = "Quiet hours need a start and an end time.";
  } else if (form.quiet_start === form.quiet_end) {
    errors.quiet_hours = "Quiet hours must not cover the whole day.";
  }
  if (!(Number.isInteger(form.overdue_days) && form.overdue_days >= 0)) {
    errors.overdue_days = "Overdue days must be 0 or more.";
  }
  return errors;
}

/** One-line label for a delivery history row. */
export function deliveryRowLabel(delivery) {
  const when = delivery.sent_at ?? delivery.created_at ?? "";
  const date = when ? String(when).slice(0, 16).replace("T", " ") : "";
  return `${channelLabel(delivery.channel)} · ${deliveryStatusLabel(delivery.status)}${date ? ` · ${date}` : ""}`;
}
