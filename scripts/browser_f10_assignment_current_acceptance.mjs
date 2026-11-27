#!/usr/bin/env node
/** Authenticated current-source department task assignment acceptance. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_F10_ASSIGNMENT_URL ?? "http://127.0.0.1:5175/admin/");
const fixturePath = process.env.INFRASPINE_F10_ASSIGNMENT_FIXTURE;
const reviewRoot = process.env.INFRASPINE_F10_ASSIGNMENT_ROOT;
const statePath = process.env.INFRASPINE_F10_ASSIGNMENT_STATE;
if (base.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(base.hostname) || !fixturePath || !reviewRoot || !statePath) {
  throw new Error("F10 assignment acceptance requires the protected synthetic loopback configuration.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const managers = Array.isArray(fixture.managers) ? fixture.managers : [];
const employees = Array.isArray(fixture.employees) ? fixture.employees : [];
if (!fixture.admin?.email || !fixture.admin?.password || !managers.length || !employees.length) {
  throw new Error("The synthetic F10 role fixture is incomplete.");
}
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find(candidate => candidate && existsSync(candidate));
if (!browserPath) throw new Error("Chrome or Edge is unavailable.");

const title = "F10 QA Manager assignment current source";
const body = "Synthetic department assignment for current-source Manager, Employee and Admin access review.";
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-f10-assignment-"));
const evidenceRoot = path.join(reviewRoot, "f10-current-review-evidence");
mkdirSync(evidenceRoot, { recursive: true });
let browser;
let socket;
let sequence = 0;
const pending = new Map();

function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error("Browser acceptance timed out during " + method)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 20_000) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("An F10 current-source browser action failed.");
  return result.result?.value;
}
async function waitFor(expression, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await delay(200);
  }
  return false;
}
async function request(method, route, bodyValue) {
  const body = bodyValue === undefined ? "undefined" : JSON.stringify(bodyValue);
  const expression = "(async()=>{const init={method:" + JSON.stringify(method) +
    ",credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'XMLHttpRequest'}};" +
    "const body=" + body + ";if(body!==undefined){init.headers['Content-Type']='application/json';init.body=JSON.stringify(body)}" +
    "const response=await fetch(" + JSON.stringify(route) + ",init);let json=null;try{json=await response.json()}catch{}" +
    "return{status:response.status,json}})()";
  return evaluate(expression);
}
function requireStatus(result, expected, label) {
  if (result.status !== expected) throw new Error(label + " returned HTTP " + result.status);
}
async function signIn(account, role) {
  const started = await evaluate("(()=>{const email=document.querySelector('form.login-form input[name=\"email\"]');" +
    "const password=document.querySelector('form.login-form input[name=\"password\"]');const form=document.querySelector('form.login-form');" +
    "if(!email||!password||!form)return false;const set=(element,value)=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(element,value);" +
    "element.dispatchEvent(new Event('input',{bubbles:true}));element.dispatchEvent(new Event('change',{bubbles:true}))};" +
    "set(email," + JSON.stringify(account.email) + ");set(password," + JSON.stringify(account.password) + ");form.requestSubmit();return true})()");
  if (!started) throw new Error("The synthetic login form did not render.");
  if (!await waitFor("(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});" +
    "return r.ok&&(await r.json()).role===" + JSON.stringify(role) + "})()", 40_000)) {
    throw new Error("Synthetic " + role + " login failed.");
  }
}
async function signOut() {
  const clicked = await evaluate("(()=>{const b=document.querySelector('button[aria-label=\"Sign out\"]');b?.click();return Boolean(b)})()");
  if (!clicked || !await waitFor("Boolean(document.querySelector('form.login-form'))", 20_000)) throw new Error("Synthetic sign-out failed.");
}
async function openMyWork() {
  const clicked = await evaluate("(()=>{const b=[...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.trim()==='My work');b?.click();return Boolean(b)})()");
  if (!clicked || !await waitFor("Boolean([...document.querySelectorAll('h1')].some(x=>x.textContent.trim()==='My work'))")) {
    throw new Error("The authenticated My work page did not render.");
  }
  if (!await waitFor("!document.querySelector('.loading')", 20_000)) throw new Error("My work did not finish loading.");
}
async function setTaskField(labelStart, selector, value) {
  const expression = "(()=>{const form=document.querySelector('.personal-task-form');" +
    "const label=[...(form?.querySelectorAll('label')??[])].find(x=>x.textContent.trim().startsWith(" + JSON.stringify(labelStart) + "));" +
    "const element=label?.querySelector(" + JSON.stringify(selector) + ");if(!element)return false;" +
    "const proto=element instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:" +
    "element instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;" +
    "Object.getOwnPropertyDescriptor(proto,'value').set.call(element," + JSON.stringify(value) + ");" +
    "element.dispatchEvent(new Event('input',{bubbles:true}));element.dispatchEvent(new Event('change',{bubbles:true}));return true})()";
  if (!await evaluate(expression)) throw new Error("A required My work field was unavailable: " + labelStart);
}
async function myTasks() {
  const result = await request("GET", "/api/personal-tasks");
  requireStatus(result, 200, "Personal task list");
  return result.json?.tasks ?? [];
}
async function waitTask(taskId, status, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const task = (await myTasks()).find(item => item.id === taskId);
    if (task?.status === status) return task;
    await delay(250);
  }
  throw new Error("The assigned task state did not persist.");
}
async function connectBrowser() {
  const environment = {
    SYSTEMROOT: process.env.SYSTEMROOT, PATH: process.env.PATH, TEMP: process.env.TEMP,
    TMP: process.env.TMP, USERPROFILE: process.env.USERPROFILE, LOCALAPPDATA: process.env.LOCALAPPDATA,
  };
  browser = spawn(browserPath, [
    "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
    "--disable-background-networking", "--disable-component-update", "--disable-save-password-bubble",
    "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", "--user-data-dir=" + profile, base.href,
  ], { stdio: "ignore", windowsHide: false, env: environment });
  const activeFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activeFile) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activeFile)) throw new Error("The isolated F10 Chrome profile did not start.");
  const port = Number(readFileSync(activeFile, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline) {
    try {
      const response = await fetch("http://127.0.0.1:" + port + "/json/list");
      if (response.ok) page = (await response.json()).find(item => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("The isolated F10 browser page was unavailable.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("The browser debugger failed.")), { once: true });
  });
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const requestInfo = pending.get(message.id);
    clearTimeout(requestInfo.timer);
    pending.delete(message.id);
    if (message.error) requestInfo.reject(new Error("Browser command failed: " + requestInfo.method));
    else requestInfo.resolve(message.result);
  });
  await call("Page.enable");
  await call("Runtime.enable");
  await call("Page.setDeviceMetricsOverride", { width: 1365, height: 900, deviceScaleFactor: 1, mobile: false });
  await call("Page.navigate", { url: base.href });
  if (!await waitFor("Boolean(document.querySelector('form.login-form'))", 45_000)) throw new Error("F10 review login did not render.");
}
async function run() {
  await connectBrowser();
  let selectedManager;
  let selectedEmployee;
  let selectedDepartment;
  let managerOptions;
  for (const candidate of managers) {
    await signIn(candidate, "manager");
    const options = await request("GET", "/api/personal-tasks/options");
    requireStatus(options, 200, "Manager assignment options");
    const match = (options.json?.departments ?? []).map(department => ({
      department,
      employee: department.members.find(member => employees.some(account => account.email.toLowerCase() === member.email.toLowerCase())),
    })).find(item => item.employee);
    if (match) {
      selectedManager = candidate;
      selectedEmployee = employees.find(account => account.email.toLowerCase() === match.employee.email.toLowerCase());
      selectedDepartment = match.department;
      managerOptions = options.json;
      break;
    }
    await signOut();
  }
  if (!selectedManager || !selectedEmployee || !selectedDepartment) {
    throw new Error("No fixture Manager has an active fixture Employee in an assignable department.");
  }
  await openMyWork();
  let task = (await myTasks()).find(item => item.title === title &&
    item.assignee_user_id === selectedDepartment.members.find(member => member.email.toLowerCase() === selectedEmployee.email.toLowerCase())?.user_id);
  if (!task) {
    await setTaskField("Task title", "input", title);
    await setTaskField("Description", "textarea", body);
    const due = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000);
    const pad = value => String(value).padStart(2, "0");
    const dueValue = due.getFullYear() + "-" + pad(due.getMonth() + 1) + "-" + pad(due.getDate()) + "T" + pad(due.getHours()) + ":" + pad(due.getMinutes());
    await setTaskField("Deadline", "input[type='datetime-local']", dueValue);
    await setTaskField("Department assignment", "select", selectedDepartment.id);
    if (!await waitFor("Boolean([...document.querySelectorAll('.personal-task-form label')].some(x=>x.textContent.trim().startsWith('Assign to')))")) {
      throw new Error("The department assignee selector did not render.");
    }
    const targetId = selectedDepartment.members.find(member => member.email.toLowerCase() === selectedEmployee.email.toLowerCase()).user_id;
    await setTaskField("Assign to", "select", targetId);
    const submit = await evaluate("(()=>{const b=document.querySelector('.personal-task-form button[type=\"submit\"]');if(!b||b.disabled)return false;b.click();return true})()");
    if (!submit) throw new Error("Manager task assignment could not be submitted from My work.");
    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
      task = (await myTasks()).find(item => item.title === title && item.assignee_email.toLowerCase() === selectedEmployee.email.toLowerCase());
      if (task) break;
      await delay(250);
    }
    if (!task) throw new Error("The Manager-created task did not persist.");
  }
  if (task.created_by_email.toLowerCase() !== selectedManager.email.toLowerCase() ||
      task.assignee_email.toLowerCase() !== selectedEmployee.email.toLowerCase() ||
      task.department_group_id !== selectedDepartment.id) {
    throw new Error("The existing F10 task is not the expected Manager-to-Employee department assignment.");
  }
  if (task.status === "cancelled") throw new Error("The resumable F10 assignment is cancelled; no duplicate was created.");
  console.log("LOCAL_F10_CURRENT_MANAGER_UI_ASSIGNMENT=PASS");

  await signOut();
  await signIn(selectedEmployee, "employee");
  await openMyWork();
  let employeeTask = (await myTasks()).find(item => item.id === task.id);
  if (!employeeTask) throw new Error("The assigned Employee did not receive the task in My work.");
  if (employeeTask.status === "open") {
    const clicked = await evaluate("(()=>{const row=document.getElementById(" + JSON.stringify("personal-task-" + task.id) + ");" +
      "const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Complete');button?.click();return Boolean(button)})()");
    if (!clicked) throw new Error("The assigned Employee completion control was unavailable.");
    employeeTask = await waitTask(task.id, "completed");
  }
  if (employeeTask.status !== "completed" || !employeeTask.completed_at) throw new Error("The Employee task completion was not persisted.");
  console.log("LOCAL_F10_CURRENT_EMPLOYEE_COMPLETION=PASS");

  await signOut();
  await signIn(selectedManager, "manager");
  const managerTask = (await myTasks()).find(item => item.id === task.id);
  if (!managerTask || managerTask.status !== "completed" || !managerTask.completed_at) {
    throw new Error("The task creator could not read the completed assignment state.");
  }
  console.log("LOCAL_F10_CURRENT_MANAGER_COMPLETION_READBACK=PASS");

  await signOut();
  await signIn(fixture.admin, "admin");
  const adminTasks = await myTasks();
  if (adminTasks.some(item => item.id === task.id)) throw new Error("The unrelated Admin received a private personal-task record.");
  const denied = await request("GET", "/api/personal-tasks/" + task.id);
  requireStatus(denied, 404, "Unrelated Admin task isolation");
  console.log("LOCAL_F10_CURRENT_ADMIN_TASK_ISOLATION=PASS");

  await signOut();
  await signIn(selectedEmployee, "employee");
  await openMyWork();
  await call("Page.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  const fits = await evaluate("document.documentElement.scrollWidth<=window.innerWidth+1");
  if (!fits) throw new Error("The Employee task view overflows the 390px viewport.");
  const image = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  const id = path.basename(statePath, ".json");
  writeFileSync(path.join(evidenceRoot, id + "-employee-completed-390px.png"), Buffer.from(image.data, "base64"), { flag: "wx" });
  writeFileSync(statePath, JSON.stringify({
    task_id: task.id, department_group_id: task.department_group_id,
    status: employeeTask.status, created_by_user_id: task.created_by_user_id,
    assignee_user_id: task.assignee_user_id, completed_at: employeeTask.completed_at,
  }, null, 2) + "\n", { flag: "wx" });
  console.log("LOCAL_F10_CURRENT_EMPLOYEE_390PX=PASS");
  console.log("LOCAL_F10_CURRENT_ASSIGNMENT_BROWSER_TO_POSTGRESQL=PASS");
}

try {
  await run();
} catch (error) {
  console.error("LOCAL_F10_CURRENT_ASSIGNMENT_ACCEPTANCE=FAIL " + (error?.message ?? "unknown failure"));
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (browser?.exitCode === null) {
    browser.kill();
    await Promise.race([new Promise(resolve => browser.once("exit", resolve)), delay(5_000)]);
  }
  try { rmSync(profile, { recursive: true, force: true }); } catch {}
}
