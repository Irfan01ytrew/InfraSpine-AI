#!/usr/bin/env node
/** Current-source browser/API acceptance for department service-request workflow triggers. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_F14_CURRENT_URL ?? "http://127.0.0.1:5175/admin/");
const fixturePath = process.env.INFRASPINE_F14_FIXTURE;
const reviewRoot = process.env.INFRASPINE_F14_REVIEW_ROOT;
if (base.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(base.hostname)) throw new Error("F14 browser acceptance is restricted to loopback HTTP.");
if (!fixturePath || !reviewRoot || !existsSync(fixturePath)) throw new Error("The protected synthetic F14 fixture is unavailable.");
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const employeeAccounts = Array.isArray(fixture.employees) ? fixture.employees : fixture.employee ? [fixture.employee] : [];
if (typeof fixture.admin?.email !== "string" || typeof fixture.admin?.password !== "string" ||
    !employeeAccounts.some((item) => typeof item?.email === "string" && typeof item?.password === "string")) {
  throw new Error("The protected synthetic Admin/Employee fixture is incomplete.");
}
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((candidate) => candidate && existsSync(candidate));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for local F14 acceptance.");

const stamp = new Date().toISOString().replace(/[-:.]/g, "").replace("T", "-").replace("Z", "");
const workflowPrefix = "QA help-request workflow ";
let workflowName = `${workflowPrefix}${stamp}`;
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-f14-current-"));
const evidenceRoot = path.join(reviewRoot, "f14-current-review-evidence");
mkdirSync(evidenceRoot, { recursive: true });
let browser; let socket; let sequence = 0;
const pending = new Map();

function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser acceptance timed out during ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method }); socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression, timeoutMs = 20_000) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("A current-source F14 browser action failed.");
  return result.result?.value;
}

async function waitFor(expression, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) { if (await evaluate(expression)) return true; await delay(200); }
  return false;
}

async function request(method, route, body) {
  return await evaluate(`(async()=>{const init={method:${JSON.stringify(method)},credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'XMLHttpRequest'}};const body=${body === undefined ? "undefined" : JSON.stringify(body)};if(body!==undefined){init.headers['Content-Type']='application/json';init.body=JSON.stringify(body)}const r=await fetch(${JSON.stringify(route)},init);let json=null;try{json=await r.json()}catch{}return {status:r.status,json}})()`);
}

async function signIn(account, role) {
  await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const password=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!password||!form)return false;const set=(el,value)=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))};set(email,${JSON.stringify(account.email)});set(password,${JSON.stringify(account.password)});form.requestSubmit();return true})()`);
  if (!await waitFor(`(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;return (await r.json()).role===${JSON.stringify(role)}})()`, 40_000)) throw new Error(`Synthetic ${role} authentication did not complete.`);
}

async function signOut() {
  const clicked = await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Sign out"]');button?.click();return !!button})()`);
  if (!clicked || !await waitFor(`Boolean(document.querySelector('form.login-form'))`)) throw new Error("The local QA browser could not sign out.");
}

async function openRoute(hash, title) {
  await evaluate(`window.location.hash=${JSON.stringify(hash)}`);
  if (!await waitFor(`Boolean([...document.querySelectorAll('h1')].some(item=>item.textContent.trim()===${JSON.stringify(title)}))`)) throw new Error(`The current-source ${title} page did not render.`);
  if (!await waitFor(`!document.querySelector('.loading')`)) throw new Error(`The current-source ${title} data did not finish loading.`);
}

async function setValue(selector, value) {
  const set = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:el instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!set) throw new Error("A required visible workflow/request form field was unavailable.");
}

async function submitRequest(category, departmentId, subject) {
  await setValue(".service-request-form label:nth-of-type(1) select", category);
  await setValue(".service-request-form label:nth-of-type(2) select", "normal");
  await setValue(".service-request-form label:nth-of-type(3) select", departmentId ?? "");
  await setValue(".service-request-form > label:nth-of-type(1) input", subject);
  await setValue(".service-request-form > label:nth-of-type(2) textarea", "Synthetic F14 event-trigger acceptance. Contains no customer content or secrets.");
  const clicked = await evaluate(`(()=>{const button=document.querySelector('.service-request-form button');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!clicked) throw new Error("The visible service-request submit control was not available.");
  const deadline = Date.now() + 20_000;
  let result;
  while (Date.now() < deadline) {
    result = await request("GET", "/api/service-requests");
    if (result.status !== 200) throw new Error("The authenticated service-request API did not return HTTP 200.");
    const found = result.json?.items?.find((item) => item.subject === subject);
    if (found) return found;
    await delay(200);
  }
  throw new Error("The synthetic service request did not persist through the visible form.");
}

async function screenshot(name) {
  const shot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  const file = path.join(evidenceRoot, `${name}-${stamp}.png`);
  writeFileSync(file, Buffer.from(shot.data, "base64"), { flag: "wx" });
  return file;
}

async function run() {
  const childEnv = { SYSTEMROOT: process.env.SYSTEMROOT, PATH: process.env.PATH, TEMP: process.env.TEMP, TMP: process.env.TMP, USERPROFILE: process.env.USERPROFILE, LOCALAPPDATA: process.env.LOCALAPPDATA };
  browser = spawn(browserPath, ["--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync", "--disable-background-networking", "--disable-component-update", "--disable-features=PasswordManagerOnboarding,PasswordManagerUI", "--disable-save-password-bubble", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", `--user-data-dir=${profile}`, base.href], { stdio: "ignore", windowsHide: false, env: childEnv });
  const activePort = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activePort) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePort)) throw new Error("The isolated current-source browser did not start.");
  const port = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline && browser.exitCode === null) { try { const r = await fetch(`http://127.0.0.1:${port}/json/list`); if (r.ok) page = (await r.json()).find((x) => x.type === "page" && x.webSocketDebuggerUrl); } catch {} if (!page) await delay(150); }
  if (!page) throw new Error("The isolated browser page did not become ready.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", () => reject(new Error("Browser debugger connection failed.")), { once: true }); });
  socket.addEventListener("message", (event) => { const msg = JSON.parse(event.data); if (!msg.id || !pending.has(msg.id)) return; const item = pending.get(msg.id); clearTimeout(item.timer); pending.delete(msg.id); if (msg.error) item.reject(new Error(`Browser protocol call failed: ${item.method}`)); else item.resolve(msg.result); });
  await call("Page.enable"); await call("Runtime.enable"); await call("Page.navigate", { url: base.href });
  if (!await waitFor(`Boolean(document.querySelector('form.login-form input[name="email"]'))`, 45_000)) throw new Error("The local Admin login page did not render.");
  await signIn(fixture.admin, "admin");
  await openRoute("/automation", "Workflows & tasks");

  const groupsResponse = await request("GET", "/api/admin/access/groups");
  const usersResponse = await request("GET", "/api/admin/users");
  const workflowResponse = await request("GET", "/api/automation/workflows");
  if (groupsResponse.status !== 200 || usersResponse.status !== 200 || workflowResponse.status !== 200) throw new Error("Admin department/member/workflow data was not available from the real API.");
  const validEmployeeEmails = new Set(employeeAccounts.map((item) => item.email));
  const departments = (groupsResponse.json?.groups ?? []).filter((item) => item.kind === "department");
  const candidates = (usersResponse.json?.users ?? []).filter((item) => item.is_active && item.role === "employee" && validEmployeeEmails.has(item.email));
  let choice = null;
  for (const employee of candidates) {
    const department = departments.find((group) => employee.departments?.some((item) => item.id === group.id));
    const account = employeeAccounts.find((item) => item.email === employee.email);
    if (department && account) { choice = { employee, department, account }; break; }
  }
  if (!choice) throw new Error("The protected synthetic Employee has no active department membership for the workflow trigger.");

  let workflow = null;
  for (const prior of (workflowResponse.json?.workflows ?? []).filter((item) => item.name.startsWith(workflowPrefix) && item.trigger_type === "service_request")) {
    const priorChoice = candidates.map((employee) => ({ employee, account: employeeAccounts.find((item) => item.email === employee.email), department: departments.find((group) => group.id === prior.department_group_id && employee.departments?.some((item) => item.id === group.id)) })).find((item) => item.account && item.department && item.employee.id === prior.assignee_user_id);
    if (priorChoice) { workflow = prior; choice = priorChoice; workflowName = prior.name; break; }
  }
  if (!workflow) {
    await setValue(".automation-form label:nth-of-type(1) input", workflowName);
    await setValue(".automation-form label:nth-of-type(2) select", choice.department.id);
    if (!await waitFor(`(()=>{const select=document.querySelector('.automation-form label:nth-of-type(4) select');return [...(select?.options??[])].some(option=>option.value===${JSON.stringify(choice.employee.id)})})()`)) throw new Error("The active department Employee did not appear in the workflow assignee list.");
    await setValue(".automation-form label:nth-of-type(3) select", "service_request");
    if (!await waitFor(`document.querySelector('.automation-wide.small.muted')?.textContent.includes('HR and requests without a department are excluded')`)) throw new Error("The UI did not explain HR/unscoped request exclusion and approval gating.");
    await setValue(".automation-form label:nth-of-type(4) select", choice.employee.id);
    await setValue(".automation-form label:nth-of-type(5) input", "Review submitted help request");
    await setValue(".automation-form textarea", "Review the synthetic request and document next steps.");
    const create = await evaluate(`(()=>{const button=[...document.querySelectorAll('.automation-form button')].find(item=>item.textContent.trim()==='Create workflow'&&!item.disabled);button?.click();return !!button})()`);
    if (!create || !await waitFor(`[...document.querySelectorAll('.automation-item h3')].some(item=>item.textContent===${JSON.stringify(workflowName)})`)) throw new Error("The current-source event workflow did not save through the Admin UI.");
    const saved = await request("GET", "/api/automation/workflows");
    workflow = saved.json?.workflows?.find((item) => item.name === workflowName);
  } else if (!await waitFor(`[...document.querySelectorAll('.automation-item h3')].some(item=>item.textContent===${JSON.stringify(workflowName)})`)) {
    throw new Error("The resumed synthetic event workflow did not render in the Admin UI.");
  }
  if (workflowResponse.status !== 200 || !workflow || workflow.trigger_type !== "service_request" || workflow.interval_minutes !== null || workflow.department_group_id !== choice.department.id || workflow.assignee_user_id !== choice.employee.id) throw new Error("The saved event trigger did not persist the selected department/assignee as a non-scheduled workflow.");
  const card = await evaluate(`(()=>{const article=[...document.querySelectorAll('.automation-item')].find(item=>item.querySelector('h3')?.textContent===${JSON.stringify(workflowName)});const button=[...(article?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()==='Validate flow');button?.click();return !!button})()`);
  if (!card || !await waitFor(`Boolean(document.querySelector('.automation-item .workflow-dry-run'))`)) throw new Error("The real API workflow dry-run did not render.");
  const dry = await request("POST", `/api/automation/workflows/${workflow.id}/dry-run`, {});
  const beforeRuns = await request("GET", "/api/automation/runs");
  const beforeTasks = await request("GET", "/api/automation/tasks");
  if (dry.status !== 200 || dry.json?.valid !== true || dry.json?.side_effects_performed !== false ||
      beforeRuns.json?.runs?.some((item) => item.workflow_id === workflow.id) || beforeTasks.json?.tasks?.some((item) => item.workflow_id === workflow.id)) {
    throw new Error("The real API dry-run was invalid or created a run/task side effect.");
  }
  console.log("LOCAL_F14_CURRENT_WORKFLOW_UI_AND_READ_ONLY_DRY_RUN=PASS");

  await signOut();
  await signIn(choice.account, "employee");
  const employeeMe = await request("GET", "/api/auth/me");
  const employeeDepartments = await request("GET", "/api/departments/mine");
  if (employeeMe.status !== 200 || !(employeeDepartments.json?.groups ?? []).some((group) => group.id === choice.department.id)) throw new Error("The selected synthetic Employee cannot access the workflow department.");
  await openRoute("/requests", "Help & requests");

  const subjectPrefix = `QA F14 ${stamp}`;
  const hr = await submitRequest("hr", choice.department.id, `${subjectPrefix} HR exclusion`);
  const unscoped = await submitRequest("access", null, `${subjectPrefix} unscoped exclusion`);
  if (hr.category !== "hr" || hr.department_group_id !== choice.department.id || unscoped.department_group_id !== null) throw new Error("The HR/unscoped synthetic exclusion requests were not persisted with the expected scope.");
  let runs = await request("GET", "/api/automation/runs");
  if (runs.status !== 200 || runs.json?.runs?.some((item) => item.workflow_id === workflow.id)) throw new Error("An excluded HR or unscoped request queued the event workflow.");
  console.log("LOCAL_F14_CURRENT_HR_AND_UNSCOPED_EXCLUSIONS=PASS");

  const matchingRequest = await submitRequest("access", choice.department.id, `${subjectPrefix} scoped department trigger`);
  if (matchingRequest.department_group_id !== choice.department.id) throw new Error("The positive department request was not persisted for the workflow scope.");
  runs = await request("GET", "/api/automation/runs");
  const matchingRuns = (runs.json?.runs ?? []).filter((item) => item.workflow_id === workflow.id && item.requested_by_user_id === employeeMe.json.id);
  if (runs.status !== 200 || matchingRuns.length !== 1 || matchingRuns[0].status !== "pending_approval" || matchingRuns[0].attempts !== 0 || matchingRuns[0].approval_decision !== null) throw new Error("A department event did not create exactly one approval-gated run.");
  const run = matchingRuns[0];
  const selfDecision = await request("POST", `/api/automation/runs/${run.id}/decision`, { approved: true, rationale: "Synthetic requester self-approval probe." });
  if (![403,404].includes(selfDecision.status)) throw new Error("The Employee requester could approve their own event-triggered workflow.");
  const tasksBeforeApproval = await request("GET", "/api/automation/tasks");
  if (tasksBeforeApproval.status !== 200 || tasksBeforeApproval.json?.tasks?.some((item) => item.run_id === run.id)) throw new Error("The workflow created a task before independent approval.");
  console.log("LOCAL_F14_CURRENT_DEPARTMENT_EVENT_PENDING_APPROVAL=PASS");
  console.log("LOCAL_F14_CURRENT_SELF_APPROVAL_AND_TASK_GATE=PASS");

  await signOut();
  await signIn(fixture.admin, "admin");
  await openRoute("/automation", "Workflows & tasks");
  if (!await waitFor(`[...document.querySelectorAll('.automation-item')].some(item=>item.querySelector('h3')?.textContent===${JSON.stringify(workflowName)}&&item.innerText.includes('pending approval')&&[...item.querySelectorAll('button')].some(button=>button.textContent.trim()==='Approve'))`)) {
    const apiRuns = await request("GET", "/api/automation/runs");
    const visible = apiRuns.json?.runs?.find((item) => item.id === run.id);
    if (apiRuns.status !== 200 || !visible?.can_approve || visible.status !== "pending_approval") throw new Error("The Admin could not see and approve the pending event run in current UI/API.");
  }
  const finalRuns = await request("GET", "/api/automation/runs");
  const finalRun = finalRuns.json?.runs?.find((item) => item.id === run.id);
  if (!finalRun?.can_approve || finalRun.status !== "pending_approval" || finalRun.approval_decision !== null) throw new Error("The pending workflow approval gate did not remain intact for the Admin reviewer.");
  const screenshot = await (async()=>{const result=await call("Page.captureScreenshot",{format:"png",captureBeyondViewport:true});const file=path.join(evidenceRoot,`pending-event-approval-${stamp}.png`);writeFileSync(file,Buffer.from(result.data,"base64"),{flag:"wx"});return file})();
  console.log("LOCAL_F14_CURRENT_ADMIN_APPROVAL_QUEUE=PASS");
  console.log("LOCAL_F14_CURRENT_BROWSER_TO_POSTGRESQL=PASS");
  console.log("LOCAL_F14_CURRENT_REVIEW_EVIDENCE=" + JSON.stringify({ screenshot }));
}

try { await run(); }
catch (error) { console.error(`LOCAL_F14_CURRENT_REVIEW_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`); process.exitCode=1; }
finally {
  try { socket?.close(); } catch {}
  if (browser?.exitCode === null) { browser.kill(); await Promise.race([new Promise((resolve)=>browser.once("exit",resolve)),delay(5_000)]); }
  try { rmSync(profile,{recursive:true,force:true}); } catch {}
}
