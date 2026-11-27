import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const mode = process.argv[2];
const base = new URL(process.argv[3] ?? "http://127.0.0.1:5182/admin/");
const fixturePath = path.resolve(process.env.INFRASPINE_C11_REVIEW_FIXTURE ?? "");
const reviewRoot = path.resolve(process.env.INFRASPINE_C11_REVIEW_ROOT ?? "");
const pdfPath = path.resolve(process.env.C05_SYNTHETIC_PDF ?? "");
const statePath = path.join(reviewRoot, "evidence", "c05-core-ingestion-acceptance.json");
if (!["upload", "retry", "verify"].includes(mode) || base.protocol !== "http:" || base.hostname !== "127.0.0.1" || !fixturePath || !reviewRoot || !pdfPath) {
  throw new Error("C05 acceptance requires a loopback review URL and protected synthetic paths.");
}
const relativePdf = path.relative(reviewRoot, pdfPath);
if (relativePdf.startsWith("..") || path.isAbsolute(relativePdf) || !existsSync(pdfPath)) {
  throw new Error("The synthetic PDF must exist inside the protected C11 review folder.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
if (!fixture.admin?.email || !fixture.admin?.password) throw new Error("The protected synthetic Admin fixture is incomplete.");
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for C05 acceptance.");

const tmpRoot = path.join(reviewRoot, "tmp");
mkdirSync(tmpRoot, { recursive: true });
mkdirSync(path.dirname(statePath), { recursive: true });
const profile = mkdtempSync(path.join(tmpRoot, "browser-c05-ingestion-"));
let chrome;
let socket;
let sequence = 0;
const pending = new Map();
const requestMethods = new Map();
const responseStatuses = {};

function command(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out at ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 20_000) {
  const result = await command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("A C05 browser action failed.");
  return result.result?.value;
}
async function waitFor(expression, label, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(100);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function waitForDocument(documentId, predicate, label, timeoutMs = 30_000) {
  const expression = `fetch('/api/rag/documents',{credentials:'include'}).then(r=>r.json()).then(j=>{const d=j.documents?.find(x=>x.id===${JSON.stringify(documentId)});return d&&(${predicate})(d)?d:null})`;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const state = await evaluate(expression);
    if (state) return state;
    await delay(250);
  }
  throw new Error(`Timed out waiting for document ${label}.`);
}
async function navigateToKnowledge() {
  await command("Page.navigate", { url: base.href });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]')) || Boolean(document.querySelector('.shell'))", "login or authenticated shell", 30_000);
  if (await evaluate("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))")) {
    await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const pass=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!pass||!form)return false;for(const [el,value] of [[email,${JSON.stringify(fixture.admin.email)}],[pass,${JSON.stringify(fixture.admin.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
    await waitFor("Boolean(document.querySelector('.shell'))", "authenticated Admin", 45_000);
  }
  const knowledge = await evaluate(`(()=>{const button=[...document.querySelectorAll('button')].find(el=>el.textContent.trim()==='Knowledge');if(!button)return false;button.click();return true})()`);
  if (!knowledge) throw new Error("The Knowledge navigation button was not available.");
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
      const request = message.params.request;
      requestMethods.set(message.params.requestId, { url: request.url, method: request.method });
    }
    if (message.method === "Network.responseReceived") {
      const request = requestMethods.get(message.params.requestId);
      if (request?.url.includes("/api/rag/documents") && request.method === "POST") responseStatuses.upload = message.params.response.status;
      if (request?.url.includes("/reprocess") && request.method === "POST") responseStatuses.reprocess = message.params.response.status;
    }
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error(`DevTools command failed: ${message.error.message}`)) : item.resolve(message.result);
  });
  await command("Page.enable");
  await command("Runtime.enable");
  await command("Network.enable");
  await command("DOM.enable");
  await navigateToKnowledge();

  if (mode === "upload") {
    const pdfBase64 = readFileSync(pdfPath).toString("base64");
    const selectedCount = await evaluate(`(()=>{const input=document.querySelector('input[type="file"][multiple]');if(!input)return 0;const raw=atob(${JSON.stringify(pdfBase64)});const bytes=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)bytes[i]=raw.charCodeAt(i);const transfer=new DataTransfer();transfer.items.add(new File([bytes],'c05-synthetic-reprocess.pdf',{type:'application/pdf'}));input.files=transfer.files;const count=input.files.length;input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));return count})()`);
    if (selectedCount !== 1) throw new Error("The synthetic PDF was not selected by the Knowledge upload control.");
    const uploadStatusDeadline = Date.now() + 20_000;
    while (responseStatuses.upload === undefined && Date.now() < uploadStatusDeadline) await delay(100);
    if (responseStatuses.upload !== 202) throw new Error("The core document upload did not return HTTP 202.");
    await waitFor("Boolean(document.body.innerText.includes('c05-synthetic-reprocess.pdf'))", "synthetic upload row");
    const metadata = await evaluate(`fetch('/api/rag/documents',{credentials:'include'}).then(r=>r.json()).then(j=>j.documents?.find(x=>x.filename==='c05-synthetic-reprocess.pdf'))`);
    if (!metadata?.id || metadata.status !== "pending") throw new Error("The synthetic PDF was not queued as pending.");
    const record = {
      review_url: base.href,
      document_id: metadata.id,
      filename: metadata.filename,
      upload_http: responseStatuses.upload,
      queued_status: metadata.status,
      version: metadata.version,
      submitted_sha256: createHash("sha256").update(readFileSync(pdfPath)).digest("hex"),
      worker_attempts: [],
    };
    writeFileSync(statePath, `${JSON.stringify(record, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
    console.log(JSON.stringify({ mode, upload_http: record.upload_http, queued_status: record.queued_status, document_id: record.document_id }));
  } else {
    const record = JSON.parse(readFileSync(statePath, "utf8").replace(/^\uFEFF/, ""));
    if (!record.document_id || record.filename !== "c05-synthetic-reprocess.pdf") throw new Error("The synthetic C05 document state is invalid.");
    if (mode === "retry") {
      const failed = await waitForDocument(record.document_id, "d=>d.status==='failed'&&d.error_code==='embedding_model_unavailable'", "safe missing-model failure");
      if (!await evaluate("Boolean(document.querySelector('button[aria-label=\"Retry\"]'))")) throw new Error("The UI did not show the safe retry action.");
      await evaluate(`document.querySelector('button[aria-label="Retry"]').click()`);
      const retryDeadline = Date.now() + 20_000;
      while (responseStatuses.reprocess === undefined && Date.now() < retryDeadline) await delay(100);
      if (responseStatuses.reprocess !== 202) throw new Error("The UI retry action did not return HTTP 202.");
      const pending = await waitForDocument(record.document_id, "d=>d.status==='pending'", "requeued document");
      record.first_failure = { status: failed.status, error_code: failed.error_code, attempts: failed.attempts };
      record.retry_http = responseStatuses.reprocess;
      record.requeued_status = pending.status;
      writeFileSync(statePath, `${JSON.stringify(record, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
      console.log(JSON.stringify({ mode, first_failure: record.first_failure, retry_http: record.retry_http, requeued_status: record.requeued_status }));
    } else {
      const ready = await waitForDocument(record.document_id, "d=>d.status==='ready'&&d.chunk_count>0", "ready searchable document", 60_000);
      const preview = await evaluate(`fetch('/api/rag/documents/${record.document_id}/pages/1/image',{credentials:'include'}).then(async r=>({status:r.status,type:r.headers.get('content-type'),bytes:(await r.arrayBuffer()).byteLength}))`);
      if (preview.status !== 200 || !preview.type?.startsWith("image/png") || preview.bytes < 1000) throw new Error("The authorized original PDF page preview did not render.");
      if (ready.version !== record.version || ready.attempts !== 1 || !ready.searchable) throw new Error("The reprocessed source did not become searchable at the expected version.");
      record.final_state = { status: ready.status, attempts: ready.attempts, version: ready.version, page_count: ready.page_count, chunk_count: ready.chunk_count, searchable: ready.searchable };
      record.preview = preview;
      const shot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
      writeFileSync(path.join(path.dirname(statePath), "c05-core-ingestion-ready.png"), Buffer.from(shot.data, "base64"));
      writeFileSync(statePath, `${JSON.stringify(record, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
      console.log(JSON.stringify({ mode, final_state: record.final_state, preview: record.preview }));
    }
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
