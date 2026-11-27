#!/usr/bin/env node
/** Current-source cloud connector UI acceptance with an isolated synthetic API stub. */
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_C16_CLOUD_URL ?? "http://127.0.0.1:5184/admin/#/connectors");
if (base.protocol !== "http:" || base.hostname !== "127.0.0.1") {
  throw new Error("C16 cloud connector UI acceptance permits loopback HTTP only.");
}
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find(candidate => candidate && existsSync(candidate));
if (!browserPath) throw new Error("Chrome or Edge is unavailable.");

const groupId = randomUUID();
const connectorId = randomUUID();
const user = { id: randomUUID(), email: "synthetic-admin@example.test", role: "admin" };
const state = { submitted: null, connector: null, syncs: 0 };
const reviewRoot = path.resolve(process.env.INFRASPINE_C16_CLOUD_EVIDENCE ?? ".verification-local-review-20261029-c16-cloud/evidence");
const profileRoot = path.join(tmpdir(), "infraspine-c16-cloud-");
const profile = mkdtempSync(profileRoot);
const browserScript = `(() => {
  const groupId = ${JSON.stringify(groupId)};
  const connectorId = ${JSON.stringify(connectorId)};
  const user = ${JSON.stringify(user)};
  const originalFetch = window.fetch.bind(window);
  const state = { submitted: null, connector: null, syncs: 0, paths: [] };
  Object.defineProperty(window, "__c16CloudQa", { value: state });
  window.confirm = () => true;
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const method = String(init.method || "GET").toUpperCase();
    state.paths.push(method + " " + url.pathname);
    const respond = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
    const group = { id: groupId, name: "Synthetic Engineering", kind: "department", source: "local", lifecycle_status: "active", member_count: 1, created_at: new Date().toISOString() };
    if (url.pathname === "/api/auth/me") return respond(user);
    if (url.pathname === "/api/deployment/profile") return respond({ company_name: "Synthetic QA", industry: "General", brand_name: "InfraSpine QA", primary_color: "#145da0", setup_complete: true, template_id: null, template_version: null });
    if (url.pathname === "/api/admin/providers/catalog") return respond({ providers: [] });
    if (url.pathname === "/api/admin/providers") return respond({ providers: [], mode: "mock" });
    if (url.pathname === "/api/admin/audit-logs") return respond({ entries: [] });
    if (url.pathname === "/api/admin/access/groups") return respond({ groups: [group] });
    if (url.pathname === "/api/admin/users") return respond({ users: [] });
    if (url.pathname === "/api/connectors" && method === "GET") return respond(state.connector ? [state.connector] : []);
    if (url.pathname === "/api/connectors" && method === "POST") {
      state.submitted = JSON.parse(init.body || "{}");
      state.connector = { id: connectorId, name: state.submitted.name, connector_type: state.submitted.connector_type,
        visibility: state.submitted.visibility, department_group_id: groupId, sync_interval_minutes: state.submitted.sync_interval_minutes,
        is_active: true, sync_in_progress: false, last_sync_status: null, last_sync_error_count: 0, last_synced_at: null, created_at: new Date().toISOString() };
      return respond(state.connector, 201);
    }
    if (url.pathname === "/api/connectors/" + connectorId + "/sync" && method === "POST") {
      state.syncs += 1;
      state.connector.last_sync_status = "success";
      return respond({ connector_id: connectorId, status: "success", files_discovered: 1, files_synced: 1,
        files_queued: 1, files_unchanged: 0, files_skipped: 0, files_deleted: 0, errors: [] });
    }
    if (url.pathname.startsWith("/api/")) return respond({});
    return originalFetch(input, init);
  };
})();`;

let browser;
let socket;
let sequence = 0;
const pending = new Map();
function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error("Cloud connector browser acceptance timed out.")); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 20_000) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("A C16 cloud connector browser action failed.");
  return result.result?.value;
}
async function waitFor(expression, label, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(125);
  }
  throw new Error("Timed out waiting for " + label + ".");
}

try {
  const environment = {
    SYSTEMROOT: process.env.SYSTEMROOT, PATH: process.env.PATH, TEMP: process.env.TEMP,
    TMP: process.env.TMP, USERPROFILE: process.env.USERPROFILE, LOCALAPPDATA: process.env.LOCALAPPDATA,
  };
  browser = spawn(browserPath, ["--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-sync", "--disable-background-networking", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
    "--user-data-dir=" + profile, "--window-size=1365,1000", "about:blank"], { stdio: "ignore", windowsHide: true, env: environment });
  const activeFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activeFile) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activeFile)) throw new Error("Isolated Chromium did not start.");
  const port = Number(readFileSync(activeFile, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline) {
    try {
      const response = await fetch("http://127.0.0.1:" + port + "/json/list");
      if (response.ok) page = (await response.json()).find(item => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("Isolated Chromium page target was unavailable.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("Browser debugger connection failed.")), { once: true });
  });
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const item = pending.get(message.id);
    clearTimeout(item.timer); pending.delete(message.id);
    if (message.error) item.reject(new Error("Browser command failed."));
    else item.resolve(message.result);
  });
  await call("Page.enable");
  await call("Runtime.enable");
  await call("Page.addScriptToEvaluateOnNewDocument", { source: browserScript });
  await call("Emulation.setDeviceMetricsOverride", { width: 1365, height: 1000, deviceScaleFactor: 1, mobile: false });
  await call("Page.navigate", { url: base.href });
  await waitFor("Boolean(document.querySelector('.shell'))", "Admin shell", 45_000);
  const opened = await evaluate(`(() => { const button=[...document.querySelectorAll('button')].find(item=>item.textContent.trim()==='Cloud connectors'); if(!button)return false;button.click();return true; })()`);
  if (!opened) throw new Error("Cloud connector navigation was unavailable.");
  try {
    await waitFor("document.querySelector('h1')?.textContent.trim()==='Cloud connectors'", "Cloud connectors page");
  } catch {
    const diagnostic = await evaluate("({ href: location.href, heading: document.querySelector('h1')?.textContent.trim() ?? null, nav: [...document.querySelectorAll('button')].map(button => button.textContent.trim()).filter(text => text.includes('Cloud') || text.includes('connect')), text: document.body.innerText.slice(-700) })");
    throw new Error("Cloud connectors page did not render: " + JSON.stringify(diagnostic));
  }

  const initial = await evaluate(`(() => ({ required: document.querySelector('select[required]')?.closest('label')?.textContent.includes('Department access') ?? false,
    warning: document.querySelector('.banner-warning')?.textContent.includes('provider file-level permissions are not mirrored yet') ?? false,
    options: [...document.querySelectorAll('.cloud-connector-form select option')].map(option=>option.textContent.trim()) }))()`);
  if (!initial.required || !initial.warning || initial.options.includes("Private") || initial.options.includes("Shared")) {
    throw new Error("The connector form did not enforce and explain its department scope.");
  }

  const filled = await evaluate(`(() => {
    const set = (labelText, value) => { const label=[...document.querySelectorAll('label')].find(item=>item.querySelector('.field-label')?.textContent.trim()===labelText);
      const element=label?.querySelector('input,textarea,select'); if(!element)return false;
      const prototype=element instanceof HTMLSelectElement?HTMLSelectElement.prototype:element instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype,'value').set.call(element,value);element.dispatchEvent(new Event('input',{bubbles:true}));element.dispatchEvent(new Event('change',{bubbles:true}));return true; };
    return set('Name','Synthetic Cloud QA')&&set('Client ID','synthetic-client')&&set('Client secret','synthetic-secret')&&
      set('Refresh token','synthetic-refresh-token')&&set('Department access',${JSON.stringify(groupId)});
  })()`);
  if (!filled) throw new Error("Required synthetic connector fields were unavailable.");
  await evaluate(`(() => { const button=[...document.querySelectorAll('button')].find(item=>item.textContent.trim()==='Save connector');if(button)button.click() })()`);
  await waitFor("Boolean(window.__c16CloudQa?.submitted)", "connector save submission", 15_000);
  await waitFor("document.body.innerText.includes('Synthetic Engineering')", "saved scoped connector", 15_000);
  const saved = await evaluate(`(() => ({ payload: { visibility: window.__c16CloudQa.submitted.visibility, department_group_id: window.__c16CloudQa.submitted.department_group_id,
      name: window.__c16CloudQa.submitted.name, connector_type: window.__c16CloudQa.submitted.connector_type },
    credential_fields_cleared: [...document.querySelectorAll('.cloud-connector-form input[type=password]')].every(input=>input.value===''),
    warning_visible: document.querySelector('.banner-warning')?.textContent.includes('provider file-level permissions are not mirrored yet') ?? false }))()`);
  if (saved.payload.visibility !== "restricted" || saved.payload.department_group_id !== groupId || !saved.credential_fields_cleared) {
    throw new Error("Connector creation did not preserve the selected restricted scope or clear credential inputs.");
  }

  await evaluate(`(() => { const button=[...document.querySelectorAll('.cloud-connector-card button')].find(item=>item.textContent.trim()==='Sync now');if(button)button.click() })()`);
  await waitFor("window.__c16CloudQa?.syncs===1", "synthetic connector sync", 15_000);
  await waitFor("document.querySelector('[role=status]')?.textContent.includes('1 queued for processing')", "queued processing result", 15_000);
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true, screenWidth: 390, screenHeight: 844 });
  await delay(300);
  const mobile = await evaluate("({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth })");
  if (mobile.document > mobile.viewport || mobile.body > mobile.viewport) throw new Error("Cloud connectors page overflows at 390px.");

  mkdirSync(reviewRoot, { recursive: true });
  writeFileSync(path.join(reviewRoot, "c16-cloud-connectors-browser.json"), JSON.stringify({
    source: "current Vite source with isolated synthetic API stub", url: base.href,
    department_required: initial.required, provider_acl_limit_explained: saved.warning_visible,
    saved_restricted_scope: saved.payload, credential_fields_cleared: saved.credential_fields_cleared,
    sync_queued_count: 1, mobile, provider_calls: 0,
  }, null, 2) + "\n", "utf8");
  console.log("C16 cloud connector current-source browser acceptance passed.");
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (browser && browser.exitCode === null) {
    const exited = new Promise(resolve => browser.once("exit", resolve));
    browser.kill();
    await Promise.race([exited, delay(2000)]);
  }
  try { rmSync(profile, { recursive: true, force: true, maxRetries: 3, retryDelay: 200 }); } catch {}
}
