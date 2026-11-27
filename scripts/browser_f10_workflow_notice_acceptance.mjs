#!/usr/bin/env node
/** Current-source browser-to-PostgreSQL acceptance for workflow notices and read receipts. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { randomUUID } from "node:crypto";

const base = new URL(process.env.INFRASPINE_F10_NOTICE_URL ?? "http://127.0.0.1:5175/admin/");
const fixturePath = process.env.INFRASPINE_F10_NOTICE_FIXTURE;
const reviewRoot = process.env.INFRASPINE_F10_NOTICE_ROOT;
const statePath = process.env.INFRASPINE_F10_NOTICE_STATE;
if (base.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(base.hostname) || !fixturePath || !reviewRoot || !statePath) {
  throw new Error("F10 notice acceptance requires the protected synthetic loopback configuration.");
}

const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const managers = Array.isArray(fixture.managers) ? fixture.managers : [];
const employees = Array.isArray(fixture.employees) ? fixture.employees : [];
if (!fixture.admin?.email || !fixture.admin?.password || !managers.length || !employees.length) {
  throw new Error("The protected synthetic role fixture is incomplete.");
}

const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find(candidate => candidate && existsSync(candidate));
if (!browserPath) throw new Error("Chrome or Edge is unavailable.");

const workflowName = "F10 current-source approval notification QA";
const taskTitle = "F10 approved workflow notification task QA";
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-f10-notice-"));
const evidenceRoot = path.join(reviewRoot, "f10-workflow-notice-evidence");
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
  if (result.exceptionDetails) throw new Error("An F10 notice browser action failed.");
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

function noticeRowStateExpression(runId, label, state) {
  const prefix = runId.slice(0, 8);
  const row = "const section=document.querySelector('section[aria-label=\"Workflow updates\"]');" +
    "const row=[...(section?.querySelectorAll('li.my-work-item')??[])].find(item=>item.innerText.includes(" + JSON.stringify("Run " + prefix) + ")&&" +
    "item.querySelector('strong')?.textContent.trim()===" + JSON.stringify(label) + ");";
  const condition = state === "unread"
    ? "!!row&&[...(row.querySelectorAll('button'))].some(button=>button.textContent.trim()==='Mark read')"
    : "!!row&&row.innerText.includes('Read')&&!row.innerText.includes('Unread')";
  return "(()=>{" + row + "return " + condition + "})()";
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

async function openAutomation() {
  const navigated = await evaluate("(()=>{window.location.hash='/automation';return true})()");
  if (!navigated || !await waitFor("Boolean([...document.querySelectorAll('h1')].some(x=>x.textContent.trim()==='Workflows & tasks'))")) {
    throw new Error("The authenticated workflow page did not render.");
  }
  if (!await waitFor("!document.querySelector('.loading')", 20_000)) throw new Error("The workflow page did not finish loading.");
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

async function chooseManagerScope() {
  for (const manager of managers) {
    await signIn(manager, "manager");
    const groupsResult = await request("GET", "/api/departments/managed");
    requireStatus(groupsResult, 200, "Manager departments");
    const groups = groupsResult.json?.groups ?? [];
    for (const group of groups.filter(item => item.kind === "department")) {
      const membersResult = await request("GET", `/api/departments/${encodeURIComponent(group.id)}/members`);
      requireStatus(membersResult, 200, "Department member list");
      const members = membersResult.json?.members ?? [];
      const employee = members.find(member => employees.some(account => account.email.toLowerCase() === member.email.toLowerCase()));
      if (employee) {
        return { manager, group, employee: employees.find(account => account.email.toLowerCase() === employee.email.toLowerCase()), employeeUserId: employee.user_id };
      }
    }
    await signOut();
  }
  throw new Error("No fixture Manager has an active fixture Employee in a managed department.");
}

async function findOrCreateWorkflow(scope) {
  const result = await request("GET", "/api/automation/workflows");
  requireStatus(result, 200, "Manager workflow list");
  let workflow = (result.json?.workflows ?? []).find(item => item.name === workflowName);
  if (workflow) {
    if (workflow.department_group_id !== scope.group.id || workflow.assignee_user_id !== scope.employeeUserId || !workflow.enabled) {
      throw new Error("The existing stable F10 workflow differs from the active synthetic department; no replacement was created.");
    }
    return workflow;
  }
  const created = await request("POST", "/api/automation/workflows", {
    name: workflowName,
    department_group_id: scope.group.id,
    trigger_type: "manual",
    interval_minutes: null,
    assignee_user_id: scope.employeeUserId,
    task_title: taskTitle,
    task_body: "Review this synthetic workflow assignment and mark the resulting notice read.",
    due_days: 5,
  });
  requireStatus(created, 201, "Manager workflow creation");
  return created.json;
}

async function findOrRequestRun(workflow) {
  const listed = await request("GET", "/api/automation/runs");
  requireStatus(listed, 200, "Manager workflow history");
  let run = (listed.json?.runs ?? []).find(item => item.workflow_id === workflow.id);
  if (run) {
    if (!["pending_approval", "queued", "completed"].includes(run.status)) {
      throw new Error("The stable F10 workflow already has a terminal run that cannot be safely resumed.");
    }
    return run;
  }
  const requested = await request("POST", `/api/automation/workflows/${encodeURIComponent(workflow.id)}/runs`, {
    idempotency_key: randomUUID(),
  });
  requireStatus(requested, 202, "Manager workflow run request");
  run = requested.json;
  if (run.status !== "pending_approval") throw new Error("The new workflow run did not wait for separate approval.");
  return run;
}

async function markNoticeRead(runId, notificationType, label, fallbackAccount, role) {
  await signOut();
  await signIn(fallbackAccount, role);
  await openMyWork();
  let notification;
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const result = await request("GET", "/api/automation/notifications");
    requireStatus(result, 200, "Workflow notification list");
    notification = (result.json?.items ?? []).find(item => item.run_id === runId && item.notification_type === notificationType);
    if (notification) break;
    await delay(250);
  }
  if (!notification) throw new Error("The expected workflow notice did not reach its role recipient.");
  if (!notification.read_at) {
    const prefix = runId.slice(0, 8);
    if (!await waitFor(noticeRowStateExpression(runId, label, "unread"), 15_000)) {
      throw new Error("The unread workflow notice did not render in My work before its read action.");
    }
    const clicked = await evaluate("(()=>{const section=document.querySelector('section[aria-label=\"Workflow updates\"]');" +
      "const row=[...(section?.querySelectorAll('li.my-work-item')??[])].find(item=>item.innerText.includes(" + JSON.stringify("Run " + prefix) + ")&&" +
      "item.querySelector('strong')?.textContent.trim()===" + JSON.stringify(label) + ");" +
      "const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Mark read');button?.click();return Boolean(button)})()");
    if (!clicked) throw new Error("The expected workflow notice had no My work Mark read action.");
    if (!await waitFor("(()=>{const section=document.querySelector('section[aria-label=\"Workflow updates\"]');" +
      "const row=[...(section?.querySelectorAll('li.my-work-item')??[])].find(item=>item.innerText.includes(" + JSON.stringify("Run " + prefix) + ")&&" +
      "item.querySelector('strong')?.textContent.trim()===" + JSON.stringify(label) + ");" +
      "return !!row&&row.innerText.includes('Read')&&!row.innerText.includes('Unread')})()", 20_000)) {
      throw new Error("My work did not update the workflow notice's read state.");
    }
  } else {
    const visible = await waitFor(noticeRowStateExpression(runId, label, "read"));
    if (!visible) throw new Error("The persisted workflow notice was not shown as read in My work.");
  }
  const after = await request("GET", "/api/automation/notifications");
  requireStatus(after, 200, "Workflow read receipt readback");
  const saved = (after.json?.items ?? []).find(item => item.id === notification.id);
  if (!saved?.read_at) throw new Error("The workflow read receipt did not persist through the API.");
  return { ...saved, recipient_user_id: (await request("GET", "/api/auth/me")).json?.id };
}

async function approveRunInUi(workflowNameToApprove, runId) {
  await signOut();
  await signIn(fixture.admin, "admin");
  await openAutomation();
  const runList = await request("GET", "/api/automation/runs");
  requireStatus(runList, 200, "Admin approval run list");
  const apiRun = (runList.json?.runs ?? []).find(item => item.id === runId);
  if (!apiRun || !apiRun.can_approve || apiRun.status !== "pending_approval") {
    throw new Error("Admin approval API state mismatch: found=" + Boolean(apiRun) + ", can_approve=" + Boolean(apiRun?.can_approve) + ", status=" + (apiRun?.status ?? "absent"));
  }
  const section = "const item=[...document.querySelectorAll('article.automation-item')].find(x=>x.querySelector('.automation-approval')&&" +
    "x.querySelector('h3')?.textContent.trim().includes(" + JSON.stringify(workflowNameToApprove) + "));";
  const textareaReady = await evaluate("(()=>{" + section + "return !!item?.querySelector('.automation-approval textarea')})()");
  if (!textareaReady) {
    const visible = await evaluate("JSON.stringify([...document.querySelectorAll('article.automation-item')].map(x=>({title:x.querySelector('h3')?.textContent.trim()??'',approval:!!x.querySelector('.automation-approval')})))");
    throw new Error("Admin approval UI omitted its API-approved pending run. Visible workflow cards=" + visible);
  }
  const filled = await evaluate("(()=>{" + section + "const area=item.querySelector('.automation-approval textarea');" +
    "Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(area,'Reviewed in current-source F10 synthetic acceptance.');" +
    "area.dispatchEvent(new Event('input',{bubbles:true}));area.dispatchEvent(new Event('change',{bubbles:true}));return true})()");
  if (!filled) throw new Error("Admin approval rationale could not be entered.");
  if (!await waitFor("(()=>{" + section + "const b=[...(item?.querySelectorAll('.automation-approval button')??[])].find(x=>x.textContent.trim()==='Approve');return !!b&&!b.disabled})()")) {
    throw new Error("The Admin approval control did not become available.");
  }
  const clicked = await evaluate("(()=>{" + section + "const b=[...(item?.querySelectorAll('.automation-approval button')??[])].find(x=>x.textContent.trim()==='Approve');b?.click();return Boolean(b)})()");
  if (!clicked) throw new Error("Admin could not approve the synthetic workflow run from the UI.");
  const completed = await waitFor("(async()=>{const response=await fetch('/api/automation/runs',{credentials:'same-origin',cache:'no-store'});" +
    "if(!response.ok)return false;const data=await response.json();const run=(data.runs??[]).find(item=>item.id===" + JSON.stringify(runId) + ");" +
    "return !!run&&['queued','completed'].includes(run.status)&&run.approval_decision==='approved'})()", 30_000);
  if (!completed) throw new Error("The Admin UI decision did not persist as an approved queued run.");
}

async function waitForTaskNotice(runId, employeeAccount) {
  await signOut();
  await signIn(employeeAccount, "employee");
  await openMyWork();
  const deadline = Date.now() + 45_000;
  let notice;
  let task;
  while (Date.now() < deadline) {
    const notices = await request("GET", "/api/automation/notifications");
    const tasks = await request("GET", "/api/automation/tasks");
    requireStatus(notices, 200, "Employee workflow notifications");
    requireStatus(tasks, 200, "Employee workflow tasks");
    notice = (notices.json?.items ?? []).find(item => item.run_id === runId && item.notification_type === "task_assigned");
    if (notice) {
      task = (tasks.json?.tasks ?? []).find(item => item.id === notice.task_id && item.run_id === runId);
      if (task) break;
    }
    await delay(350);
  }
  if (!notice || !task) throw new Error("The approved workflow task notice did not reach its assigned Employee.");
  const read = await markNoticeReadForCurrentUser(notice, runId, "A workflow task was assigned");
  await call("Page.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  const fits = await evaluate("document.documentElement.scrollWidth<=window.innerWidth+1");
  if (!fits) throw new Error("The Employee workflow notice view overflows the 390px viewport.");
  await evaluate("document.querySelector('section[aria-label=\"Workflow updates\"]')?.scrollIntoView({block:'center'})");
  const image = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  const evidenceId = path.basename(statePath, ".json");
  writeFileSync(path.join(evidenceRoot, evidenceId + "-employee-workflow-notice-390px.png"), Buffer.from(image.data, "base64"), { flag: "wx" });
  return { notification: read, task };
}

async function markNoticeReadForCurrentUser(notification, runId, label) {
  const prefix = runId.slice(0, 8);
  if (!notification.read_at) {
    if (!await waitFor(noticeRowStateExpression(runId, label, "unread"), 15_000)) {
      throw new Error("The Employee unread workflow notice did not render in My work before its read action.");
    }
    const clicked = await evaluate("(()=>{const section=document.querySelector('section[aria-label=\"Workflow updates\"]');" +
      "const row=[...(section?.querySelectorAll('li.my-work-item')??[])].find(item=>item.innerText.includes(" + JSON.stringify("Run " + prefix) + ")&&" +
      "item.querySelector('strong')?.textContent.trim()===" + JSON.stringify(label) + ");" +
      "const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Mark read');button?.click();return Boolean(button)})()");
    if (!clicked) {
      const diagnostic = await evaluate("JSON.stringify({heading:[...document.querySelectorAll('h1')].map(x=>x.textContent.trim())," +
        "section:document.querySelector('section[aria-label=\"Workflow updates\"]')?.innerText??null," +
        "rows:[...document.querySelectorAll('section[aria-label=\"Workflow updates\"] li.my-work-item')].map(x=>({title:x.querySelector('strong')?.textContent.trim()??''," +
        "summary:x.querySelector('small')?.textContent.trim()??'',buttons:[...x.querySelectorAll('button')].map(b=>b.textContent.trim())}))," +
        "errors:[...document.querySelectorAll('[role=alert],.banner-error')].map(x=>x.textContent.trim())})");
      throw new Error("The Employee workflow task notice had no Mark read action. My work DOM=" + diagnostic);
    }
    if (!await waitFor("(()=>{const section=document.querySelector('section[aria-label=\"Workflow updates\"]');" +
      "const row=[...(section?.querySelectorAll('li.my-work-item')??[])].find(item=>item.innerText.includes(" + JSON.stringify("Run " + prefix) + ")&&" +
      "item.querySelector('strong')?.textContent.trim()===" + JSON.stringify(label) + ");return !!row&&row.innerText.includes('Read')&&!row.innerText.includes('Unread')})()")) {
      throw new Error("Employee My work did not show the saved workflow read state.");
    }
  } else if (!await waitFor(noticeRowStateExpression(runId, label, "read"))) {
    throw new Error("The Employee workflow notice was not rendered as read in My work.");
  }
  const after = await request("GET", "/api/automation/notifications");
  requireStatus(after, 200, "Employee workflow read receipt readback");
  const saved = (after.json?.items ?? []).find(item => item.id === notification.id);
  if (!saved?.read_at) throw new Error("The Employee workflow task read receipt did not persist.");
  const me = await request("GET", "/api/auth/me");
  requireStatus(me, 200, "Employee identity readback");
  return { ...saved, recipient_user_id: me.json.id };
}

async function run() {
  await connectBrowser();
  const scope = await chooseManagerScope();
  const workflow = await findOrCreateWorkflow(scope);
  const runRecord = await findOrRequestRun(workflow);
  const managerUser = (await request("GET", "/api/auth/me")).json;
  if (!managerUser?.id) throw new Error("The Manager identity was unavailable.");
  console.log("LOCAL_F10_CURRENT_MANAGER_WORKFLOW_SETUP=PASS");

  const adminNoticePromise = (async () => {
    await signOut();
    await signIn(fixture.admin, "admin");
    const list = await request("GET", "/api/automation/notifications");
    requireStatus(list, 200, "Admin approval notice list");
    let notice = (list.json?.items ?? []).find(item => item.run_id === runRecord.id && item.notification_type === "approval_required");
    if (!notice) throw new Error("The independent Admin approver did not receive an approval-required notice.");
    await openMyWork();
    return notice;
  })();
  const adminNotice = await adminNoticePromise;
  const adminRead = await markCurrentAdminNoticeRead(adminNotice, runRecord.id);
  console.log("LOCAL_F10_CURRENT_ADMIN_APPROVAL_NOTICE_READ=PASS");

  const currentRun = (await request("GET", "/api/automation/runs")).json?.runs?.find(item => item.id === runRecord.id);
  if (!currentRun) throw new Error("The workflow run was not visible to the Admin.");
  if (currentRun.status === "pending_approval") await approveRunInUi(workflowName, runRecord.id);
  else if (!(["queued", "completed"].includes(currentRun.status) && currentRun.approval_decision === "approved")) {
    throw new Error("The workflow run is not in a safely resumable approved state.");
  }
  console.log("LOCAL_F10_CURRENT_ADMIN_UI_APPROVAL=PASS");

  const managerNotice = await markNoticeRead(runRecord.id, "run_approved", "A workflow run was approved", scope.manager, "manager");
  console.log("LOCAL_F10_CURRENT_MANAGER_APPROVED_NOTICE_READ=PASS");
  const employeeResult = await waitForTaskNotice(runRecord.id, scope.employee);
  console.log("LOCAL_F10_CURRENT_EMPLOYEE_TASK_NOTICE_READ=PASS");
  console.log("LOCAL_F10_CURRENT_WORKFLOW_NOTIFICATION_390PX=PASS");
  console.log("LOCAL_F10_CURRENT_WORKFLOW_NOTIFICATION_BROWSER_TO_POSTGRESQL=PASS");

  writeFileSync(statePath, JSON.stringify({
    workflow_id: workflow.id,
    run_id: runRecord.id,
    status: "approved",
    manager_user_id: managerUser.id,
    approval_notification_id: adminRead.id,
    approval_recipient_user_id: adminRead.recipient_user_id,
    approval_notification_type: adminRead.notification_type,
    manager_notification_id: managerNotice.id,
    manager_recipient_user_id: managerNotice.recipient_user_id,
    manager_notification_type: managerNotice.notification_type,
    employee_notification_id: employeeResult.notification.id,
    employee_recipient_user_id: employeeResult.notification.recipient_user_id,
    employee_notification_type: employeeResult.notification.notification_type,
    task_id: employeeResult.task.id,
    employee_task_status: employeeResult.task.status,
    workflow_name: workflowName,
  }, null, 2) + "\n", { flag: "wx" });
}

async function markCurrentAdminNoticeRead(notification, runId) {
  const label = "A workflow run needs an approval";
  if (!notification.read_at) {
    if (!await waitFor(noticeRowStateExpression(runId, label, "unread"), 15_000)) {
      throw new Error("The Admin unread workflow notice did not render in My work before its read action.");
    }
    const clicked = await evaluate("(()=>{const section=document.querySelector('section[aria-label=\"Workflow updates\"]');" +
      "const row=[...(section?.querySelectorAll('li.my-work-item')??[])].find(item=>item.innerText.includes(" + JSON.stringify("Run " + runId.slice(0, 8)) + ")&&" +
      "item.querySelector('strong')?.textContent.trim()===" + JSON.stringify(label) + ");" +
      "const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Mark read');button?.click();return Boolean(button)})()");
    if (!clicked) throw new Error("The Admin approval notice had no My work Mark read action.");
    if (!await waitFor(noticeRowStateExpression(runId, label, "read"))) {
      throw new Error("Admin My work did not reflect the approval notice read state.");
    }
  } else if (!await waitFor(noticeRowStateExpression(runId, label, "read"))) {
    throw new Error("The Admin approval notice was not rendered as read in My work.");
  }
  const after = await request("GET", "/api/automation/notifications");
  requireStatus(after, 200, "Admin workflow read receipt readback");
  const saved = (after.json?.items ?? []).find(item => item.id === notification.id);
  if (!saved?.read_at) throw new Error("The Admin approval notice read receipt did not persist.");
  const me = await request("GET", "/api/auth/me");
  requireStatus(me, 200, "Admin identity readback");
  return { ...saved, recipient_user_id: me.json.id };
}

try {
  await run();
} catch (error) {
  console.error("LOCAL_F10_CURRENT_WORKFLOW_NOTIFICATION_ACCEPTANCE=FAIL " + (error?.message ?? "unknown failure"));
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (browser?.exitCode === null) {
    browser.kill();
    await Promise.race([new Promise(resolve => browser.once("exit", resolve)), delay(5_000)]);
  }
  try { rmSync(profile, { recursive: true, force: true }); } catch {}
}
