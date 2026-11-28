const ACTIVATION = /^(?:\/agent|irfanix|عرفانکس|ارفانکس)(?=$|[\s,:،!؟])(?:[\s,:،!؟]+)?/iu;
const GENERATED_REPLY = /^InfraSpine AI(?:\s|$)/iu;
export const FOLLOWUP_WINDOW_MS = 5 * 60 * 1000;
const MAX_COMMAND_CHARS = 6000;

export function activationFor(body, now, activeUntil) {
  if (typeof body !== 'string') return null;
  const text = body.trim();
  if (!text || GENERATED_REPLY.test(text)) return null;
  const prefix = text.match(ACTIVATION);
  if (!prefix && now >= activeUntil) return null;
  const command = prefix ? text.slice(prefix[0].length).trim() : text;
  if (command.length > MAX_COMMAND_CHARS) return null;
  return { text: command || 'Hello', activeUntil: now + FOLLOWUP_WINDOW_MS };
}
