#!/usr/bin/env node
/** Current-source Manager/Employee browser acceptance for reviewed finance CSV authorization. */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const appUrl = process.env.INFRASPINE_F06_CURRENT_URL;
const managerEmail = process.env.INFRASPINE_F06_CURRENT_MANAGER_EMAIL;
const managerPassword = process.env.INFRASPINE_F06_CURRENT_MANAGER_PASSWORD;
const employeeEmail = process.env.INFRASPINE_F06_CURRENT_EMPLOYEE_EMAIL;
const employeePassword = process.env.INFRASPINE_F06_CURRENT_EMPLOYEE_PASSWORD;
const reviewRoot = process.env.INFRASPINE_F06_CURRENT_REVIEW_ROOT;
if (!appUrl || !managerEmail || !managerPassword || !employeeEmail || !employeePassword || !reviewRoot) {
  throw new Error("The protected F06 role-browser harness is incomplete.");
}
const origin = new URL(appUrl);
if (origin.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(origin.hostname)) {
  throw new Error("The F06 browser acceptance only permits loopback HTTP.");
}
const browserPath = process.env.INFRASPINE_BROWSER_PATH || [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((candidate) => candidate && existsSync(candidate));
if (!browserPath || !existsSync(browserPath)) throw new Error("Chrome or Edge is unavailable for isolated F06 acceptance.");

const stamp = new Date().toISOString().replace(/[-:.]/g, "").replace("T", "-").replace("Z", "");
const profile = path.join(reviewRoot, `f06-role-browser-profile-${stamp}`);
const downloadRoot = path.join(profile, "downloads");
const evidenceRoot = path.join(reviewRoot, "f06-role-browser-evidence");
mkdirSync(downloadRoot, { recursive: true });
mkdirSync(evidenceRoot, { recursive: true });

let browser;
let socket;
let sequence = 0;
const pending = new Map();

function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Timed out during F06 browser step ${method}.`));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error("An F06 browser action raised an exception.");
  return result.result?.value;
}

async function waitFor(expression, timeoutMs = 25_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await delay(150);
  }
  return false;
}

async function fill(selector, value) {
  const ok = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:el instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!ok) throw new Error(`Expected F06 browser control is missing: ${selector}`);
}

async function clickText(selector, text) {
  const ok = await evaluate(`(()=>{const el=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(text)}&&!x.disabled);if(!el)return false;el.click();return true})()`);
  if (!ok) throw new Error(`Expected enabled F06 browser control is missing: ${text}`);
}

async function api(method, route) {
  return evaluate(`(async()=>{const r=await fetch(${JSON.stringify(route)},{method:${JSON.stringify(method)},credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineAdmin'}});let data=null;try{data=await r.json()}catch{}return {status:r.status,data}})()`);
}

async function signIn(email, password, expectedRole) {
  await call("Page.navigate", { url: appUrl });
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`, 30_000)) {
    throw new Error("The isolated F06 sign-in form did not load.");
  }
  await fill('form.login-form input[name="email"]', email);
  await fill('form.login-form input[name="password"]', password);
  await evaluate(`document.querySelector('form.login-form')?.requestSubmit()`);
  if (!await waitFor(`!!document.querySelector('.user-chip-role')`, 30_000)) {
    throw new Error(`Synthetic ${expectedRole} sign-in failed.`);
  }
  const me = await api("GET", "/api/auth/me");
  if (me.status !== 200 || me.data?.role !== expectedRole) throw new Error(`The signed-in synthetic account is not ${expectedRole}.`);
}

async function screenshot(name) {
  const result = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const file = path.join(evidenceRoot, `${name}-${stamp}.png`);
  writeFileSync(file, Buffer.from(result.data, "base64"), { flag: "wx" });
  return file;
}

async function connect() {
  const sanitizedEnv = { ...process.env };
  for (const name of [
    "INFRASPINE_F06_CURRENT_URL", "INFRASPINE_F06_CURRENT_MANAGER_EMAIL", "INFRASPINE_F06_CURRENT_MANAGER_PASSWORD",
    "INFRASPINE_F06_CURRENT_EMPLOYEE_EMAIL", "INFRASPINE_F06_CURRENT_EMPLOYEE_PASSWORD", "INFRASPINE_F06_CURRENT_REVIEW_ROOT",
  ]) delete sanitizedEnv[name];
  browser = spawn(browserPath, [
    "--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
    "--disable-background-networking", "--disable-component-update", "--disable-features=PasswordManagerOnboarding,PasswordManagerUI",
    "--disable-save-password-bubble", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
    `--user-data-dir=${profile}`, "about:blank",
  ], { stdio: "ignore", windowsHide: true, env: sanitizedEnv });
  const activePort = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activePort) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePort)) throw new Error("The isolated F06 browser did not start.");
  const port = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline && browser.exitCode === null) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      if (response.ok) page = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("The isolated F06 browser page did not become ready.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("F06 browser connection failed.")), { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const entry = pending.get(message.id);
    clearTimeout(entry.timer);
    pending.delete(message.id);
    if (message.error) entry.reject(new Error(`F06 browser command failed: ${entry.method}`));
    else entry.resolve(message.result);
  });
  await call("Page.enable");
  await call("Runtime.enable");
  await call("DOM.enable");
  await call("Network.enable");
  await call("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloadRoot, eventsEnabled: true });
  await call("Emulation.setDeviceMetricsOverride", { width: 1365, height: 900, deviceScaleFactor: 1, mobile: false });
}

async function openKnowledge() {
  if (!await waitFor(`!![...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.trim()==='Knowledge')`, 20_000)) {
    throw new Error("The Knowledge navigation is unavailable to the signed-in role.");
  }
  await clickText("button.nav-item", "Knowledge");
  if (!await waitFor(`document.querySelector('main h1,h1')?.textContent.trim()==='Knowledge'`, 20_000)) {
    throw new Error("The Knowledge page did not open for the signed-in role.");
  }
}

async function signOut() {
  await call("Runtime.evaluate", { expression: `document.querySelector('button[aria-label="Sign out"]')?.click()`, userGesture: true });
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`, 20_000)) {
    throw new Error("The browser did not return to the sign-in screen.");
  }
}

async function run() {
  await connect();

  await signIn(managerEmail, managerPassword, "manager");
  await openKnowledge();
  const managerExportControl = await evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Export reviewed bookkeeping CSV');return !!b&&!b.disabled})()`);
  if (!managerExportControl) throw new Error("Manager did not receive the reviewed CSV export control.");
  const managerShot = await screenshot("manager-export-control");
  console.log("LOCAL_F06_CURRENT_MANAGER_LOGIN_AND_EXPORT_CONTROL=PASS");

  await clickText("button", "Export reviewed bookkeeping CSV");
  const downloadDeadline = Date.now() + 20_000;
  let csvName;
  while (Date.now() < downloadDeadline) {
    csvName = readdirSync(downloadRoot).find((name) => name === "infraspine-bookkeeping-reviewed.csv");
    if (csvName) break;
    await delay(150);
  }
  if (!csvName) throw new Error("The Manager export control did not download the reviewed CSV.");
  const csvPath = path.join(downloadRoot, csvName);
  const csv = readFileSync(csvPath, "utf8");
  if (!csv.includes("INV-204-CHECKED")) throw new Error("The Manager CSV omitted the previously reviewed synthetic invoice.");
  const csvSha256 = createHash("sha256").update(csv).digest("hex");
  console.log("LOCAL_F06_CURRENT_MANAGER_REVIEWED_CSV_DOWNLOAD=PASS");

  await signOut();
  await signIn(employeeEmail, employeePassword, "user");
  await openKnowledge();
  const employeeHasExportControl = await evaluate(`[...document.querySelectorAll('button')].some(x=>x.textContent.trim()==='Export reviewed bookkeeping CSV')`);
  if (employeeHasExportControl) throw new Error("Employee received the restricted bookkeeping export control.");
  const employeeExport = await evaluate(`(async()=>{const r=await fetch('/api/rag/financial-extractions/bookkeeping.csv',{credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineAdmin'}});return {status:r.status,type:r.headers.get('content-type')||''}})()`);
  if (employeeExport.status !== 403) throw new Error(`Employee direct export returned HTTP ${employeeExport.status}, expected 403.`);
  console.log("LOCAL_F06_CURRENT_EMPLOYEE_CONTROL_HIDDEN_AND_API_403=PASS");

  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  const narrow = await evaluate(`({width:window.innerWidth,scroll:document.documentElement.scrollWidth,hasExport:[...document.querySelectorAll('button')].some(x=>x.textContent.trim()==='Export reviewed bookkeeping CSV')})`);
  if (narrow.hasExport || narrow.scroll > narrow.width) throw new Error("Employee 390px Knowledge view exposed export or overflowed horizontally.");
  const employeeShot = await screenshot("employee-no-export-390px");
  console.log("LOCAL_F06_CURRENT_EMPLOYEE_390PX_NO_OVERFLOW=PASS");

  const evidence = {
    database: "infraspine_f06roles_a2ae10a9_test",
    migration: "0052_user_totp_mfa",
    managerCsvSha256: csvSha256,
    managerScreenshot: managerShot,
    employeeScreenshot: employeeShot,
    managerExportStatus: 200,
    employeeExportStatus: employeeExport.status,
  };
  writeFileSync(path.join(evidenceRoot, `f06-role-browser-${stamp}.json`), `${JSON.stringify(evidence, null, 2)}\n`, { flag: "wx" });
  console.log("LOCAL_F06_CURRENT_SOURCE_BROWSER_TO_POSTGRESQL=PASS");
  console.log("LOCAL_F06_CURRENT_SOURCE_EVIDENCE=" + evidenceRoot);
}

try {
  await run();
} catch (error) {
  console.error(`LOCAL_F06_CURRENT_SOURCE_BROWSER_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  try { browser?.kill(); } catch {}
}
