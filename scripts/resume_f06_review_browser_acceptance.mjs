#!/usr/bin/env node
/** Resume saved current-source review evidence, then verify F06 roles without repeating setup or ingestion. */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const appUrl = process.env.INFRASPINE_LOCAL_REVIEW_URL;
const reviewRoot = process.env.INFRASPINE_LOCAL_REVIEW_ROOT;
const fixturePath = process.env.INFRASPINE_LOCAL_REVIEW_FIXTURE;
const conversationId = process.env.INFRASPINE_LOCAL_REVIEW_CONVERSATION;
if (!appUrl || !reviewRoot || !fixturePath || !conversationId) throw new Error("The saved-review continuation fixture is incomplete.");
const host = new URL(appUrl);
if (host.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(host.hostname)) throw new Error("Only a loopback review URL is permitted.");
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const state = JSON.parse(readFileSync(path.join(reviewRoot, "stack-state.json"), "utf8").replace(/^\uFEFF/, ""));
const manager = fixture.managers?.at(-1);
const employee = fixture.employees?.at(-1);
const admin = fixture.admin;
if (!admin?.email || !admin?.password || !manager?.email || !manager?.password || !employee?.email || !employee?.password) {
  throw new Error("The protected current-source Admin, Manager and Employee fixtures are incomplete.");
}
const browserPath = process.env.LOCALAPPDATA && existsSync(path.join(process.env.LOCALAPPDATA, "Google\Chrome\Application\chrome.exe"))
  ? path.join(process.env.LOCALAPPDATA, "Google\Chrome\Application\chrome.exe")
  : "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
if (!existsSync(browserPath)) throw new Error("Chrome is unavailable for the isolated review continuation.");

const stamp = new Date().toISOString().replace(/[-:.]/g, "").replace("T", "-").replace("Z", "");
const profile = path.join(reviewRoot, `f06-resume-profile-${stamp}`);
const downloadRoot = path.join(profile, "downloads");
const evidenceRoot = path.join(reviewRoot, "f06-role-browser-evidence");
mkdirSync(downloadRoot, { recursive: true });
mkdirSync(evidenceRoot, { recursive: true });

let browser;
let socket;
let sequence = 0;
const pending = new Map();

function call(method, params = {}, timeoutMs = 25_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out during review continuation step ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error("A review continuation browser action raised an exception.");
  return result.result?.value;
}

async function waitFor(expression, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await delay(150);
  }
  return false;
}

async function fill(selector, value) {
  const ok = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!ok) throw new Error(`Review continuation sign-in control is missing: ${selector}`);
}

async function callApi(method, route) {
  return evaluate(`(async()=>{const r=await fetch(${JSON.stringify(route)},{method:${JSON.stringify(method)},credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineAdmin'}});let data=null;let text='';try{const type=r.headers.get('content-type')||'';if(type.includes('json'))data=await r.json();else text=await r.text()}catch{}return {status:r.status,type:r.headers.get('content-type')||'',data,text}})()`);
}

async function signIn(user, expectedRole) {
  await call("Page.navigate", { url: appUrl });
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`)) throw new Error("Current-source sign-in form did not load.");
  await fill('form.login-form input[name="email"]', user.email);
  await fill('form.login-form input[name="password"]', user.password);
  await evaluate(`document.querySelector('form.login-form')?.requestSubmit()`);
  if (!await waitFor(`!!document.querySelector('.user-chip-role')`)) throw new Error(`Synthetic ${expectedRole} sign-in failed.`);
  const identity = await callApi("GET", "/api/auth/me");
  if (identity.status !== 200 || identity.data?.role !== expectedRole) throw new Error(`The saved account is not ${expectedRole}.`);
}

async function signOut() {
  await evaluate(`document.querySelector('button[aria-label="Sign out"]')?.click()`);
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`)) throw new Error("Review sign-out did not return to sign-in.");
}

async function clickText(selector, text) {
  const ok = await evaluate(`(()=>{const e=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(text)}&&!x.disabled);if(!e)return false;e.click();return true})()`);
  if (!ok) throw new Error(`Expected enabled review control is missing: ${text}`);
}

async function screenshot(name) {
  const result = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const file = path.join(evidenceRoot, `${name}-${stamp}.png`);
  writeFileSync(file, Buffer.from(result.data, "base64"), { flag: "wx" });
  return file;
}

async function connect() {
  const cleanEnv = { ...process.env };
  for (const key of ["INFRASPINE_LOCAL_REVIEW_FIXTURE", "INFRASPINE_LOCAL_REVIEW_ADMIN_EMAIL", "INFRASPINE_LOCAL_REVIEW_ADMIN_PASSWORD",
    "INFRASPINE_LOCAL_REVIEW_MANAGER_EMAIL", "INFRASPINE_LOCAL_REVIEW_MANAGER_PASSWORD", "INFRASPINE_LOCAL_REVIEW_EMPLOYEE_EMAIL",
    "INFRASPINE_LOCAL_REVIEW_EMPLOYEE_PASSWORD"]) delete cleanEnv[key];
  browser = spawn(browserPath, ["--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
    "--disable-background-networking", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"],
  { stdio: "ignore", windowsHide: true, env: cleanEnv });
  const activePort = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activePort) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePort)) throw new Error("The isolated browser did not start.");
  const port = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline && browser.exitCode === null) {
    try { const r = await fetch(`http://127.0.0.1:${port}/json/list`); if (r.ok) page = (await r.json()).find((x) => x.type === "page" && x.webSocketDebuggerUrl); } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("The isolated browser page did not become ready.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", () => reject(new Error("Browser connection failed.")), { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const item = pending.get(message.id); clearTimeout(item.timer); pending.delete(message.id);
    if (message.error) item.reject(new Error(`Review browser command failed: ${item.method}`)); else item.resolve(message.result);
  });
  await call("Page.enable"); await call("Runtime.enable"); await call("DOM.enable"); await call("Network.enable");
  await call("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloadRoot, eventsEnabled: true });
  await call("Emulation.setDeviceMetricsOverride", { width: 1365, height: 900, deviceScaleFactor: 1, mobile: false });
}

async function run() {
  await connect();
  await signIn(admin, "admin");
  await call("Page.navigate", { url: `${appUrl}#/c/${encodeURIComponent(conversationId)}` });
  const citedRendered = await waitFor(`!!document.querySelector('.sources .source')`, 25_000);
  const conversation = await callApi("GET", `/api/conversations/${conversationId}`);
  const savedAssistant = conversation.data?.messages?.findLast((message) => message.role === "assistant");
  if (conversation.status !== 200) throw new Error("The saved citation conversation was unavailable through the current API.");
  if (!citedRendered) {
    const dom = await evaluate(`(()=>({hash:location.hash,role:document.querySelector('.user-chip-role')?.textContent.trim()??'',headings:[...document.querySelectorAll('h1,h2')].slice(0,5).map(x=>x.textContent.trim()),bubbles:document.querySelectorAll('.bubble-ai').length,sources:document.querySelectorAll('.sources .source').length,chatBody:(document.querySelector('.chat-scroll')?.innerText??'').slice(0,300)}))()`);
    await screenshot("saved-answer-render-diagnostic");
    const response = savedAssistant?.response;
    throw new Error(`Saved response state: ${JSON.stringify({ role: dom.role, hash: dom.hash, bubbles: dom.bubbles, visibleSources: dom.sources, savedMessage: !!savedAssistant, savedCitations: response?.citations?.length ?? 0, savedInsufficient: response?.insufficient_evidence ?? null, domHeadings: dom.headings })}`);
  }
  const savedAnswer = await evaluate(`(()=>({answer:document.querySelector('.bubble-ai')?.innerText??'',source:document.querySelector('.sources .source')?.innerText??'',citation:!!document.querySelector('.bubble-ai .cite-chip')}))()`);
  if (!savedAnswer.citation || !savedAnswer.answer.includes("48 hours") || !savedAnswer.source.includes("review-evidence-")) {
    throw new Error("Saved current-source conversation did not render its synthetic source citation.");
  }
  const answerShot = await screenshot("cited-answer");
  const opened = await evaluate(`(()=>{const b=document.querySelector('.sources .source-head');if(!b)return false;b.click();return true})()`);
  if (!opened || !await waitFor(`!!document.querySelector('.source-page-link')`)) throw new Error("The saved citation did not open its original-page preview.");
  const previewUrl = await evaluate(`document.querySelector('.source-page-link')?.getAttribute('href')??''`);
  const preview = await evaluate(`(async()=>{const r=await fetch(${JSON.stringify(previewUrl)});const b=await r.blob();return {status:r.status,type:r.headers.get('content-type')||'',size:b.size}})()`);
  if (preview.status !== 200 || !preview.type.startsWith("image/") || preview.size < 100) throw new Error("Original source preview did not return a page image.");
  await call("Page.navigate", { url: new URL(previewUrl, appUrl).href });
  if (!await waitFor(`document.contentType.startsWith('image/') && !!document.images[0]?.naturalWidth`, 30_000)) throw new Error("Original source preview did not render in the browser.");
  const previewShot = await screenshot("original-source-preview");
  console.log("LOCAL_REVIEW_CITED_ANSWER_AND_ORIGINAL_PREVIEW=PASS");

  await call("Page.navigate", { url: appUrl });
  if (!await waitFor(`!!document.querySelector('.user-chip-role')`)) throw new Error("Admin session did not return after preview.");
  await signOut();
  await signIn(employee, "employee");
  const employeeAdmin = await callApi("GET", "/api/admin/users");
  const employeeManager = await callApi("GET", "/api/departments/managed");
  const employeeCsv = await callApi("GET", "/api/rag/financial-extractions/bookkeeping.csv");
  const employeeControl = await evaluate(`[...document.querySelectorAll('button')].some(x=>x.textContent.trim()==='Export reviewed bookkeeping CSV')`);
  if (employeeAdmin.status !== 403 || employeeManager.status !== 403 || employeeCsv.status !== 403 || employeeControl) throw new Error("Employee accessed an Admin, Manager, or bookkeeping export control.");
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  const viewport = await evaluate(`({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})`);
  if (viewport.width !== 390 || viewport.scrollWidth > 390) throw new Error("Employee Knowledge view overflows at 390px.");
  const employeeShot = await screenshot("employee-denied-390px");
  console.log("LOCAL_F06_CURRENT_SOURCE_EMPLOYEE_HIDDEN_403_AND_390PX=PASS");

  await call("Emulation.setDeviceMetricsOverride", { width: 1365, height: 900, deviceScaleFactor: 1, mobile: false });
  await signOut();
  await signIn(manager, "manager");
  const knowledgeNav = await evaluate(`!![...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.trim()==='Knowledge')`);
  if (!knowledgeNav) throw new Error("Manager could not reach Knowledge.");
  await clickText("button.nav-item", "Knowledge");
  if (!await waitFor(`document.querySelector('main h1,h1')?.textContent.trim()==='Knowledge'`)) throw new Error("Manager Knowledge page did not load.");
  const control = await evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Export reviewed bookkeeping CSV');return !!b&&!b.disabled})()`);
  if (!control) throw new Error("Manager did not receive the reviewed CSV control.");
  const managerShot = await screenshot("manager-export-control");
  await clickText("button", "Export reviewed bookkeeping CSV");
  const deadline = Date.now() + 20_000;
  let filename;
  while (Date.now() < deadline) { filename = readdirSync(downloadRoot).find((name) => name === "infraspine-bookkeeping-reviewed.csv"); if (filename) break; await delay(150); }
  if (!filename) throw new Error("Manager export did not download the CSV.");
  const csv = readFileSync(path.join(downloadRoot, filename), "utf8");
  const directExport = await callApi("GET", "/api/rag/financial-extractions/bookkeeping.csv");
  if (!csv.includes("INV-204-CHECKED") || directExport.status !== 200 || !directExport.type.includes("text/csv")) throw new Error("Manager CSV did not contain the reviewed invoice or return 200.");
  const invoiceRows = csv.trim().split(/\r?\n/).length - 1;
  if (invoiceRows < 1) throw new Error("Manager CSV did not contain a reviewed record row.");
  console.log("LOCAL_F06_CURRENT_SOURCE_MANAGER_EXPORT_DOWNLOAD_AND_200=PASS");

  const evidence = {
    reviewUrl: appUrl, gitHead: state.gitHead, database: state.database, migration: state.migration,
    conversationId, managerCsvSha256: createHash("sha256").update(csv).digest("hex"), managerCsvDataRows: invoiceRows,
    managerExportStatus: directExport.status, employeeExportStatus: employeeCsv.status,
    employeeAdminStatus: employeeAdmin.status, employeeManagerStatus: employeeManager.status,
    citedAnswerScreenshot: answerShot, originalPreviewScreenshot: previewShot,
    employeeScreenshot390px: employeeShot, managerExportScreenshot: managerShot, acceptedAtUtc: new Date().toISOString(),
  };
  const evidencePath = path.join(evidenceRoot, `f06-current-source-browser-resume-${stamp}.json`);
  writeFileSync(evidencePath, `${JSON.stringify(evidence, null, 2)}\n`, { flag: "wx" });
  console.log("LOCAL_F06_CURRENT_SOURCE_EVIDENCE=" + evidencePath);
}

try { await run(); }
catch (error) { console.error(`LOCAL_REVIEW_CONTINUATION=FAIL ${error?.message ?? "unknown failure"}`); process.exitCode = 1; }
finally { try { socket?.close(); } catch {} try { browser?.kill(); } catch {} }
