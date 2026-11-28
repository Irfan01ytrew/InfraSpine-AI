import assert from "node:assert/strict";
import test from "node:test";

import {
  buildConfigPayload,
  recipientError,
  ruleOverride,
} from "../src/control-room/controlRoomConfig.mjs";

function rule(overrides = {}) {
  return {
    key: "failed_runs_spike",
    title: "Failed runs spiking",
    severity: "warning",
    threshold: 5,
    cooldown_seconds: 3600,
    enabled: true,
    breaks_quiet_hours: false,
    channels: ["whatsapp", "email", "in_app"],
    default_threshold: 5,
    default_cooldown_seconds: 3600,
    default_enabled: true,
    default_breaks_quiet_hours: false,
    default_channels: ["whatsapp", "email", "in_app"],
    ...overrides,
  };
}

test("ruleOverride is empty when nothing changed from the default", () => {
  assert.deepEqual(ruleOverride(rule()), {});
});

test("ruleOverride only carries the fields that changed", () => {
  assert.deepEqual(ruleOverride(rule({ threshold: 10 })), { threshold: 10 });
  assert.deepEqual(ruleOverride(rule({ enabled: false })), { enabled: false });
  assert.deepEqual(ruleOverride(rule({ breaks_quiet_hours: true })), { breaks_quiet_hours: true });
  assert.deepEqual(ruleOverride(rule({ cooldown_seconds: 900 })), { cooldown_seconds: 900 });
  assert.deepEqual(ruleOverride(rule({ channels: ["whatsapp"] })), { channels: ["whatsapp"] });
  assert.deepEqual(ruleOverride(rule({ channels: ["in_app", "email", "whatsapp"] })), {});
});

test("buildConfigPayload omits untouched rules and keeps changed ones", () => {
  const payload = buildConfigPayload({
    enabled: true,
    quietStart: "23:00",
    quietEnd: "06:00",
    recipients: [{ channel: "whatsapp", address: "+923001234567" }],
    rules: [rule(), rule({ key: "failed_payment", threshold: 2 })],
  });
  assert.deepEqual(payload, {
    enabled: true,
    quiet_start: "23:00",
    quiet_end: "06:00",
    recipients: [{ channel: "whatsapp", address: "+923001234567" }],
    rules: { failed_payment: { threshold: 2 } },
  });
});

test("buildConfigPayload tolerates a missing rules list", () => {
  assert.deepEqual(
    buildConfigPayload({ enabled: false, quietStart: "22:00", quietEnd: "07:00" }).rules,
    {},
  );
});

test("recipientError accepts E.164 numbers and emails, rejects the rest", () => {
  assert.equal(recipientError("whatsapp", "+923001234567"), "");
  assert.equal(recipientError("whatsapp", "03001234567"), "WhatsApp needs an E.164 number, e.g. +923001234567.");
  assert.equal(recipientError("whatsapp", "+92"), "WhatsApp needs an E.164 number, e.g. +923001234567.");
  assert.equal(recipientError("email", "owner@example.com"), "");
  assert.equal(recipientError("email", "not-an-email"), "Enter a valid email address.");
});
