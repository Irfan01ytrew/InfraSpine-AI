#!/usr/bin/env node
/** Authenticated browser acceptance for direct My Work tasks on an isolated synthetic restore. */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_F10_URL ?? "http://127.0.0.1:18107/");
const fixturePath = process.env.INFRASPINE_QA_USERS_FILE;
const restoreEnvPath = process.env.INFRASPINE_F10_RESTORE_ENV;
const roles = ["admin", "manager", "employee"];
let credentials;
let cdp;
let browser;
let profile;
const evidence = [];

if (!["http:", "https:"].includes(base.protocol) || !["localhost", "127.0.0.1", "::1"].includes(base.hostname)) {
  throw new Error("Personal-task browser acceptance is restricted to a loopback QA candidate.");
}
if (!fixturePath || !restoreEnvPath || !existsSync(fixturePath) || !existsSync(restoreEnvPath)) {
  throw new Error("Protected synthetic role fixture and isolated restore metadata are required.");
}
const restoreRoot = path.resolve(path.dirname(restoreEnvPath));
const project = path.basename(restoreRoot);
if (!/^infraspine-combined-restore-[a-f0-9]{12}$/i.test(project) || path.basename(restoreEnvPath) !== "restore-test.env") {
  throw new Error("Restore metadata is outside the expected isolated synthetic project.");
}
const restoreValues = Object.fromEntries(readFileSync(restoreEnvPath, "utf8").split(/\r?\n/).flatMap((row) => {
  const separator = row.indexOf("=");
  return separator > 0 ? [[row.slice(0, separator), row.slice(separator + 1)]] : [];
}));
if (!/^restore_[a-f0-9]{12}$/i.test(restoreValues.POSTGRES_DB ?? "") || !/^qa_[a-f0-9]{12}$/i.test(restoreValues.POSTGRES_USER ?? "")) {
  throw new Error("Restored synthetic database identity is invalid.");
}
credentials = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
for (const role of roles) {
  if (typeof credentials?.[role]?.email !== "string" || typeof credentials?.[role]?.password !== "string") {
    throw new Error("Protected fixture is missing a synthetic role account.");
  }
}

function findBrowser() {
  const candidates = process.platform === "win32"
    ? [
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
        path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
      ]
    : ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/microsoft-edge"];
  const browserPath = candidates.find((candidate) => candidate && existsSync(candidate));
  if (!browserPath) throw new Error("Chrome, Chromium or Edge was not found.");
  return browserPath;
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
    if (message.error) entry.reject(new Error("Browser command failed."));
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
        pending.set(id, { resolve, reject, timer });
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

async function waitFor(expression, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await delay(250);
  }
  return false;
}

async function request(method, route, body) {
  const result = await evaluate(`(async()=>{
    const init={method:${JSON.stringify(method)},credentials:'same-origin',headers:{'X-Requested-With':'XMLHttpRequest'}};
    const payload=${body === undefined ? "undefined" : JSON.stringify(body)};
    if(payload!==undefined){init.headers['Content-Type']='application/json';init.body=JSON.stringify(payload)}
    const response=await fetch(${JSON.stringify(route)},init);let json=null;
    try{json=await response.json()}catch{}
    return {status:response.status,json};
  })()`);
  if (!result || typeof result.status !== "number") throw new Error("Browser API request returned no status.");
  return result;
}

async function expect(method, route, status, body) {
  const result = await request(method, route, body);
  if (result.status !== status) throw new Error(`${method} ${route} returned HTTP ${result.status}; expected ${status}.`);
  return result.json;
}

async function signIn(role) {
  const account = credentials[role];
  const submitted = await evaluate(`(()=>{
    const email=document.querySelector('input[name="email"]');
    const password=document.querySelector('input[name="password"]');
    const form=document.querySelector('form.login-form');
    if(!email||!password||!form)return false;
    const set=(input,value)=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,value);input.dispatchEvent(new Event('input',{bubbles:true}))};
    set(email,${JSON.stringify(account.email)});set(password,${JSON.stringify(account.password)});
    form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return true;
  })()`);
  if (!submitted || !await waitFor(`(async()=>{const response=await fetch('/api/auth/me',{credentials:'same-origin'});return response.ok&&(await response.json()).role===${JSON.stringify(role)}})()`)) {
    throw new Error(`Synthetic ${role} login failed.`);
  }
  if (!await waitFor(`Boolean(document.querySelector('button.nav-item'))`)) throw new Error(`${role} navigation did not render.`);
}

async function signOut() {
  const clicked = await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Sign out"]');if(!button)return false;button.click();return true})()`);
  if (!clicked || !await waitFor(`Boolean(document.querySelector('form.login-form'))`)) throw new Error("Browser logout failed.");
}

async function openMyWork() {
  const clicked = await evaluate(`(()=>{const button=[...document.querySelectorAll('button.nav-item')].find((item)=>item.textContent.trim()==='My work');if(!button)return false;button.click();return true})()`);
  if (!clicked || !await waitFor(`Boolean([...document.querySelectorAll('h1')].some((item)=>item.textContent.trim()==='My work'))`)) {
    throw new Error("My work did not open in the browser.");
  }
  if (!await waitFor(`!document.querySelector('.loading')`)) throw new Error("My work data did not finish loading.");
}

function futureLocalDateTime() {
  const date = new Date(Date.now() + 48 * 60 * 60 * 1000);
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

async function setFormValue(selector, value, event = "input") {
  const result = await evaluate(`(()=>{
    const element=document.querySelector(${JSON.stringify(selector)});if(!element)return false;
    const proto=element instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto,'value').set.call(element,${JSON.stringify(value)});
    element.dispatchEvent(new Event(${JSON.stringify(event)},{bubbles:true}));return true;
  })()`);
  if (!result) throw new Error("A visible personal-task form control was unavailable.");
}

async function createTaskThroughUi(role, action) {
  const title = `QA ${role} task ${Date.now()}`;
  await setFormValue(".personal-task-form label:nth-of-type(1) input", title);
  await setFormValue(".personal-task-form label:nth-of-type(2) textarea", `Synthetic ${role} deadline acceptance task.`);
  await setFormValue(".personal-task-form input[type='datetime-local']", futureLocalDateTime());
  const clicked = await evaluate(`(()=>{const button=document.querySelector('.personal-task-form button[type="submit"]');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!clicked || !await waitFor(`[...document.querySelectorAll('.my-work-item strong')].some((item)=>item.textContent===${JSON.stringify(title)})`)) {
    throw new Error(`${role} could not create a task through the visible My work form.`);
  }
  const rows = await expect("GET", "/api/personal-tasks", 200);
  const task = rows.tasks.find((item) => item.title === title);
  if (!task || task.status !== "open" || task.assignee_user_id !== task.created_by_user_id || !task.due_at) {
    throw new Error(`${role} self-task did not persist with an authenticated owner and deadline.`);
  }
  const text = await evaluate(`(()=>{const row=[...document.querySelectorAll('li.my-work-item')].find((item)=>item.querySelector('.my-work-copy strong')?.textContent===${JSON.stringify(title)});return row?.innerText??''})()`);
  if (!text.includes("Due") || !text.includes("Assigned to")) throw new Error(`${role} task details were not visible in the inbox.`);
  evidence.push(`F10_${role.toUpperCase()}_CREATE_AND_DEADLINE_UI=PASS`);
  if (action === "complete") {
    const complete = await evaluate(`(()=>{const row=[...document.querySelectorAll('li.my-work-item')].find((item)=>item.querySelector('.my-work-copy strong')?.textContent===${JSON.stringify(title)});const button=[...(row?.querySelectorAll('button')??[])].find((item)=>item.textContent.trim()==='Complete');if(!button)return false;button.click();return true})()`);
    if (!complete || !await waitFor(`(()=>{const row=[...document.querySelectorAll('li.my-work-item')].find((item)=>item.querySelector('.my-work-copy strong')?.textContent===${JSON.stringify(title)});return row?.innerText.includes('completed')??false})()`)) {
      throw new Error(`${role} could not complete their task through the visible inbox.`);
    }
    const updated = await expect("GET", `/api/personal-tasks/${task.id}`, 200);
    if (updated.status !== "completed" || !updated.completed_at) throw new Error(`${role} task completion was not persisted.`);
    evidence.push(`F10_${role.toUpperCase()}_COMPLETE_UI_API=PASS`);
  } else {
    const cancel = await evaluate(`(()=>{const row=[...document.querySelectorAll('li.my-work-item')].find((item)=>item.querySelector('.my-work-copy strong')?.textContent===${JSON.stringify(title)});const button=[...(row?.querySelectorAll('button')??[])].find((item)=>item.textContent.trim()==='Cancel');if(!button)return false;button.click();return true})()`);
    if (!cancel || !await waitFor(`(()=>{const row=[...document.querySelectorAll('li.my-work-item')].find((item)=>item.querySelector('.my-work-copy strong')?.textContent===${JSON.stringify(title)});return row?.innerText.includes('cancelled')??false})()`)) {
      throw new Error(`${role} could not cancel their open task through the visible inbox.`);
    }
    const updated = await expect("GET", `/api/personal-tasks/${task.id}`, 200);
    if (updated.status !== "cancelled" || !updated.cancelled_at) throw new Error(`${role} task cancellation was not persisted.`);
    evidence.push(`F10_${role.toUpperCase()}_CANCEL_UI_API=PASS`);
  }
  return task.id;
}

async function run() {
  profile = mkdtempSync(path.join(tmpdir(), "infraspine-f10-browser-"));
  const activePortFile = path.join(profile, "DevToolsActivePort");
  browser = spawn(findBrowser(), [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-background-networking", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${profile}`, base.href,
  ], { stdio: "ignore", windowsHide: true });
  try {
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
    if (!await waitFor(`Boolean(document.querySelector('form.login-form'))`)) throw new Error("Synthetic QA login page did not render.");

    const taskIds = {};
    for (const [index, role] of roles.entries()) {
      if (index > 0) await signOut();
      await signIn(role);
      await openMyWork();
      const rows = await expect("GET", "/api/personal-tasks", 200);
      for (const [otherRole, taskId] of Object.entries(taskIds)) {
        if (rows.tasks.some((item) => item.id === taskId) || (await request("GET", `/api/personal-tasks/${taskId}`)).status !== 404) {
          throw new Error(`${role} could read ${otherRole}'s private synthetic task.`);
        }
      }
      evidence.push(`F10_${role.toUpperCase()}_PRIVATE_OWNER_BOUNDARY=PASS`);
      const action = role === "manager" ? "cancel" : "complete";
      taskIds[role] = await createTaskThroughUi(role, action);
    }
    const options = await expect("GET", "/api/personal-tasks/options", 200);
    if (options.departments.length !== 0) throw new Error("Employee was given manager/admin department assignment options.");
    evidence.push("F10_EMPLOYEE_NO_ASSIGNMENT_OPTIONS=PASS");
    evidence.forEach((marker) => console.log(marker));
    console.log("F10_PERSONAL_TASK_BROWSER_ACCEPTANCE=PASS");
  } finally {
    if (cdp) cdp.socket.close();
    if (browser.exitCode === null) {
      const exited = new Promise((resolve) => browser.once("exit", resolve));
      browser.kill();
      await Promise.race([exited, delay(5_000)]);
    }
    rmSync(profile, { recursive: true, force: true });
  }
}

run().catch((error) => {
  console.error(`F10_PERSONAL_TASK_BROWSER_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
});
