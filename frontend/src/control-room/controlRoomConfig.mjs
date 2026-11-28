// Step AF slice 5: pure helpers for the alert configuration form.
//
// The server returns one effective_rules row per rule: the current value
// next to its default. The form only sends an override for a rule whose
// values differ from the default, so an untouched rule returns to its
// default simply by clearing the override. Sending everything would bury
// the defaults under redundant overrides and make "reset" impossible.

export const ALERT_CHANNELS = ["whatsapp", "email", "in_app"];

/** Override for one rule row, or an empty object when nothing changed. */
export function ruleOverride(row) {
  const override = {};
  if (row.threshold !== row.default_threshold) override.threshold = row.threshold;
  if (row.cooldown_seconds !== row.default_cooldown_seconds) {
    override.cooldown_seconds = row.cooldown_seconds;
  }
  if (row.enabled !== row.default_enabled) override.enabled = row.enabled;
  if (row.breaks_quiet_hours !== row.default_breaks_quiet_hours) {
    override.breaks_quiet_hours = row.breaks_quiet_hours;
  }
  const channels = [...(row.channels ?? [])].sort();
  const defaults = [...(row.default_channels ?? [])].sort();
  if (channels.join(",") !== defaults.join(",")) override.channels = row.channels;
  return override;
}

/** Full PUT payload for the form state. Rules with no changes are omitted. */
export function buildConfigPayload(form) {
  const rules = {};
  for (const row of form.rules ?? []) {
    const override = ruleOverride(row);
    if (Object.keys(override).length > 0) rules[row.key] = override;
  }
  return {
    enabled: form.enabled,
    quiet_start: form.quietStart,
    quiet_end: form.quietEnd,
    recipients: form.recipients ?? [],
    rules,
  };
}

/** Inline recipient check mirroring the server rule; "" means valid. */
export function recipientError(channel, address) {
  const value = (address ?? "").trim();
  if (channel === "whatsapp") {
    return /^\+?[1-9]\d{6,14}$/.test(value)
      ? ""
      : "WhatsApp needs an E.164 number, e.g. +923001234567.";
  }
  if (channel === "email") {
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value) ? "" : "Enter a valid email address.";
  }
  return "Unknown channel.";
}
