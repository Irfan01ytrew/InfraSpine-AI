import test from "node:test";
import assert from "node:assert/strict";

import {
  cadenceLabel,
  channelLabel,
  channelLabels,
  deliveryRowLabel,
  deliveryStatusLabel,
  scheduleSummary,
  templateLabel,
  validateScheduleForm,
  weekdayLabel,
} from "../src/reports/scheduledReportsView.mjs";

const BASE_FORM = {
  template: "daily_cash_summary",
  cadence: "daily",
  time_of_day: "08:00",
  weekday: 0,
  channels: ["whatsapp"],
  recipients: ["user-1"],
  quiet_start: "22:00",
  quiet_end: "07:00",
  overdue_days: 7,
};

test("labels fall back to the raw value for unknown entries", () => {
  assert.equal(templateLabel("daily_cash_summary"), "Daily cash summary");
  assert.equal(templateLabel("weekly_invoices"), "Weekly invoices");
  assert.equal(templateLabel("overdue_alert"), "Overdue alert");
  assert.equal(templateLabel("nope"), "nope");
  assert.equal(channelLabel("whatsapp"), "WhatsApp");
  assert.equal(channelLabel("email"), "Email");
  assert.equal(channelLabel("in_app"), "In-app");
  assert.equal(deliveryStatusLabel("retry"), "Retrying");
  assert.equal(deliveryStatusLabel("sent"), "Sent");
  assert.equal(cadenceLabel("weekly"), "Weekly");
  assert.equal(weekdayLabel(5), "Saturday");
  assert.equal(weekdayLabel(9), "");
});

test("schedule summary reads like a sentence", () => {
  assert.equal(
    scheduleSummary({
      template: "daily_cash_summary", cadence: "daily", time_of_day: "08:00",
      timezone: "Asia/Karachi", channels: ["whatsapp", "in_app"], enabled: true,
    }),
    "Daily cash summary · Daily at 08:00 Asia/Karachi · WhatsApp, In-app",
  );
  assert.equal(
    scheduleSummary({
      template: "weekly_invoices", cadence: "weekly", time_of_day: "08:00",
      timezone: "Asia/Karachi", weekday: 5, channels: ["email"], enabled: false,
    }),
    "Weekly invoices · Weekly at 08:00 Asia/Karachi · on Saturday · Email · disabled",
  );
  assert.deepEqual(channelLabels(["whatsapp", "email"]), ["WhatsApp", "Email"]);
});

test("schedule form validation accepts a good form and flags each bad field", () => {
  assert.deepEqual(validateScheduleForm(BASE_FORM), {});
  assert.equal(validateScheduleForm({ ...BASE_FORM, template: "nope" }).template, "Choose a report template.");
  assert.equal(validateScheduleForm({ ...BASE_FORM, time_of_day: "8am" }).time_of_day, "Enter a time such as 08:00.");
  assert.equal(
    validateScheduleForm({ ...BASE_FORM, cadence: "weekly", weekday: 9 }).weekday,
    "Choose a weekday for the weekly report.",
  );
  assert.equal(validateScheduleForm({ ...BASE_FORM, channels: [] }).channels, "Choose at least one channel.");
  assert.equal(validateScheduleForm({ ...BASE_FORM, recipients: [] }).recipients, "Choose at least one recipient.");
  assert.equal(
    validateScheduleForm({ ...BASE_FORM, quiet_start: "22:00", quiet_end: "22:00" }).quiet_hours,
    "Quiet hours must not cover the whole day.",
  );
  assert.equal(validateScheduleForm({ ...BASE_FORM, overdue_days: -1 }).overdue_days, "Overdue days must be 0 or more.");
});

test("delivery row label shows channel, status and time", () => {
  assert.equal(
    deliveryRowLabel({ channel: "whatsapp", status: "sent", sent_at: "2026-11-28T08:05:00+05:00", created_at: "" }),
    "WhatsApp · Sent · 2026-11-28 08:05",
  );
  assert.equal(
    deliveryRowLabel({ channel: "email", status: "retry", sent_at: null, created_at: "2026-11-28T08:00:00+05:00" }),
    "Email · Retrying · 2026-11-28 08:00",
  );
});
