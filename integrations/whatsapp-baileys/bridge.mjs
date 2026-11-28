// Private stdin/stdout protocol. No HTTP listener and no plaintext auth files.
import readline from 'node:readline';
import makeWASocket, { BufferJSON, DisconnectReason, downloadMediaMessage, initAuthCreds, jidNormalizedUser, proto } from '@whiskeysockets/baileys';
import pino from 'pino';
import QRCode from 'qrcode';
import { activationFor, FOLLOWUP_WINDOW_MS } from './activation.mjs';

// Same bound as the Python voice helpers (app/integrations/whatsapp_voice.py):
// a downloaded voice note larger than this is reported, not forwarded.
const MAX_VOICE_BYTES = 8 * 1024 * 1024;

console.log = () => {}; // Libraries must not leak session material into application logs.
const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
const logger = pino({ level: 'silent' });
let socket, creds, keys = {}, stopped = false, retries = 0, generation = 0;
let saving = Promise.resolve();
const acknowledgements = new Map();
const recent = new Set();
let activeUntil = 0;
const startedAt = Math.floor(Date.now() / 1000);

function persist() {
  const state = JSON.stringify({ creds, keys }, BufferJSON.replacer);
  saving = saving.then(() => new Promise((resolve, reject) => {
    const id = crypto.randomUUID();
    const timer = setTimeout(() => { acknowledgements.delete(id); reject(new Error('auth_persistence_timeout')); }, 20000);
    acknowledgements.set(id, () => { clearTimeout(timer); resolve(); });
    emit({ type: 'auth', id, state });
  }));
  return saving;
}

async function connect() {
  const current = ++generation;
  socket = makeWASocket({
    logger, auth: { creds, keys: {
      get: async (type, ids) => Object.fromEntries(ids.map(id => {
        let value = keys[type]?.[id];
        if (type === 'app-state-sync-key' && value) value = proto.Message.AppStateSyncKeyData.fromObject(value);
        return [id, value];
      })),
      set: async data => {
        for (const [type, entries] of Object.entries(data)) {
          keys[type] ??= {};
          for (const [id, value] of Object.entries(entries)) {
            if (value == null) delete keys[type][id]; else keys[type][id] = value;
          }
        }
        await persist();
      },
    } },
    markOnlineOnConnect: false,
    syncFullHistory: false,
    shouldSyncHistoryMessage: () => false,
    connectTimeoutMs: 30000,
    defaultQueryTimeoutMs: 30000,
  });
  socket.ev.on('creds.update', update => {
    Object.assign(creds, update);
    void persist().catch(() => fatal('session_storage_failed'));
  });
  socket.ev.on('connection.update', async update => {
    if (stopped || current !== generation) return;
    try {
      if (update.qr) emit({ type: 'qr', image: await QRCode.toDataURL(update.qr, { width: 280, margin: 2 }) });
      if (update.connection === 'open') {
        retries = 0;
        emit({ type: 'status', status: 'connected', suffix: jidNormalizedUser(socket.user?.id || '').split('@')[0].slice(-4) });
      }
      if (update.connection === 'close') {
        const code = update.lastDisconnect?.error?.output?.statusCode;
        if (code === DisconnectReason.loggedOut || code === DisconnectReason.badSession) {
          emit({ type: 'status', status: 'logged_out' });
          stopped = true; process.exitCode = 0; setTimeout(() => process.exit(0), 100);
        } else if (++retries <= 8) {
          emit({ type: 'status', status: 'reconnecting' });
          setTimeout(() => { if (!stopped) void connect().catch(() => fatal('connection_failed')); }, code === DisconnectReason.restartRequired ? 1000 : Math.min(30000, 1000 * 2 ** retries));
        } else fatal('connection_failed');
      }
    } catch { fatal('connection_failed'); }
  });
  socket.ev.on('messages.update', updates => {
    if (stopped || current !== generation) return;
    const self = new Set([socket.user?.id, socket.user?.lid].filter(Boolean).map(jidNormalizedUser));
    const { SERVER_ACK, DELIVERY_ACK, READ } = proto.WebMessageInfo.Status;
    for (const { key, update } of updates) {
      if (!key.fromMe || !self.has(jidNormalizedUser(key.remoteJid || ''))) continue;
      if (!/^[0-9A-F]{32}$/.test(key.id || '')) continue;
      if ([SERVER_ACK, DELIVERY_ACK, READ].includes(update.status)) {
        emit({ type: 'receipt', id: key.id, status: update.status });
      }
    }
  });
  socket.ev.on('messages.upsert', async ({ type, messages }) => {
    if (type !== 'notify' || stopped || current !== generation) return;
    const self = new Set([socket.user?.id, socket.user?.lid].filter(Boolean).map(jidNormalizedUser));
    for (const message of messages) {
      const remote = message.key.remoteJid || '';
      // Only the linked account's own self-chat, sent after this bridge started.
      // Neither other contacts, groups, old sync messages nor our replies trigger tools.
      if (!message.key.fromMe || !self.has(jidNormalizedUser(remote))) continue;
      if (Number(message.messageTimestamp || 0) < startedAt - 5) continue;
      const body = message.message?.conversation || message.message?.extendedTextMessage?.text || '';
      const audioMessage = message.message?.audioMessage;
      if (!message.key.id || recent.has(message.key.id)) continue;
      if (audioMessage && audioMessage.ptt === true) {
        // Step F slice 4: voice notes carry no text, so they cannot match
        // the wake word. They are commands only inside an active follow-up
        // window (the user said "/agent" or "irfanix" by text first). The
        // audio is downloaded here and forwarded as base64; Python
        // transcribes it and the transcript becomes the task.
        if (Date.now() >= activeUntil) continue;
        activeUntil = Date.now() + FOLLOWUP_WINDOW_MS;
        recent.add(message.key.id);
        if (recent.size > 2000) recent.delete(recent.values().next().value);
        try {
          const buffer = await downloadMediaMessage(message, 'buffer');
          if (buffer.length > MAX_VOICE_BYTES) {
            emit({ type: 'message', id: message.key.id, jid: remote, text: '', audioTooLarge: true });
          } else {
            emit({ type: 'message', id: message.key.id, jid: remote, text: '', audio: buffer.toString('base64'), mime: audioMessage.mimetype || 'audio/ogg' });
          }
        } catch {
          // A download failure stays silent: the note goes unanswered
          // rather than crashing the bridge.
        }
        continue;
      }
      const activation = activationFor(body, Date.now(), activeUntil);
      if (!activation) continue;
      activeUntil = activation.activeUntil;
      recent.add(message.key.id);
      if (recent.size > 2000) recent.delete(recent.values().next().value);
      emit({ type: 'message', id: message.key.id, jid: remote, text: activation.text });
    }
  });
}

function fatal(code) {
  emit({ type: 'error', code }); stopped = true;
  setTimeout(() => process.exit(1), 100);
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on('line', line => {
  if (line.length > 16 * 1024 * 1024) return fatal('protocol_limit');
  let command;
  try { command = JSON.parse(line); } catch { return fatal('protocol_invalid'); }
  if (command.type === 'ack') {
    acknowledgements.get(command.id)?.(); acknowledgements.delete(command.id); return;
  }
  void (async () => {
    if (command.type === 'init' && !creds) {
      const state = command.state ? JSON.parse(command.state, BufferJSON.reviver) : null;
      creds = state?.creds || initAuthCreds(); keys = state?.keys || {};
      await persist(); await connect();
    } else if (command.type === 'send' && socket && !stopped) {
      const self = new Set([socket.user?.id, socket.user?.lid].filter(Boolean).map(jidNormalizedUser));
      if (!self.has(jidNormalizedUser(command.jid))) throw new Error('recipient_denied');
      await socket.sendMessage(command.jid, { text: String(command.text).slice(0, 12000) }, { messageId: command.id });
      emit({ type: 'sent', id: command.id });
    } else if (command.type === 'logout') {
      stopped = true;
      try { await socket?.logout(); } finally { process.exit(0); }
    } else if (command.type === 'stop') {
      stopped = true; socket?.end(undefined); process.exit(0);
    }
  })().catch(() => command.type === 'send' ? emit({ type: 'send_error', id: command.id }) : fatal('bridge_operation_failed'));
});
input.on('close', () => { stopped = true; socket?.end(undefined); process.exit(0); });
setInterval(() => emit({ type: 'heartbeat' }), 15000).unref();
