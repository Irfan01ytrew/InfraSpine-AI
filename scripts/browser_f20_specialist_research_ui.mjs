#!/usr/bin/env node
/** Current-source authenticated specialist-research UI smoke on a synthetic no-source fixture. */
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

const fixturePath = process.env.INFRASPINE_C04_REVIEW_FIXTURE;
const evidencePath = process.env.INFRASPINE_F20_RESEARCH_EVIDENCE;
const base = process.env.INFRASPINE_F20_RESEARCH_URL ?? "http://127.0.0.1:5183/admin/";
const url = new URL(base);
if (!fixturePath || !evidencePath || !existsSync(fixturePath)) throw new Error("Protected synthetic review fixture is unavailable.");
if (url.hostname !== "127.0.0.1" || !/^https?:$/.test(url.protocol)) throw new Error("F20 research acceptance is limited to loopback HTTP.");
const { admin } = JSON.parse(readFileSync(fixturePath, "utf8"));
const chromePath = process.env.INFRASPINE_BROWSER_PATH ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const reviewRoot = path.join(repoRoot, ".verification-local-review-20261027-native", "c04-catalog-transfer-current-20261027");
const dataRoot = path.join(reviewRoot, "data");
const pythonPath = path.join(repoRoot, ".venv-verify-312", "Scripts", "python.exe");
if (!admin?.email || !admin?.password || !existsSync(chromePath)) throw new Error("Protected synthetic Admin fixture or local Chrome is unavailable.");

let chrome;
let socket;
let profile;
let sequence = 0;
let researchResponse = null;
let responseId = null;
const pending = new Map();
const cdp = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++sequence;
  const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Chrome command timed out: ${method}`)); }, 30_000);
  pending.set(id, { resolve, reject, timer });
  socket.send(JSON.stringify({ id, method, params }));
});
async function evaluate(expression) {
  const result = await cdp("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error("Research browser action failed.");
  return result.result?.value;
}
async function waitFor(expression, label, timeout = 35_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(120);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function fill(selector, value) {
  const ok = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!ok) throw new Error("Research page form control is missing.");
}
async function attachSyntheticText(selector, filename, text) {
  const input = await cdp("DOM.getDocument", { depth: -1 });
  const query = await cdp("DOM.querySelector", { nodeId: input.root.nodeId, selector });
  if (!query.nodeId) throw new Error("Synthetic research upload control is unavailable.");
  const base64 = Buffer.from(text, "utf8").toString("base64");
  const result = await evaluate(`(()=>{const i=document.querySelector(${JSON.stringify(selector)});if(!i)return false;const raw=atob(${JSON.stringify(base64)}),bytes=new Uint8Array(raw.length);for(let n=0;n<raw.length;n++)bytes[n]=raw.charCodeAt(n);const data=new DataTransfer();data.items.add(new File([bytes],${JSON.stringify(filename)},{type:'text/plain'}));i.files=data.files;i.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!result) throw new Error("Synthetic research document could not be attached.");
}
async function runSingleWorkerPass() {
  if (!existsSync(pythonPath) || !existsSync(dataRoot)) throw new Error("Isolated F20 worker runtime or data directory is unavailable.");
  const env = {
    ...process.env,
    DATABASE_URL: "postgresql+asyncpg://postgres@127.0.0.1:55888/infraspine_c04catalog_20261027_test",
    APP_ENV: "testing",
    LLM_MOCK_MODE: "true",
    MASTER_ENCRYPTION_KEY: randomBytes(32).toString("base64"),
    MASTER_ENCRYPTION_KEY_ID: "f20-research-worker",
    SESSION_COOKIE_SECURE: "false",
    RAG_DATA_DIR: dataRoot,
    RAG_MODELS_DIR: path.join(repoRoot, "data", "models"),
    RAG_EMBEDDING_ALLOW_DOWNLOAD: "false",
    RAG_VECTOR_STORE: "faiss",
    RAG_INGESTION_MODE: "worker",
    RAG_WORKER_ENABLED: "false",
    RAG_PARSE_ISOLATION: "true",
    HF_HUB_OFFLINE: "1",
    TRANSFORMERS_OFFLINE: "1",
    LOG_DIR: path.join(reviewRoot, "logs", "f20-research-worker"),
  };
  await new Promise((resolve, reject) => {
    const child = spawn(pythonPath, ["-m", "app.worker", "--once"], { cwd: repoRoot, env, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    let output = "";
    let settled = false;
    const timeout = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill();
      reject(new Error("Isolated F20 worker pass exceeded its 180-second bound."));
    }, 180_000);
    child.stdout.on("data", (chunk) => { output = (output + chunk.toString()).slice(-4000); });
    child.stderr.on("data", (chunk) => { output = (output + chunk.toString()).slice(-4000); });
    child.once("error", () => {
      if (settled) return;
      settled = true; clearTimeout(timeout); reject(new Error("Isolated F20 worker could not start."));
    });
    child.once("close", (code) => {
      if (settled) return;
      settled = true; clearTimeout(timeout);
      if (code !== 0) reject(new Error(`Isolated F20 worker exited with code ${code}; diagnostics are retained only in the protected session.`));
      else resolve(output);
    });
  });
}

try {
  profile = mkdtempSync(path.join(tmpdir(), "infraspine-f20-research-ui-"));
  chrome = spawn(chromePath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const activeFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activeFile) && Date.now() < deadline) await delay(100);
  if (!existsSync(activeFile)) throw new Error("Isolated Chrome did not start.");
  const port = readFileSync(activeFile, "utf8").split(/\r?\n/)[0];
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method === "Network.responseReceived" && message.params.response.url.includes("/api/rag/research")) {
      responseId = message.params.requestId;
      researchResponse = { status: message.params.response.status, url: new URL(message.params.response.url).pathname };
    }
    if (message.method === "Network.loadingFinished" && message.params.requestId === responseId) {
      void cdp("Network.getResponseBody", { requestId: responseId }).then((body) => {
        try { researchResponse.body = JSON.parse(body.body); } catch { researchResponse.body = null; }
      });
    }
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error("Chrome DevTools command failed.")) : item.resolve(message.result);
  });
  await cdp("Page.enable"); await cdp("Runtime.enable"); await cdp("Network.enable");
  await cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await cdp("Page.navigate", { url: base });
  await waitFor(`Boolean(document.querySelector('form.login-form input[name="email"]'))`, "Admin sign-in");
  await fill('form.login-form input[name="email"]', admin.email);
  await fill('form.login-form input[name="password"]', admin.password);
  await evaluate(`document.querySelector('form.login-form')?.requestSubmit()`);
  await waitFor(`Boolean(document.querySelector('.shell'))`, "authenticated research workspace");
  const knowledgeNav = await evaluate(`(()=>{const button=[...document.querySelectorAll('button')].find(item=>item.innerText.trim()==='Knowledge');if(!button)return false;button.click();return true})()`);
  if (!knowledgeNav) throw new Error("Knowledge navigation is unavailable.");
  await waitFor(`Boolean(document.querySelector('input[type="file"][accept*=".txt"]'))`, "synthetic source upload control");
  const token = randomBytes(4).toString("hex").toUpperCase();
  const filename = `f20-specialist-source-${token}.txt`;
  const sourceSentence = `Synthetic F20 procedure ${token}: the service desk must acknowledge an internal review ticket within 4 business hours. The escalation owner is Operations.`;
  const filePath = path.join(reviewRoot, "evidence", filename);
  writeFileSync(filePath, `${sourceSentence}\n`, { flag: "wx" });
  await attachSyntheticText('input[type="file"][accept*=".txt"]', filename, `${sourceSentence}\n`);
  await waitFor(`Boolean(document.querySelector('.toast-success')?.innerText.includes(${JSON.stringify(filename)}))`, "synthetic source upload receipt");
  await waitFor(`(async()=>{const r=await fetch('/api/rag/documents',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;const body=await r.json(),doc=body.documents.find(item=>item.filename===${JSON.stringify(filename)});if(doc)window.__f20ResearchDoc=doc;return Boolean(doc&&['pending','processing'].includes(doc.status))})()`, "queued synthetic document", 15_000);
  await runSingleWorkerPass();
  await waitFor(`(async()=>{const r=await fetch('/api/rag/documents',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;const body=await r.json(),doc=body.documents.find(item=>item.filename===${JSON.stringify(filename)});if(doc)window.__f20ResearchDoc=doc;return Boolean(doc&&doc.status==='ready'&&doc.searchable&&doc.chunk_count>0)})()`, "current-source worker ingestion and API index sync", 120_000);
  const ingested = await evaluate("window.__f20ResearchDoc");
  if (!ingested?.searchable || ingested.chunk_count < 1) throw new Error("Synthetic F20 source did not become searchable.");
  console.log(`F20_RESEARCH_SYNTHETIC_SOURCE_WORKER=PASS (${ingested.chunk_count} searchable chunk)`);
  const researchAgain = await evaluate(`(()=>{const button=[...document.querySelectorAll('button')].find(item=>item.innerText.trim()==='Company data research');if(!button)return false;button.click();return true})()`);
  if (!researchAgain) throw new Error("Company data research navigation is unavailable.");
  await waitFor(`document.querySelector('h1')?.innerText==='Company data research'`, "specialist research page");
  researchResponse = null; responseId = null;
  const question = `According to synthetic F20 procedure ${token}, how soon must the service desk acknowledge the review ticket?`;
  await fill(".research-form textarea", question);
  await evaluate(`(()=>{const button=[...document.querySelectorAll('.research-form button')].find(item=>item.innerText.includes('Run research'));if(!button||button.disabled)return false;button.click();return true})()`);
  await waitFor(`Boolean(document.querySelector('.research-results'))`, "document-grounded specialist report", 60_000);
  const researchDeadline = Date.now() + 10_000;
  while (!researchResponse?.body?.findings?.length && Date.now() < researchDeadline) await delay(50);
  if (!researchResponse?.body?.findings?.length) throw new Error("Current-source research API response body was not captured.");
  const result = await evaluate(`(()=>{const finding=document.querySelector('.research-finding');return{title:document.querySelector('.research-results h2')?.innerText,specialist:[...(finding?.querySelectorAll('.chip')??[])].map(item=>item.innerText.trim())[0]??null,evidenceState:[...(finding?.querySelectorAll('.pill')??[])].map(item=>item.innerText.trim())[0]??null,answer:document.querySelector('.research-answer')?.innerText??'',sourceNames:[...document.querySelectorAll('.research-source strong')].map(item=>item.innerText.trim()),sourceSnippets:[...document.querySelectorAll('.research-source small')].map(item=>item.innerText.trim()),viewport:visualViewport?.width,bodyWidth:document.body.scrollWidth,documentWidth:document.documentElement.scrollWidth}})()`);
  const apiFinding = researchResponse.body.findings[0];
  const citedSource = apiFinding.citations.find((citation) => citation.filename === filename);
  if (researchResponse.status !== 200 || result.specialist !== "Evidence finder" || result.evidenceState !== "Supported" || apiFinding.insufficient_evidence || !citedSource || !result.sourceNames.includes(filename) || !result.answer.includes("4 business hours") || !result.answer.includes(citedSource.source_id)) {
    throw new Error(`Document-backed research assertion mismatch: ${JSON.stringify({status:researchResponse.status,specialist:apiFinding.specialist,insufficient:apiFinding.insufficient_evidence,citations:apiFinding.citations.map((item)=>item.filename),uiSourceCount:result.sourceNames.length,answerContainsFact:result.answer.includes("4 business hours")})}`);
  }
  if (result.viewport !== 390 || result.bodyWidth > 390) throw new Error("Research page did not fit the 390px mobile visual viewport.");
  await delay(150);
  const image = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  writeFileSync(evidencePath, Buffer.from(image.data, "base64"), { flag: "wx" });
  console.log(`F20_SPECIALIST_RESEARCH_CURRENT_SOURCE_API_BROWSER=PASS HTTP ${researchResponse.status}; ${apiFinding.specialist}; cited source ${filename}; grounded answer; mobile ${JSON.stringify({viewport:result.viewport,bodyWidth:result.bodyWidth,documentWidth:result.documentWidth})}`);
  console.log(`F20_RESEARCH_EVIDENCE=${evidencePath}`);
} catch (error) {
  console.error(`F20_SPECIALIST_RESEARCH_UI=FAIL ${error?.message ?? "unknown"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (chrome && chrome.exitCode === null) { chrome.kill(); await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3000)]); }
  if (profile && path.basename(profile).startsWith("infraspine-f20-research-ui-")) rmSync(profile, { recursive: true, force: true });
  admin.password = "";
}
