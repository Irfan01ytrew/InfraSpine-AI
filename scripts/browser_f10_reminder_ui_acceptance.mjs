#!/usr/bin/env node
/** Current-source My Work browser checks with an in-page synthetic API. */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { createServer as createNetServer } from "node:net";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const frontend = path.join(repo, "frontend");
const viteEntry = path.join(frontend, "node_modules", "vite", "bin", "vite.js");
const browserCandidates = process.platform === "win32"
  ? [
      "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
      "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
      path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
      path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
    ]
  : ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/microsoft-edge"];
const browserPath = browserCandidates.find((candidate) => candidate && existsSync(candidate));
if (!existsSync(viteEntry)) throw new Error("Frontend dependencies are not installed; Vite was not found.");
if (!browserPath) throw new Error("Chrome, Chromium or Edge was not found.");

let browser;
let vite;
let cdp;
let profile;
let vitePort;

async function freePort() {
  const server = createNetServer();
  await new Promise((resolve, reject) => server.once("error", reject).listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  return port;
}

function connectDebugger(endpoint) {
  const socket = new WebSocket(endpoint);
  const pending = new Map();
  let sequence = 0;
  const opened = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("Browser debugger connection failed.")), { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const entry = pending.get(message.id);
    clearTimeout(entry.timer);
    pending.delete(message.id);
    if (message.error) entry.reject(new Error(`Browser command failed: ${entry.method}`));
    else entry.resolve(message.result);
  });
  return {
    socket,
    ready: () => opened,
    command(method, params = {}, timeoutMs = 20_000) {
      const id = ++sequence;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(new Error(`Browser command timed out: ${method}`));
        }, timeoutMs);
        pending.set(id, { resolve, reject, timer, method });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
  };
}

async function evaluate(expression, timeoutMs = 20_000) {
  const response = await cdp.command("Runtime.evaluate", {
    expression, awaitPromise: true, returnByValue: true, userGesture: true,
  }, timeoutMs);
  if (response.exceptionDetails) throw new Error("Browser action raised an exception.");
  return response.result?.value;
}

async function waitFor(expression, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await delay(200);
  }
  return false;
}

function mockApiScript() {
  const fixture = `(() => {
    const user = { id: "synthetic-employee", email: "employee@example.test", role: "employee" };
    const today = new Date(); today.setHours(12, 0, 0, 0);
    const dueAt = today.toISOString();
    const state = window.__infraspineF10 = {
      calls: [],
      personalTask: {
        id: "synthetic-task", created_by_user_id: user.id, assignee_user_id: user.id,
        department_group_id: null, title: "Synthetic assignment due today", body: "Browser-only fixture",
        due_at: dueAt, status: "open", created_at: dueAt, completed_at: null, cancelled_at: null,
        created_by_email: user.email, assignee_email: user.email,
      },
      reminder: {
        id: "synthetic-reminder", task_id: "synthetic-task", title: "Synthetic due-soon reminder",
        reminder_type: "due_soon", due_at: dueAt, created_at: dueAt, read_at: null,
      },
    };
    const json = (value, status = 200) => new Response(JSON.stringify(value), {
      status, headers: { "Content-Type": "application/json" },
    });
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (input, init = {}) => {
      const requestUrl = new URL(typeof input === "string" ? input : input.url, location.href);
      if (!requestUrl.pathname.startsWith("/api/")) return originalFetch(input, init);
      const method = String(init.method ?? (typeof input === "string" ? "GET" : input.method) ?? "GET").toUpperCase();
      state.calls.push({ method, path: requestUrl.pathname });
      if (method === "GET" && requestUrl.pathname === "/api/auth/me") return json(user);
      if (method === "GET" && requestUrl.pathname === "/api/deployment/profile") return json({
        company_name: "Synthetic Company", industry: "Testing", brand_name: "Synthetic QA",
        primary_color: "#1765ad", setup_complete: true, template_id: null, template_version: null,
      });
      if (method === "GET" && requestUrl.pathname === "/api/personal-tasks") return json({ tasks: [state.personalTask] });
      if (method === "GET" && requestUrl.pathname === "/api/personal-tasks/reminders") return json({ reminders: [state.reminder] });
      if (method === "POST" && requestUrl.pathname === "/api/personal-tasks/reminders/synthetic-reminder/read") {
        state.reminder.read_at = new Date().toISOString();
        return json(state.reminder);
      }
      if (method === "GET" && requestUrl.pathname === "/api/personal-tasks/options") return json({ departments: [] });
      if (method === "GET" && requestUrl.pathname === "/api/automation/tasks") return json({ tasks: [] });
      if (method === "GET" && requestUrl.pathname === "/api/automation/notifications") return json({ items: [] });
      if (method === "GET" && requestUrl.pathname === "/api/service-requests/notifications") return json({ items: [] });
      if (method === "GET" && requestUrl.pathname === "/api/rag/training/reminders") return json({ reminders: [{
        id: "synthetic-training-reminder", course_id: "synthetic-course", topic: "Synthetic training deadline",
        reminder_type: "due_soon", due_at: state.personalTask.due_at, created_at: state.personalTask.created_at, read_at: state.personalTask.created_at,
      }] });
      if (method === "GET" && requestUrl.pathname === "/api/folder-sync/notifications") return json({ items: [] });
      if (method === "GET" && requestUrl.pathname === "/api/folder-sync/preferences") return json({ change_alerts_enabled: true });
      return json({ error: { code: "unmocked_route", message: "The browser test reached an unmocked API route." } }, 501);
    };
  })();`;
  return fixture;
}

async function startVite() {
  vitePort = await freePort();
  vite = spawn(process.execPath, [viteEntry, "--host", "127.0.0.1", "--port", String(vitePort), "--strictPort"], {
    cwd: frontend, stdio: "inherit", windowsHide: true,
  });
  const url = `http://127.0.0.1:${vitePort}/admin/`;
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline && vite.exitCode === null) {
    try {
      const response = await fetch(url);
      if (response.ok) return url;
    } catch {}
    await delay(150);
  }
  throw new Error("Vite did not become ready.");
}

async function startBrowser(url) {
  profile = mkdtempSync(path.join(tmpdir(), "infraspine-f10-ui-"));
  const activePortFile = path.join(profile, "DevToolsActivePort");
  browser = spawn(browserPath, [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-background-networking", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank",
  ], { stdio: "ignore", windowsHide: true });
  const deadline = Date.now() + 30_000;
  while (!existsSync(activePortFile) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePortFile)) throw new Error("Browser debugger did not start.");
  const port = Number(readFileSync(activePortFile, "utf8").split(/\r?\n/)[0]);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) throw new Error("Browser debugger port was invalid.");
  let target;
  while (!target && Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      if (response.ok) target = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!target) await delay(150);
  }
  if (!target) throw new Error("Browser page target did not become ready.");
  cdp = connectDebugger(target.webSocketDebuggerUrl);
  await cdp.ready();
  await cdp.command("Page.enable");
  await cdp.command("Runtime.enable");
  await cdp.command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await cdp.command("Page.addScriptToEvaluateOnNewDocument", { source: mockApiScript() });
  await cdp.command("Page.navigate", { url: `${url}#/my-work` });
  if (!await waitFor(`Boolean(document.querySelector('h1')?.textContent.includes('My work'))`)) {
    throw new Error("The current-source My work page did not render.");
  }
  if (!await waitFor(`!document.querySelector('.loading')`)) throw new Error("Synthetic My work data did not finish loading.");
}

async function run() {
  const url = await startVite();
  await startBrowser(url);

  const initial = await evaluate(`(() => ({
    reminder: document.querySelector('[aria-label="Assignment deadline reminders"]')?.innerText ?? "",
    training: document.querySelector('[aria-label="Training deadlines"]')?.innerText ?? "",
    agenda: document.querySelector('.work-calendar-agenda')?.innerText ?? "",
    unread: document.querySelector('.my-work-summary')?.innerText ?? "",
    dayCounts: [...document.querySelectorAll('.work-calendar-count')].map((item) => item.textContent),
  }))()`);
  if (!initial.reminder.includes("Synthetic due-soon reminder") || !initial.reminder.includes("Unread")) {
    throw new Error("The assignment reminder and unread state were not displayed.");
  }
  if (!initial.training.includes("Synthetic training deadline")) throw new Error("The training deadline was not displayed.");
  if (!initial.agenda.includes("Synthetic assignment due today") || !initial.agenda.includes("Synthetic training deadline")) {
    throw new Error("The selected-day agenda did not combine the personal task and training deadline.");
  }
  if (!initial.dayCounts.includes("2 due")) throw new Error("The calendar day count did not include both due items.");
  if (!initial.unread.replace(/\s+/g, " ").includes("1 unread reminders")) throw new Error("Unread reminder count did not start at one.");
  const layout = await evaluate(`({ viewport: window.innerWidth, page: document.documentElement.scrollWidth, calendar: document.querySelector('.my-work-calendar')?.scrollWidth ?? 0 })`);
  if (layout.viewport !== 390 || layout.page > layout.viewport || layout.calendar > layout.viewport) {
    throw new Error(`The 390px calendar layout overflows: ${JSON.stringify(layout)}`);
  }
  console.log("F10_CALENDAR_AND_REMINDER_RENDER_390PX=PASS");

  const monthBefore = await evaluate(`document.querySelector('.work-calendar-controls strong')?.textContent ?? ''`);
  const nextMonth = await evaluate(`(() => { const button=document.querySelector('button[aria-label="Next month"]'); if(!button)return false; button.click(); return true; })()`);
  if (!nextMonth || !await waitFor(`document.querySelector('.work-calendar-controls strong')?.textContent !== ${JSON.stringify(monthBefore)}`)) {
    throw new Error("Next month did not change the displayed calendar month.");
  }
  if (!await waitFor(`document.querySelector('.work-calendar-agenda')?.innerText.includes('No due items on this day.')`)) {
    throw new Error("Selecting next month did not show its empty selected-day agenda.");
  }
  const today = await evaluate(`(() => { const button=[...document.querySelectorAll('.work-calendar-controls button')].find((item)=>item.textContent.trim()==='Today'); if(!button)return false; button.click(); return true; })()`);
  if (!today || !await waitFor(`document.querySelector('.work-calendar-agenda')?.innerText.includes('Synthetic assignment due today')`)) {
    throw new Error("Today did not return the calendar to the current-day agenda.");
  }
  console.log("F10_CALENDAR_MONTH_AND_TODAY_NAVIGATION=PASS");

  const clicked = await evaluate(`(() => {
    const section = document.querySelector('[aria-label="Assignment deadline reminders"]');
    const button = [...(section?.querySelectorAll('button') ?? [])].find((item) => item.textContent.trim() === 'Mark read');
    if (!button) return false;
    button.click(); return true;
  })()`);
  if (!clicked || !await waitFor(`document.querySelector('[aria-label="Assignment deadline reminders"]')?.innerText.includes('Read')`)) {
    throw new Error("Mark read did not update the visible reminder state.");
  }
  if (!await waitFor(`/0\\s+unread reminders/.test(document.querySelector('.my-work-summary')?.innerText ?? '')`)) {
    throw new Error("Unread reminder count did not update after the read receipt.");
  }
  const calls = await evaluate(`window.__infraspineF10?.calls ?? []`);
  if (!calls.some((call) => call.method === "POST" && call.path === "/api/personal-tasks/reminders/synthetic-reminder/read")) {
    throw new Error("The UI did not send the reminder read-receipt request.");
  }
  console.log("F10_REMINDER_READ_RECEIPT_UI=PASS");
  console.log("F10_MOCKED_API_CALLS=" + calls.length);
  console.log("F10_CURRENT_SOURCE_BROWSER_ACCEPTANCE=PASS (synthetic in-page API; backend authorization not exercised)");
}

run().catch((error) => {
  console.error(`F10_REMINDER_UI_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
}).finally(async () => {
  if (cdp) cdp.socket.close();
  for (const processHandle of [browser, vite]) {
    if (processHandle && processHandle.exitCode === null) {
      const exited = new Promise((resolve) => processHandle.once("exit", resolve));
      processHandle.kill();
      await Promise.race([exited, delay(5_000)]);
    }
  }
  if (profile && existsSync(profile)) rmSync(profile, { recursive: true, force: true });
});
