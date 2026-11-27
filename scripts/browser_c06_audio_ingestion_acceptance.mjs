import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const mode = process.argv[2];
const base = new URL(process.argv[3] ?? "http://127.0.0.1:5182/admin/");
const fixturePath = path.resolve(process.env.INFRASPINE_C11_REVIEW_FIXTURE ?? "");
const reviewRoot = path.resolve(process.env.INFRASPINE_C11_REVIEW_ROOT ?? "");
const audioPath = path.resolve(process.env.C06_SYNTHETIC_AUDIO ?? "");
const statePath = path.join(reviewRoot, "evidence", "c06-audio-browser-acceptance.json");
if (!["upload", "verify"].includes(mode) || base.protocol !== "http:" || base.hostname !== "127.0.0.1" ||
    !fixturePath || !reviewRoot || !audioPath || !existsSync(audioPath)) {
  throw new Error("C06 audio acceptance requires the loopback C11 stack and protected synthetic paths.");
}
const relativeAudio = path.relative(reviewRoot, audioPath);
if (relativeAudio.startsWith("..") || path.isAbsolute(relativeAudio) || path.basename(audioPath) !== "c06-synthetic-maintenance.wav") {
  throw new Error("The C06 audio source must be the staged synthetic WAV inside the protected C11 review folder.");
}
if (!existsSync(fixturePath)) throw new Error("The protected synthetic Admin fixture is unavailable.");
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
if (!fixture.admin?.email || !fixture.admin?.password) throw new Error("The protected synthetic Admin fixture is incomplete.");
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for C06 audio acceptance.");

mkdirSync(path.join(reviewRoot, "tmp"), { recursive: true });
mkdirSync(path.dirname(statePath), { recursive: true });
const profile = mkdtempSync(path.join(reviewRoot, "tmp", "browser-c06-audio-"));
let chrome;
let socket;
let sequence = 0;
const pending = new Map();
const networkRequests = new Map();
let uploadHttp;

function command(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out at ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression, timeoutMs = 30_000) {
  const result = await command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("A C06 browser action failed.");
  return result.result?.value;
}

async function waitFor(expression, label, timeoutMs = 45_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(100);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}

async function navigateToKnowledge() {
  await command("Page.navigate", { url: base.href });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]')) || Boolean(document.querySelector('.shell'))", "login or authenticated shell");
  if (await evaluate("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))")) {
    await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const pass=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!pass||!form)return false;for(const [el,value] of [[email,${JSON.stringify(fixture.admin.email)}],[pass,${JSON.stringify(fixture.admin.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
    await waitFor("Boolean(document.querySelector('.shell'))", "authenticated synthetic Admin", 45_000);
  }
  const clicked = await evaluate(`(()=>{const button=[...document.querySelectorAll('button')].find(el=>el.textContent.trim()==='Knowledge');if(!button)return false;button.click();return true})()`);
  if (!clicked) throw new Error("The Knowledge navigation button was unavailable.");
  await waitFor("Boolean(document.querySelector('.dropzone'))", "Knowledge upload screen");
}

try {
  chrome = spawn(browserPath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < deadline) await delay(100);
  if (!existsSync(portFile)) throw new Error("Isolated Chromium did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!page?.webSocketDebuggerUrl) throw new Error("Isolated Chromium did not expose a page target.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method === "Network.requestWillBeSent") {
      networkRequests.set(message.params.requestId, { url: message.params.request.url, method: message.params.request.method });
    }
    if (message.method === "Network.responseReceived") {
      const request = networkRequests.get(message.params.requestId);
      if (request?.url.includes("/api/rag/documents") && request.method === "POST") uploadHttp = message.params.response.status;
    }
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer);
    pending.delete(message.id);
    message.error ? item.reject(new Error(`DevTools command failed: ${message.error.message}`)) : item.resolve(message.result);
  });
  await command("Page.enable");
  await command("Runtime.enable");
  await command("Network.enable");
  await command("DOM.enable");
  await navigateToKnowledge();

  if (mode === "upload") {
    if (existsSync(statePath)) throw new Error("C06 evidence state already exists; refusing to overwrite it.");
    const audioBase64 = readFileSync(audioPath).toString("base64");
    const selected = await evaluate(`(()=>{const input=document.querySelector('input[type="file"][multiple]');if(!input)return false;const raw=atob(${JSON.stringify(audioBase64)});const bytes=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)bytes[i]=raw.charCodeAt(i);const transfer=new DataTransfer();transfer.items.add(new File([bytes],'c06-synthetic-maintenance.wav',{type:'audio/wav'}));input.files=transfer.files;const count=input.files.length;input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));return count===1})()`);
    if (!selected) throw new Error("The synthetic WAV was not selected by the Knowledge upload control.");
    const uploadDeadline = Date.now() + 25_000;
    while (uploadHttp === undefined && Date.now() < uploadDeadline) await delay(100);
    if (uploadHttp !== 202) throw new Error("The current-source audio upload did not return HTTP 202.");
    const deadline = Date.now() + 25_000;
    let uploaded;
    while (Date.now() < deadline) {
      uploaded = await evaluate(`fetch('/api/rag/documents',{credentials:'same-origin'}).then(async r=>({status:r.status,body:r.ok?await r.json():null}))`);
      const item = uploaded?.body?.documents?.find((row) => row.filename === "c06-synthetic-maintenance.wav");
      if (item) {
        if (item.status !== "pending" || item.file_format !== "audio") throw new Error("The C06 source was not queued as an audio document.");
        const state = {
          review_url: base.href,
          source_database: "infraspine_c11sqliteui_20261028_test",
          document_id: item.id,
          filename: item.filename,
          file_format: item.file_format,
          upload_http: uploadHttp,
          queued_status: item.status,
          submitted_sha256: createHash("sha256").update(readFileSync(audioPath)).digest("hex"),
          media_model: "local faster-whisper-tiny",
          attempts: [],
        };
        writeFileSync(statePath, `${JSON.stringify(state, null, 2)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
        console.log(JSON.stringify({ mode, upload_http: state.upload_http, status: state.queued_status, file_format: state.file_format, document_id: state.document_id }));
        break;
      }
      await delay(250);
    }
    if (!uploaded?.body?.documents?.some((row) => row.filename === "c06-synthetic-maintenance.wav")) {
      throw new Error("The C06 WAV did not appear in the authenticated document list.");
    }
  } else {
    if (!existsSync(statePath)) throw new Error("The C06 upload evidence state is unavailable.");
    const state = JSON.parse(readFileSync(statePath, "utf8").replace(/^\uFEFF/, ""));
    if (!state.document_id || state.filename !== "c06-synthetic-maintenance.wav" || state.file_format !== "audio") {
      throw new Error("The saved C06 document identity is invalid.");
    }
    const media = await evaluate(`fetch('/api/admin/media-transcription',{credentials:'same-origin'}).then(async r=>({status:r.status,body:r.ok?await r.json():null}))`);
    if (media.status !== 200 || !media.body?.enabled || !media.body?.runtime_available || !media.body?.model_available || media.body.status !== "ready") {
      throw new Error("The authenticated Admin media status did not report a ready local Whisper runtime.");
    }
    const ready = await evaluate(`fetch('/api/rag/documents',{credentials:'same-origin'}).then(r=>r.json()).then(j=>j.documents?.find(d=>d.id===${JSON.stringify(state.document_id)}))`);
    if (!ready || ready.status !== "ready" || !ready.searchable || ready.file_format !== "audio" || ready.chunk_count < 1 || ready.attempts !== 1) {
      throw new Error("The current-source document API did not expose a ready searchable audio file.");
    }
    if (process.env.C06_NEW_CONVERSATION === "1" && state.conversation_id) {
      state.pre_fix_conversation_id = state.conversation_id;
      state.pre_fix_follow_up_refused = true;
      state.support_classification_fix = "removed maintenance-only trigger for time-valued fact classification";
      delete state.conversation_id;
      writeFileSync(statePath, `${JSON.stringify(state, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
    }
    let conversationId = state.conversation_id;
    if (!conversationId) {
      const create = await evaluate(`fetch('/api/conversations',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'},body:'{}'}).then(async r=>({status:r.status,body:r.ok?await r.json():null}))`);
      if (create.status !== 201 || !create.body?.id) throw new Error("A persistent synthetic review conversation could not be created.");
      conversationId = create.body.id;
      state.conversation_id = conversationId;
      writeFileSync(statePath, `${JSON.stringify(state, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
    }
    let existing = await evaluate(`fetch('/api/conversations/${conversationId}',{credentials:'same-origin'}).then(async r=>({status:r.status,body:r.ok?await r.json():null}))`);
    if (existing.status !== 200 || !existing.body?.messages) throw new Error("The saved synthetic conversation could not be reopened.");
    let first;
    if (existing.body.messages.length >= 2) {
      first = { status: 200, body: { assistant_message: existing.body.messages[1] } };
    } else {
      first = await evaluate(`fetch('/api/conversations/${conversationId}/messages',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'},body:${JSON.stringify(JSON.stringify({question:"What ticket number was spoken in the maintenance recording?",document_ids:[state.document_id]}) )}}).then(async r=>({status:r.status,body:r.ok?await r.json():null}))`, 120_000);
    }
    if (first.status !== 200 || !first.body?.assistant_message?.response) throw new Error("The current-source cited audio answer request failed.");
    const firstResponse = first.body.assistant_message.response;
    const firstCitation = firstResponse.citations?.find((citation) => citation.document_id === state.document_id);
    const firstSnippet = String(firstCitation?.snippet ?? "");
    const normalizedTicketDigits = firstSnippet.replace(/\D/g, "");
    if (firstResponse.provider !== "mock" || firstResponse.insufficient_evidence || !firstCitation ||
        !/ticket number/i.test(firstSnippet) || !normalizedTicketDigits.includes("4821") || firstCitation.locator?.kind !== "time" ||
        !/^\d{2}:\d{2}:\d{2}\.\d{3}/.test(firstCitation.location ?? "")) {
      throw new Error("The transcript answer did not carry an exact synthetic marker and time-based source citation.");
    }
    existing = await evaluate(`fetch('/api/conversations/${conversationId}',{credentials:'same-origin'}).then(async r=>({status:r.status,body:r.ok?await r.json():null}))`);
    if (existing.status !== 200 || !existing.body?.messages) throw new Error("The synthetic conversation could not be reread before its follow-up.");
    let second;
    if (existing.body.messages.length >= 4) {
      second = { status: 200, body: { assistant_message: existing.body.messages[3] } };
    } else {
      second = await evaluate(`fetch('/api/conversations/${conversationId}/messages',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'},body:${JSON.stringify(JSON.stringify({question:"What happened with that maintenance patch?",document_ids:[state.document_id]}) )}}).then(async r=>({status:r.status,body:r.ok?await r.json():null}))`, 120_000);
    }
    if (second.status !== 200 || !second.body?.assistant_message?.response) throw new Error("The saved-conversation audio follow-up failed.");
    const secondResponse = second.body.assistant_message.response;
    const secondCitation = secondResponse.citations?.find((citation) => citation.document_id === state.document_id);
    if (secondResponse.provider !== "mock" || secondResponse.insufficient_evidence || !secondCitation ||
        !String(secondCitation.snippet ?? "").toLowerCase().includes("maintenance patch") || secondCitation.locator?.kind !== "time") {
      throw new Error(`The audio follow-up did not preserve relevant history and return a timestamped source citation (${JSON.stringify({provider:secondResponse.provider,answer:secondResponse.answer,insufficient_evidence:secondResponse.insufficient_evidence,retrieved_chunks:secondResponse.retrieved_chunks,citations:(secondResponse.citations??[]).map((citation)=>({document_id:citation.document_id,locator_kind:citation.locator?.kind,snippet:citation.snippet}))})}).`);
    }
    const mediaPreview = await evaluate(`fetch('/api/rag/documents/${state.document_id}/media',{credentials:'same-origin',headers:{Range:'bytes=0-63'}}).then(async r=>({status:r.status,type:r.headers.get('content-type')??'',range:r.headers.get('content-range')??'',bytes:(await r.arrayBuffer()).byteLength}))`);
    if (mediaPreview.status !== 206 || !mediaPreview.type.startsWith("audio/") || mediaPreview.bytes !== 64 || !/^bytes 0-63\//.test(mediaPreview.range)) {
      throw new Error("The authenticated original audio range preview failed.");
    }
    await command("Page.navigate", { url: base.href });
    await waitFor("Boolean(document.querySelector('.shell'))", "application reloaded", 30_000);
    const persisted = await evaluate(`fetch('/api/conversations/${conversationId}',{credentials:'same-origin'}).then(async r=>({status:r.status,body:r.ok?await r.json():null}))`);
    if (persisted.status !== 200 || persisted.body?.messages?.length !== 4 ||
        !persisted.body.messages.some((message) => message.role === "assistant" && message.response?.citations?.some((citation) => citation.document_id === state.document_id))) {
      throw new Error("The audio conversation and citations did not survive an application refresh.");
    }
    let vectorStatus;
    let documentIndex;
    const vectorDeadline = Date.now() + 30_000;
    while (Date.now() < vectorDeadline) {
      vectorStatus = await evaluate(`fetch('/api/rag/vector-store',{credentials:'same-origin'}).then(async r=>({status:r.status,body:r.ok?await r.json():null}))`);
      documentIndex = vectorStatus.body?.indexes?.find((index) => index.name === "documents");
      if (vectorStatus.status === 200 && vectorStatus.body?.backend === "faiss" && vectorStatus.body?.status === "healthy" &&
          documentIndex?.status === "healthy" && documentIndex.vectors === documentIndex.expected_vectors &&
          documentIndex.vectors >= ready.chunk_count) break;
      await delay(250);
    }
    if (vectorStatus.status !== 200 || vectorStatus.body?.backend !== "faiss" || vectorStatus.body?.status !== "healthy" ||
        documentIndex?.status !== "healthy" || documentIndex.vectors !== documentIndex.expected_vectors ||
        documentIndex.vectors < ready.chunk_count) {
      throw new Error(`The current-source API restart did not expose a healthy persisted FAISS index matching PostgreSQL (${JSON.stringify({http:vectorStatus.status,backend:vectorStatus.body?.backend,status:vectorStatus.body?.status,indexes:vectorStatus.body?.indexes?.map((index)=>({name:index.name,status:index.status,vectors:index.vectors,expected_vectors:index.expected_vectors}))})}).`);
    }
    state.final_state = { status: ready.status, attempts: ready.attempts, chunk_count: ready.chunk_count, searchable: ready.searchable };
    state.media_status = { status: media.body.status, enabled: media.body.enabled, runtime_available: media.body.runtime_available, model_available: media.body.model_available };
    state.answer = { status: first.status, provider: firstResponse.provider, cited: true, synthetic_ticket_text: firstSnippet.match(/ticket number[^.]+/i)?.[0] ?? null, normalized_marker_digits: "4821", location: firstCitation.location };
    state.follow_up = { status: second.status, provider: secondResponse.provider, cited: true, maintenance_patch_present: true, location: secondCitation.location };
    state.original_preview = mediaPreview;
    state.conversation_persisted_after_refresh = true;
    state.vector_store_after_api_restart = { backend: vectorStatus.body.backend, status: vectorStatus.body.status, document_vectors: documentIndex.vectors, database_expected_vectors: documentIndex.expected_vectors };
    const shot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    writeFileSync(path.join(path.dirname(statePath), "c06-audio-ready.png"), Buffer.from(shot.data, "base64"));
    writeFileSync(statePath, `${JSON.stringify(state, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
    console.log(JSON.stringify({ mode, final_state: state.final_state, answer: state.answer, follow_up: state.follow_up, original_preview: mediaPreview, persisted_after_refresh: true }));
  }
} finally {
  if (socket) socket.close();
  if (chrome && chrome.exitCode === null) {
    chrome.kill();
    await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3_000)]);
    if (chrome.exitCode === null) chrome.kill("SIGKILL");
  }
  rmSync(profile, { recursive: true, force: true });
}
