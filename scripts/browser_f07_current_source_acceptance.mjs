#!/usr/bin/env node
/** Authenticated current-source F07 read-only database analysis in an isolated Chrome profile. */
import { spawn } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const repoRoot = process.cwd();
const reviewRoot = path.resolve(process.env.INFRASPINE_F07_REVIEW_ROOT ?? ".verification-local-review-20261027-native/f07-current-source-r3");
const fixturePath = path.resolve(process.env.INFRASPINE_F07_FIXTURE ?? ".verification-local-review-20261027-native/f07-synthetic-role-users.json");
const state = JSON.parse(readFileSync(path.join(reviewRoot, "stack-state.json"), "utf8").replace(/^\uFEFF/, ""));
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const appUrl = state.reviewUrl;
const admin = fixture.admin;
const manager = fixture.managers?.[0];
const employee = fixture.employees?.[0];
if (!admin?.email || !admin?.password || !manager?.email || !manager?.password || !employee?.email || !employee?.password) {
  throw new Error("The protected synthetic review fixture is incomplete.");
}
const parsedUrl = new URL(appUrl);
if (parsedUrl.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(parsedUrl.hostname)) {
  throw new Error("Only the loopback F07 review URL is allowed.");
}
if (state.migration !== "0052_user_totp_mfa" || !state.gitHead) throw new Error("F07 stack identity is incomplete.");

const localAppData = process.env.LOCALAPPDATA ?? "";
const browserPath = localAppData && path.join(localAppData, "Google", "Chrome", "Application", "chrome.exe");
const chrome = browserPath && await import("node:fs").then(({ existsSync }) => existsSync(browserPath))
  ? browserPath
  : "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const { existsSync } = await import("node:fs");
if (!existsSync(chrome)) throw new Error("Chrome is unavailable for F07 browser acceptance.");

const stamp = new Date().toISOString().replace(/[-:.]/g, "").replace("T", "-").replace("Z", "");
const profile = path.join(reviewRoot, `f07-browser-profile-${stamp}`);
const downloads = path.join(profile, "downloads");
const evidenceRoot = path.join(reviewRoot, "f07-browser-evidence");
mkdirSync(downloads, { recursive: true });
mkdirSync(evidenceRoot, { recursive: true });

let browser;
let socket;
let sequence = 0;
const pending = new Map();

function cdp(method, params = {}, timeoutMs = 30_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`F07 browser action timed out: ${method}`));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression, timeoutMs = 30_000) {
  const result = await cdp("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("A current-source F07 browser action raised an exception.");
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
  if (!ok) throw new Error(`F07 sign-in or filter control is missing: ${selector}`);
}

async function fillLabel(labelText, value) {
  const ok = await evaluate(`(()=>{const label=[...document.querySelectorAll('label')].find(x=>x.textContent.trim().startsWith(${JSON.stringify(labelText)}));const el=label?.querySelector('input');if(!el)return false;const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!ok) throw new Error(`First-run company setup field is missing: ${labelText}`);
}

async function selectField(labelText, value) {
  const ok = await evaluate(`(()=>{const label=[...document.querySelectorAll('label.field')].find(x=>x.querySelector('.field-label')?.textContent.trim()===${JSON.stringify(labelText)});const el=label?.querySelector('select');if(!el)return false;const option=[...el.options].find(x=>x.value===${JSON.stringify(value)});if(!option)return false;const setter=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value')?.set;setter.call(el,option.value);el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!ok) throw new Error(`F07 analysis selector is missing: ${labelText}`);
}

async function clickText(selector, text) {
  const ok = await evaluate(`(()=>{const e=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(text)}&&!x.disabled);if(!e)return false;e.click();return true})()`);
  if (!ok) throw new Error(`F07 control is unavailable: ${text}`);
}

async function api(method, route, body) {
  return evaluate(`(async()=>{const options={method:${JSON.stringify(method)},credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineF07'}};${body === undefined ? "" : `options.headers['Content-Type']='application/json';options.body=JSON.stringify(${JSON.stringify(body)});`}const r=await fetch(${JSON.stringify(route)},options);let data=null;try{if((r.headers.get('content-type')||'').includes('json'))data=await r.json()}catch{}return {status:r.status,data}})()`);
}

async function screenshot(name) {
  const result = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const destination = path.join(evidenceRoot, `${name}-${stamp}.png`);
  writeFileSync(destination, Buffer.from(result.data, "base64"), { flag: "wx" });
  return destination;
}

async function signIn(account, role) {
  await cdp("Page.navigate", { url: appUrl });
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`)) throw new Error("F07 sign-in form did not load.");
  await fill('form.login-form input[name="email"]', account.email);
  await fill('form.login-form input[name="password"]', account.password);
  await evaluate(`document.querySelector('form.login-form')?.requestSubmit()`);
  if (!await waitFor(`!!document.querySelector('.user-chip-role')`)) throw new Error(`Synthetic ${role} sign-in failed.`);
  if (role === "admin" && await evaluate(`!!document.querySelector('.first-run-setup')`)) {
    await fillLabel("Company name", "InfraSpine Synthetic QA");
    await fillLabel("Industry", "Infrastructure QA");
    await fillLabel("Product brand", "InfraSpine QA");
    await clickText("button", "Save company profile");
    if (!await waitFor(`!document.querySelector('.first-run-setup') && !!document.querySelector('button.nav-item')`, 20_000)) {
      throw new Error("Admin first-run company setup did not complete.");
    }
  }
  const identity = await api("GET", "/api/auth/me");
  if (identity.status !== 200 || identity.data?.role !== role) throw new Error(`The signed-in account is not ${role}.`);
  return identity.data.role;
}

async function openKnowledge() {
  if (!await waitFor(`!![...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.trim()==='Knowledge')`)) {
    throw new Error("The Knowledge navigation is unavailable.");
  }
  await clickText("button.nav-item", "Knowledge");
  if (!await waitFor(`document.querySelector('h1')?.textContent.trim()==='Knowledge'`)) {
    throw new Error("The Knowledge page did not load.");
  }
}

async function signOut() {
  await evaluate(`document.querySelector('button[aria-label="Sign out"]')?.click()`);
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`)) throw new Error("F07 sign-out did not return to the sign-in screen.");
}

async function connect() {
  const cleanEnv = { ...process.env };
  for (const key of Object.keys(cleanEnv)) {
    if (/^(INFRASPINE_.*(PASSWORD|FIXTURE)|DATABASE_URL|RAG_DATABASE_ANALYSIS_URL|MASTER_ENCRYPTION_KEY)$/i.test(key)) delete cleanEnv[key];
  }
  browser = spawn(chrome, ["--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
    "--disable-background-networking", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"],
  { stdio: "ignore", windowsHide: true, env: cleanEnv });
  const activePort = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activePort) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePort)) throw new Error("The isolated F07 Chrome profile did not start.");
  const port = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline && browser.exitCode === null) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      if (response.ok) page = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("The isolated F07 Chrome page did not become ready.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("The F07 browser connection failed.")), { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const item = pending.get(message.id);
    clearTimeout(item.timer);
    pending.delete(message.id);
    if (message.error) item.reject(new Error(`F07 browser command failed: ${item.method}`));
    else item.resolve(message.result);
  });
  await cdp("Page.enable");
  await cdp("Runtime.enable");
  await cdp("DOM.enable");
  await cdp("Network.enable");
  await cdp("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloads, eventsEnabled: true });
}

async function run() {
  await connect();
  await signIn(admin, "admin");
  await openKnowledge();
  await clickText("button.database-analysis-toggle", "Analyze business database");
  if (!await waitFor(`!!document.querySelector('.database-analysis-form') && [...document.querySelectorAll('.database-analysis-form select')].some(x=>[...x.options].some(o=>o.value==='erp.orders'))`)) {
    throw new Error("The Admin database catalog did not load from the configured synthetic source.");
  }

  const adminCatalog = await api("GET", "/api/rag/database-analysis/catalog");
  if (adminCatalog.status !== 200 || adminCatalog.data?.source_label !== "Synthetic F07 ERP replica" ||
      adminCatalog.data?.tables?.map((item) => item.name).join(",") !== "erp.orders") {
    throw new Error("The Admin source catalog did not match the allowlisted synthetic table.");
  }

  await selectField("Calculation", "sum");
  await selectField("Value column", "amount");
  await selectField("Filter column (optional)", "region");
  if (!await waitFor(`!!document.querySelector('.database-analysis-form label.field input')`)) throw new Error("The configured text filter did not render.");
  await fill(".database-analysis-form label.field input", "north");
  await clickText('button[type="submit"]', "Run read-only analysis");
  if (!await waitFor(`document.querySelector('.database-analysis-result')?.innerText.includes('30.75')`, 30_000)) {
    throw new Error("The Admin UI did not render the filtered synthetic sum.");
  }
  const sumView = await evaluate(`({text:document.querySelector('.database-analysis-result')?.innerText||'',provenance:document.querySelector('.database-analysis-provenance')?.innerText||''})`);
  if (!sumView.provenance.includes("Synthetic F07 ERP replica") || !sumView.provenance.includes("erp.orders")) {
    throw new Error("The F07 UI result omitted its configured source provenance.");
  }
  const uiSumScreenshot = await screenshot("admin-filtered-sum");

  const adminQuery = await api("POST", "/api/rag/database-analysis/query", {
    table: "erp.orders", operation: "sum", value: "amount", filters: [{ column: "region", operation: "eq", value: "north" }], limit: 10,
  });
  if (adminQuery.status !== 200 || adminQuery.data?.rows?.[0]?.[0] !== "30.75" || adminQuery.data?.truncated !== false) {
    throw new Error("The current-source F07 API did not return the bounded read-only result.");
  }
  console.log("F07_CURRENT_SOURCE_ADMIN_FILTERED_SUM_AND_PROVENANCE=PASS");

  await signOut();
  await signIn(manager, "manager");
  await openKnowledge();
  await clickText("button.database-analysis-toggle", "Analyze business database");
  if (!await waitFor(`!!document.querySelector('.database-analysis-form')`)) {
    throw new Error("The granted Manager did not receive the department-scoped database analysis UI.");
  }
  const managerCatalog = await api("GET", "/api/rag/database-analysis/catalog");
  const managerQuery = await api("POST", "/api/rag/database-analysis/query", {
    table: "erp.orders", operation: "count", filters: [{ column: "department_code", operation: "eq", value: "operations" }], limit: 10,
  });
  const managerAllowedQuery = await api("POST", "/api/rag/database-analysis/query", {
    table: "erp.orders", operation: "count", filters: [{ column: "department_code", operation: "eq", value: "finance" }], limit: 10,
  });
  const managerControl = await evaluate(`!!document.querySelector('button.database-analysis-toggle')`);
  if (managerCatalog.status !== 200 || !managerCatalog.data?.enabled ||
      managerCatalog.data?.tables?.map((item) => item.name).join(",") !== "erp.orders" ||
      managerQuery.status !== 200 || managerQuery.data?.rows?.[0]?.[0] !== "0" ||
      managerAllowedQuery.status !== 200 || managerAllowedQuery.data?.rows?.[0]?.[0] !== "2" || !managerControl) {
    throw new Error("The Manager row grant did not enforce its server-owned finance scope.");
  }
  const managerScreenshot = await screenshot("manager-f07-department-scope");
  console.log("F07_CURRENT_SOURCE_MANAGER_DEPARTMENT_SCOPE_AND_FILTER_BYPASS=PASS");

  await signOut();
  const employeeApiRole = await signIn(employee, "user");
  await openKnowledge();
  await clickText("button.database-analysis-toggle", "Analyze business database");
  if (!await waitFor(`!!document.querySelector('.database-analysis-status')`)) {
    throw new Error("The Employee with no reporting grant did not receive the no-access state.");
  }
  const employeeCatalog = await api("GET", "/api/rag/database-analysis/catalog");
  const employeeQuery = await api("POST", "/api/rag/database-analysis/query", { table: "erp.orders", operation: "count", limit: 10 });
  const employeeControl = await evaluate(`!!document.querySelector('button.database-analysis-toggle')`);
  const employeeForm = await evaluate(`!!document.querySelector('.database-analysis-form')`);
  if (employeeCatalog.status !== 200 || employeeCatalog.data?.enabled !== false ||
      employeeQuery.status !== 403 || !employeeControl || employeeForm) {
    throw new Error("Employee access without a department grant was not denied.");
  }
  await cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  const mobile = await evaluate(`({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})`);
  if (mobile.width !== 390 || mobile.scrollWidth > 390) throw new Error("The Employee F07 Knowledge view overflows at 390px.");
  const employeeScreenshot = await screenshot("employee-f07-no-grant-390px");
  console.log("F07_CURRENT_SOURCE_EMPLOYEE_NO_GRANT_403_AND_390PX=PASS");

  const evidence = {
    reviewUrl: appUrl,
    gitHead: state.gitHead,
    applicationDatabase: state.applicationDatabase,
    sourceDatabase: state.sourceDatabase,
    migration: state.migration,
    sourceLabel: "Synthetic F07 ERP replica",
    allowedCatalog: adminCatalog.data.tables.map((item) => item.name),
    adminCatalogStatus: adminCatalog.status,
    adminQueryStatus: adminQuery.status,
    adminSum: adminQuery.data.rows[0][0],
    uiSumText: sumView.text,
    provenance: sumView.provenance,
    managerCatalogStatus: managerCatalog.status,
    managerQueryStatus: managerQuery.status,
    managerOperationsFilteredCount: managerQuery.data.rows[0][0],
    managerFinanceCount: managerAllowedQuery.data.rows[0][0],
    managerControlVisible: managerControl,
    employeeCatalogStatus: employeeCatalog.status,
    employeeQueryStatus: employeeQuery.status,
    employeeCatalogEnabled: employeeCatalog.data.enabled,
    employeeControlVisible: employeeControl,
    employeeFormVisible: employeeForm,
    employeeApiRole,
    employeeViewport: mobile,
    workerPid: state.workerPid,
    uiSumScreenshot,
    managerScreenshot,
    employeeScreenshot,
    acceptedAtUtc: new Date().toISOString(),
  };
  const evidencePath = path.join(evidenceRoot, `f07-current-source-browser-${stamp}.json`);
  writeFileSync(evidencePath, `${JSON.stringify(evidence, null, 2)}\n`, { flag: "wx" });
  console.log("F07_CURRENT_SOURCE_BROWSER_ACCEPTANCE=PASS");
  console.log(`F07_EVIDENCE=${evidencePath}`);
}

try {
  await run();
} catch (error) {
  console.error(`F07_CURRENT_SOURCE_BROWSER_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  try { browser?.kill(); } catch {}
}
