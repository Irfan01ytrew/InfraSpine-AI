// Types for ./controlRoomConfig.mjs (Step AF slice 5 pure helpers).
// Added at integration: the .mjs shipped without a declaration file, so
// tsc failed with TS7016 on the ControlRoomPage import. Same pattern as
// chartData.d.mts / whatIfPanel.d.mts.
import type { AlertRecipient, EffectiveRule } from "./api";

export const ALERT_CHANNELS: string[];

export function ruleOverride(
  row: EffectiveRule,
): Record<string, unknown>;

export function buildConfigPayload(form: {
  enabled: boolean;
  quietStart: string;
  quietEnd: string;
  recipients?: AlertRecipient[];
  rules?: EffectiveRule[];
}): {
  enabled: boolean;
  quiet_start: string;
  quiet_end: string;
  recipients: AlertRecipient[];
  rules: Record<string, Record<string, unknown>>;
};

export function recipientError(
  channel: string,
  address: string | null | undefined,
): string;
