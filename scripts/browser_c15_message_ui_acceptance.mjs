#!/usr/bin/env node
/** Current-source browser acceptance for the bounded workflow inbox-message editor. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { randomUUID } from "node:crypto";

const base = new URL(process.env.INFRASPINE_C15_MESSAGE_URL ?? "http://127.0.0.1:5183/admin/#/automation");
const evidenceRoot = path.resolve(process.env.INFRASPINE_C15_MESSAGE_EVIDENCE ?? ".verification-local-review-20261029-c15-message/evidence");
if (base.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(base.hostname)) {
  throw new Error("C15 message UI acceptance permits loopback HTTP only.");
}

const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find(candidate => candidate && existsSync(candidate));
if (!browserPath) throw new Error("Chrome or Edge is unavailable.");

const groupId = randomUUID();
const memberId = randomUUID();
const user = { id: randomUUID(), email: "admin@example.test", role: "admin" };
const memberEmail = "employee@example.test";
const state = { workflow: null, submitted: null };
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-c15-message-ui-"));
const browserScript = `(() => {
  const user = ${JSON.stringify(user)};
  const groupId = ${JSON.stringify(groupId)};
  const memberId = ${JSON.stringify(memberId)};
  const memberEmail = ${JSON.stringify(memberEmail)};
  const originalFetch = window.fetch.bind(window);
  const state = { workflow: null, submitted: null, requests: [] };
  Object.defineProperty(window, "__c15Qa", { value: state });
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const method = String(init.method || "GET").toUpperCase();
    state.requests.push({ method, path: url.pathname });
    const respond = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
    if (url.pathname === "/api/auth/me") return respond(user);
    if (url.pathname === "/api/deployment/profile") return respond({ company_name: "QA Company", industry: "General", brand_name: "InfraSpine QA", primary_color: "#145da0", setup_complete: true, template_id: null, template_version: null });
    if (url.pathname === "/api/admin/providers/catalog") return respond({ providers: [] });
    if (url.pathname === "/api/admin/providers") return respond({ providers: [], mode: "mock" });
    if (url.pathname === "/api/admin/audit-logs") return respond({ entries: [] });
    if (url.pathname === "/api/admin/access/groups") return respond({ groups: [{ id: groupId, name: "QA Finance", kind: "department", source: "local", external_id: null, member_count: 1, created_at: new Date().toISOString() }] });
    if (url.pathname === "/api/admin/users") return respond({ users: [{ id: memberId, email: memberEmail, is_active: true, departments: [{ id: groupId, name: "QA Finance" }] }] });
    if (url.pathname === "/api/automation/workflows" && method === "GET") return respond({ workflows: state.workflow ? [state.workflow] : [] });
    if (url.pathname === "/api/automation/workflows" && method === "POST") {
      state.submitted = JSON.parse(init.body || "{}");
      const body = state.submitted;
      state.workflow = {
        id: crypto.randomUUID(), definition_key: crypto.randomUUID(), version: 1, name: body.name,
        department_group_id: body.department_group_id, department_name: "QA Finance", trigger_type: body.trigger_type,
        interval_minutes: body.interval_minutes, action_type: body.action_type, assignee_user_id: null,
        task_title: null, task_body: null, due_days: null,
        notification_recipient_user_id: body.notification_recipient_user_id,
        notification_title: body.notification_title, notification_body: body.notification_body,
        enabled: true, next_run_at: null, created_at: new Date().toISOString(), can_manage: true,
      };
      return respond(state.workflow, 201);
    }
    if (url.pathname === "/api/automation/runs") return respond({ runs: [] });
    if (url.pathname === "/api/automation/tasks") return respond({ tasks: [] });
    if (url.pathname === "/api/automation/notifications") return respond({ items: [] });
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
    const timer = setTimeout(() => { pending.delete(id); reject(new Error("Browser acceptance timed out during " + method)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression, timeoutMs = 20_000) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("A C15 message browser action failed.");
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

async function connectBrowser() {
  const environment = {
    SYSTEMROOT: process.env.SYSTEMROOT, PATH: process.env.PATH, TEMP: process.env.TEMP,
    TMP: process.env.TMP, USERPROFILE: process.env.USERPROFILE, LOCALAPPDATA: process.env.LOCALAPPDATA,
  };
  browser = spawn(browserPath, [
    "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
    "--disable-background-networking", "--disable-component-update", "--disable-save-password-bubble", "--headless=new",
    "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", "--user-data-dir=" + profile,
    "--window-size=1365,1000", "about:blank",
  ], { stdio: "ignore", windowsHide: true, env: environment });
  const activeFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activeFile) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activeFile)) throw new Error("The isolated C15 browser profile did not start.");
  const port = Number(readFileSync(activeFile, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline) {
    try {
      const response = await fetch("http://127.0.0.1:" + port + "/json/list");
      if (response.ok) page = (await response.json()).find(item => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("The isolated C15 browser page was unavailable.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("The C15 browser debugger failed.")), { once: true });
  });
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const item = pending.get(message.id);
    clearTimeout(item.timer);
    pending.delete(message.id);
    if (message.error) item.reject(new Error("Browser command failed: " + message.error.message));
    else item.resolve(message.result);
  });
  await call("Page.enable");
  await call("Runtime.enable");
  await call("Page.addScriptToEvaluateOnNewDocument", { source: browserScript });
  await call("Emulation.setDeviceMetricsOverride", { width: 1365, height: 1000, deviceScaleFactor: 1, mobile: false });
  await call("Page.navigate", { url: base.href });
  if (!await waitFor("[...document.querySelectorAll('h1')].some(h => h.textContent.trim() === 'Workflows & tasks')", 45_000)) {
    const state = await evaluate("({ href: location.href, text: document.body.innerText.slice(0, 500), qaStub: Boolean(window.__c15Qa) })");
    throw new Error("The current-source workflow UI did not render: " + JSON.stringify(state));
  }
  if (!await waitFor(`(() => { const button=[...document.querySelectorAll("button")].find(item => item.textContent.trim() === "Create workflow"); return Boolean(button && !button.disabled && [...document.querySelectorAll("select option")].some(option => option.textContent.includes("employee@example.test"))); })()`, 20_000)) {
    throw new Error("The synthetic workflow form did not finish loading its department members.");
  }
}

try {
  await connectBrowser();
  const opened = await evaluate(`(async() => {
    const set = async (labelText, value) => {
      const label = [...document.querySelectorAll("label")].find(item => item.querySelector(".field-label")?.textContent.trim() === labelText);
      const element = label?.querySelector("input,textarea,select");
      if (!element) return false;
      const prototype = element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, "value").set.call(element, value);
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
      await new Promise(resolve => setTimeout(resolve, 100));
      return true;
    };
    return await set("Workflow name", "Approved member message QA") &&
      await set("Department", ${JSON.stringify(groupId)}) &&
      await set("Approved action", "notify_member") &&
      await set("Message recipient", ${JSON.stringify(memberId)}) &&
      await set("Message title", "Quarterly review") &&
      await set("Message", "Please complete the synthetic access review.");
  })()`);
  if (!opened) throw new Error("The message action form did not expose all required fields.");
  const submitted = await evaluate(`(() => { const button = [...document.querySelectorAll("button")].find(item => item.textContent.trim() === "Create workflow"); if (!button) return false; button.click(); return true; })()`);
  if (!submitted) throw new Error("The workflow create action was unavailable.");
  if (!await waitFor("Boolean(window.__c15Qa?.submitted)", 20_000)) throw new Error("The message workflow request was not submitted.");
  if (!await waitFor("document.body.innerText.includes('Message: Quarterly review')", 20_000)) {
    throw new Error("The saved in-app message action did not appear in the workflow list.");
  }
  const result = await evaluate(`(() => ({ submitted: window.__c15Qa.submitted, workflow: window.__c15Qa.workflow, requests: window.__c15Qa.requests, visible: document.body.innerText.includes("Message: Quarterly review"), text: document.body.innerText.slice(-900), viewport: { width: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth } }))()`);
  const payload = result?.submitted;
  if (!payload || payload.action_type !== "notify_member" || payload.notification_title !== "Quarterly review" ||
      payload.notification_recipient_user_id !== memberId || !result.visible ||
      ["assignee_user_id", "task_title", "task_body", "due_days"].some(key => Object.hasOwn(payload, key))) {
    throw new Error("The rendered workflow form did not submit the expected message-only action: " + JSON.stringify(result));
  }
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await delay(300);
  const mobile = await evaluate("({ viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth })");
  if (!mobile || mobile.document > mobile.viewport || mobile.body > mobile.viewport) {
    throw new Error("The workflow message form overflows the mobile viewport.");
  }
  mkdirSync(evidenceRoot, { recursive: true });
  writeFileSync(path.join(evidenceRoot, "c15-message-ui-browser.json"), JSON.stringify({
    source: "current Vite source with isolated synthetic API stub",
    url: base.href,
    desktop: result.viewport,
    mobile,
    action_type: payload.action_type,
    message_title: payload.notification_title,
    recipient_user_id: payload.notification_recipient_user_id,
    task_fields_sent: false,
    workflow_visible_after_save: result.visible,
  }, null, 2) + "\n");
  console.log("C15 workflow message UI browser acceptance passed: form submit, saved workflow rendering, desktop and mobile layout.");
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (browser && browser.exitCode === null) {
    const exited = new Promise(resolve => browser.once("exit", resolve));
    browser.kill();
    await Promise.race([exited, delay(2000)]);
  }
  try { rmSync(profile, { recursive: true, force: true, maxRetries: 3, retryDelay: 200 }); } catch {}
}
