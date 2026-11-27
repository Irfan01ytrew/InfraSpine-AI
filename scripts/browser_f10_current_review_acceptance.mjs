#!/usr/bin/env node
/** Current-source reminder acceptance against the protected loopback synthetic review stack. */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const base = new URL(process.env.INFRASPINE_F10_CURRENT_URL ?? "http://127.0.0.1:5175/admin/");
const fixturePath = process.env.INFRASPINE_F10_FIXTURE;
const reviewRoot = process.env.INFRASPINE_F10_REVIEW_ROOT;
const databaseUrl = process.env.DATABASE_URL;
const pythonPath = process.env.INFRASPINE_F10_PYTHON;
if (base.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(base.hostname)) {
  throw new Error("F10 acceptance is restricted to loopback HTTP.");
}
if (!fixturePath || !reviewRoot || !databaseUrl || !pythonPath || !existsSync(fixturePath)) {
  throw new Error("The protected synthetic review acceptance configuration is incomplete.");
}
const credentials = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const accounts = { admin: credentials.admin, employee: credentials.employee ?? credentials.employees?.[0] };
for (const role of ["employee", "admin"]) {
  if (typeof accounts[role]?.email !== "string" || typeof accounts[role]?.password !== "string") {
    throw new Error("The protected synthetic role fixture is incomplete.");
  }
}

const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((candidate) => candidate && existsSync(candidate));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for local browser acceptance.");

const stamp = new Date().toISOString().replace(/[-:.]/g, "").replace("T", "-").replace("Z", "");
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-f10-current-"));
const evidenceRoot = path.join(reviewRoot, "f10-current-review-evidence");
mkdirSync(evidenceRoot, { recursive: true });
let browser;
let socket;
let sequence = 0;
const pending = new Map();

function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser acceptance timed out during ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression, timeoutMs = 20_000) {
  const response = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (response.exceptionDetails) throw new Error("A current-source browser acceptance action failed.");
  return response.result?.value;
}

async function waitFor(expression, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await delay(200);
  }
  return false;
}

async function request(method, route, body) {
  const response = await evaluate(`(async()=>{const init={method:${JSON.stringify(method)},credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'XMLHttpRequest'}};const body=${body === undefined ? "undefined" : JSON.stringify(body)};if(body!==undefined){init.headers['Content-Type']='application/json';init.body=JSON.stringify(body)}const r=await fetch(${JSON.stringify(route)},init);let json=null;try{json=await r.json()}catch{}return {status:r.status,json}})()`);
  return response;
}

async function signIn(role) {
  const account = accounts[role];
  await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const password=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!password||!form)return false;const set=(el,value)=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))};set(email,${JSON.stringify(account.email)});set(password,${JSON.stringify(account.password)});form.requestSubmit();return true})()`);
  if (!await waitFor(`(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;return (await r.json()).role===${JSON.stringify(role)}})()`, 40_000)) {
    throw new Error(`Synthetic ${role} authentication did not complete.`);
  }
}

async function signOut() {
  const clicked = await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Sign out"]');button?.click();return !!button})()`);
  if (!clicked || !await waitFor(`Boolean(document.querySelector('form.login-form'))`)) throw new Error("Local QA browser sign-out did not complete.");
}

async function openMyWork() {
  const clicked = await evaluate(`(()=>{const button=[...document.querySelectorAll('button.nav-item')].find(item=>item.textContent.trim()==='My work');button?.click();return !!button})()`);
  if (!clicked || !await waitFor(`Boolean([...document.querySelectorAll('h1')].some(item=>item.textContent.trim()==='My work'))`)) throw new Error("My work did not render in the current-source browser.");
  if (!await waitFor(`!document.querySelector('.loading')`)) throw new Error("My work data did not finish loading.");
}

function localDateTime(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

async function setValue(selector, value) {
  const changed = await evaluate(`(()=>{const element=document.querySelector(${JSON.stringify(selector)});if(!element)return false;const proto=element instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(element,${JSON.stringify(value)});element.dispatchEvent(new Event('input',{bubbles:true}));element.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!changed) throw new Error("A visible My work form field was unavailable.");
}

async function createOverdueTask() {
  const before = await request("GET", "/api/personal-tasks");
  if (before.status !== 200) throw new Error("The authenticated personal-task API did not return HTTP 200.");
  const prior = before.json?.tasks?.find((item) => item.title.startsWith("QA reminder persistence ") && item.status === "open" && item.due_at && new Date(item.due_at).valueOf() <= Date.now());
  if (prior) return { task: prior, title: prior.title, resumed: true };
  const title = `QA reminder persistence ${stamp}`;
  await setValue(".personal-task-form label:nth-of-type(1) input", title);
  await setValue(".personal-task-form label:nth-of-type(2) textarea", "Synthetic acceptance of worker-created due-date reminders and persisted read receipts.");
  await setValue(".personal-task-form input[type='datetime-local']", localDateTime(new Date(Date.now() - 2 * 60 * 60 * 1000)));
  const clicked = await evaluate(`(()=>{const button=document.querySelector('.personal-task-form button[type="submit"]');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!clicked || !await waitFor(`[...document.querySelectorAll('li.my-work-item strong')].some(item=>item.textContent===${JSON.stringify(title)})`)) {
    throw new Error("The synthetic Employee could not create a personal task with a due time through My work.");
  }
  const listing = await request("GET", "/api/personal-tasks");
  if (listing.status !== 200) throw new Error("The authenticated personal-task API did not return HTTP 200.");
  const task = listing.json?.tasks?.find((item) => item.title === title);
  if (!task || task.status !== "open" || task.assignee_user_id !== task.created_by_user_id || !task.due_at || new Date(task.due_at).valueOf() > Date.now()) {
    throw new Error("The Employee task deadline or private owner did not persist as expected.");
  }
  return { task, title, resumed: false };
}

function createWorkerReminder() {
  const workerEnv = {
    SYSTEMROOT: process.env.SYSTEMROOT,
    PATH: process.env.PATH,
    TEMP: process.env.TEMP,
    TMP: process.env.TMP,
    USERPROFILE: process.env.USERPROFILE,
    PYTHONIOENCODING: "utf-8",
    DATABASE_URL: databaseUrl,
  };
  const code = `import asyncio, os\nfrom sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker\nfrom app.services.personal_task_reminders import process_personal_task_reminders\nasync def main():\n engine=create_async_engine(os.environ["DATABASE_URL"])\n try:\n  created=await process_personal_task_reminders(async_sessionmaker(engine, expire_on_commit=False))\n  print(f"LOCAL_F10_WORKER_REMINDERS_CREATED={created}")\n finally:\n  await engine.dispose()\nasyncio.run(main())`;
  const result = spawnSync(pythonPath, ["-B", "-c", code], { cwd: path.resolve(path.dirname(fileURLToPath(import.meta.url)), ".."), env: workerEnv, encoding: "utf8", timeout: 45_000, windowsHide: true });
  if (result.error || result.status !== 0) throw new Error("The isolated personal-task reminder worker pass did not complete.");
  const match = result.stdout.match(/^LOCAL_F10_WORKER_REMINDERS_CREATED=(\d+)\s*$/m);
  if (!match || Number(match[1]) < 1) throw new Error("The local worker did not generate the newly due synthetic reminder.");
  return Number(match[1]);
}

async function screenshot(name) {
  const captured = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  const output = path.join(evidenceRoot, `${name}-${stamp}.png`);
  writeFileSync(output, Buffer.from(captured.data, "base64"), { flag: "wx" });
  return output;
}

async function run() {
  const env = {
    SYSTEMROOT: process.env.SYSTEMROOT,
    PATH: process.env.PATH,
    TEMP: process.env.TEMP,
    TMP: process.env.TMP,
    USERPROFILE: process.env.USERPROFILE,
    LOCALAPPDATA: process.env.LOCALAPPDATA,
  };
  browser = spawn(browserPath, [
    "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync", "--disable-background-networking",
    "--disable-component-update", "--disable-features=PasswordManagerOnboarding,PasswordManagerUI", "--disable-save-password-bubble",
    "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", `--user-data-dir=${profile}`, base.href,
  ], { stdio: "ignore", windowsHide: false, env });
  const activePort = path.join(profile, "DevToolsActivePort");
  const browserDeadline = Date.now() + 30_000;
  while (!existsSync(activePort) && Date.now() < browserDeadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePort)) throw new Error("The isolated current-source browser did not start.");
  const port = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < browserDeadline && browser.exitCode === null) {
    try { const response = await fetch(`http://127.0.0.1:${port}/json/list`); if (response.ok) page = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl); } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("The isolated current-source browser page did not become ready.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", () => reject(new Error("Browser debugger connection failed.")), { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const item = pending.get(message.id); clearTimeout(item.timer); pending.delete(message.id);
    if (message.error) item.reject(new Error(`Browser protocol call failed: ${item.method}`)); else item.resolve(message.result);
  });
  await call("Page.enable");
  await call("Runtime.enable");
  await call("Page.navigate", { url: base.href });
  if (!await waitFor(`Boolean(document.querySelector('form.login-form input[name="email"]'))`, 45_000)) throw new Error("The review app login page did not render.");

  await signIn("employee");
  await openMyWork();
  const { task, title } = await createOverdueTask();
  console.log("LOCAL_F10_CURRENT_EMPLOYEE_TASK_DEADLINE_UI_API=PASS");

  let reminders = (await request("GET", "/api/personal-tasks/reminders")).json?.reminders ?? [];
  let generated = 0;
  if (!reminders.some((item) => item.task_id === task.id)) generated = createWorkerReminder();
  const dueDeadline = Date.now() + 15_000;
  while (Date.now() < dueDeadline) {
    const result = await request("GET", "/api/personal-tasks/reminders");
    if (result.status !== 200) throw new Error("The Employee reminder API did not return HTTP 200.");
    reminders = result.json?.reminders ?? [];
    if (reminders.some((item) => item.task_id === task.id)) break;
    await delay(250);
  }
  const reminder = reminders.find((item) => item.task_id === task.id);
  if (!reminder || reminder.title !== title || reminder.reminder_type !== "overdue" || reminder.read_at !== null) {
    throw new Error("The worker-created overdue reminder was not visible and unread for its Employee recipient.");
  }
  await call("Page.reload", { ignoreCache: true });
  if (!await waitFor(`Boolean(document.querySelector('.user-chip-role'))`, 30_000) ||
      !await waitFor(`Boolean([...document.querySelectorAll('h1')].some(item=>item.textContent.trim()==='My work'))`, 30_000) ||
      !await waitFor(`!document.querySelector('.loading')`, 30_000)) {
    throw new Error("My work did not refresh after the worker generated the real reminder.");
  }
  if (!await waitFor(`(()=>{const section=document.querySelector('section[aria-label="Assignment deadline reminders"]');return [...(section?.querySelectorAll('li.my-work-item')??[])].some(row=>row.querySelector('strong')?.textContent===${JSON.stringify(title)}&&row.innerText.includes('Overdue')&&[...row.querySelectorAll('button')].some(button=>button.textContent.trim()==='Mark read'))})()`)) {
    throw new Error("The current-source Employee UI did not render its real overdue reminder.");
  }
  const rowClick = await evaluate(`(()=>{const section=document.querySelector('section[aria-label="Assignment deadline reminders"]');const row=[...(section?.querySelectorAll('li.my-work-item')??[])].find(item=>item.querySelector('strong')?.textContent===${JSON.stringify(title)});const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Mark read');button?.click();return !!button})()`);
  if (!rowClick) throw new Error("The Employee reminder read action was unavailable.");
  if (!await waitFor(`(()=>{const section=document.querySelector('section[aria-label="Assignment deadline reminders"]');const row=[...(section?.querySelectorAll('li.my-work-item')??[])].find(item=>item.querySelector('strong')?.textContent===${JSON.stringify(title)});return !!row&&row.innerText.includes('Read')&&!row.innerText.includes('Unread')})()`)) {
    throw new Error("The Employee UI did not reflect the saved reminder read state.");
  }
  const persisted = await request("GET", "/api/personal-tasks/reminders");
  const saved = persisted.json?.reminders?.find((item) => item.id === reminder.id);
  if (persisted.status !== 200 || !saved?.read_at) throw new Error("The reminder read receipt did not persist to PostgreSQL.");
  const shot = await screenshot("employee-overdue-reminder-read");
  console.log("LOCAL_F10_CURRENT_WORKER_REMINDER_AND_READ_RECEIPT=PASS");

  await signOut();
  await signIn("admin");
  const adminList = await request("GET", "/api/personal-tasks/reminders");
  const adminWrite = await request("POST", `/api/personal-tasks/reminders/${reminder.id}/read`);
  if (adminList.status !== 200 || (adminList.json?.reminders ?? []).some((item) => item.id === reminder.id) || adminWrite.status !== 404) {
    throw new Error("The current-source API exposed an Employee reminder to an unrelated Admin account.");
  }
  console.log("LOCAL_F10_CURRENT_REMINDER_RECIPIENT_SCOPE=PASS");
  console.log("LOCAL_F10_CURRENT_REVIEW_ACCEPTANCE=PASS");
  console.log("LOCAL_F10_CURRENT_REVIEW_EVIDENCE=" + JSON.stringify({ screenshot: shot, workerCreatedCount: generated }));
}

try {
  await run();
} catch (error) {
  console.error(`LOCAL_F10_CURRENT_REVIEW_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (browser?.exitCode === null) {
    browser.kill();
    await Promise.race([new Promise((resolve) => browser.once("exit", resolve)), delay(5_000)]);
  }
  try { rmSync(profile, { recursive: true, force: true }); } catch {}
}
