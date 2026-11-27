#!/usr/bin/env node
/** Current-source Chromium acceptance for approved workflow tasks in the My Work inbox. */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const repo = process.cwd();
const stackPath = path.resolve(process.env.C15_STACK_STATE ?? ".verification-local-review-20261029-c15-mywork-r1/stack-state.json");
const readJson = filepath => JSON.parse(readFileSync(filepath, "utf8").replace(/^\uFEFF/, ""));
const stack = readJson(stackPath);
const fixturePath = path.resolve(stack.credential_fixture);
const runtimePath = path.resolve(stack.runtime_state);
const fixture = readJson(fixturePath);
const runtime = readJson(runtimePath);
const base = new URL(stack.frontend_url);
const evidenceDir = path.join(path.dirname(stackPath), "evidence");
const profile = path.join(path.dirname(stackPath), `browser-profile-c15-${Date.now()}`);
if (!base.href.startsWith("http://127.0.0.1:") || !base.hash.includes("automation")) throw new Error("C15 browser acceptance requires the isolated loopback workflow app.");
if (existsSync(profile)) throw new Error("The C15 browser profile already exists; preserving it.");
mkdirSync(profile, { recursive: false });

const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find(candidate => candidate && existsSync(candidate));
if (!browserPath) throw new Error("Chrome or Edge is unavailable.");

const observed = [];
const facts = {};
let browser;
let socket;
let commandSequence = 0;
const pending = new Map();
function call(method, params = {}, timeoutMs = 25_000) {
  const id = ++commandSequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Chromium timed out during ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 25_000) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("A C15 browser action raised an exception.");
  return result.result?.value;
}
async function waitFor(expression, timeoutMs = 35_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await delay(120);
  }
  return false;
}
async function go(url) {
  await call("Page.navigate", { url });
  await delay(250);
}
async function clickText(text, { contains = false, timeoutMs = 20_000 } = {}) {
  const match = `(() => { const nodes=[...document.querySelectorAll("button")]; const b=nodes.find(x => ${contains ? `x.textContent.trim().includes(${JSON.stringify(text)})` : `x.textContent.trim() === ${JSON.stringify(text)}`}); if (!b || b.disabled) return false; b.click(); return true; })()`;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(match)) return;
    await delay(100);
  }
  throw new Error(`C15 UI button was unavailable: ${text}`);
}
async function setField(labelText, value) {
  const expression = `(async() => {
    const label=[...document.querySelectorAll("label")].find(item => item.querySelector(".field-label")?.textContent.trim() === ${JSON.stringify(labelText)});
    const element=label?.querySelector("input,textarea,select"); if(!element) return false;
    const prototype=element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype,"value").set.call(element,${JSON.stringify(value)});
    element.dispatchEvent(new Event("input",{bubbles:true})); element.dispatchEvent(new Event("change",{bubbles:true})); await new Promise(r=>setTimeout(r,160)); return true;
  })()`;
  if (!await evaluate(expression)) {
    const page = await evaluate("({title:document.title, text:document.body.innerText.slice(0,1200)})");
    throw new Error(`C15 form field was unavailable: ${labelText}; visible page: ${JSON.stringify(page)}`);
  }
}
async function waitForLogin() {
  if (!await waitFor(`Boolean([...document.querySelectorAll("label")].find(label => label.querySelector(".field-label")?.textContent.trim() === "Email"))`, 25_000)) {
    const page = await evaluate("({url:location.href,text:document.body.innerText.slice(0,600)})");
    throw new Error(`The synthetic login form did not appear: ${JSON.stringify(page)}`);
  }
}
async function loginAs(person) {
  await waitForLogin();
  await setField("Email", fixture[person].email);
  await setField("Password", fixture[person].password);
  await clickText("Sign in");
  const rendered = await waitFor(`(async()=>{if(document.querySelector("form.login-form"))return false;try{const r=await fetch("/api/auth/me",{credentials:"same-origin",cache:"no-store"});if(!r.ok)return false;return (await r.json()).email===${JSON.stringify(fixture[person].email)}}catch{return false}})()`, 35_000);
  if (!rendered) {
    const page = await evaluate("({url:location.href,text:document.body.innerText.slice(0,700)})");
    throw new Error(`The ${person} authenticated app shell did not render after login: ${JSON.stringify(page)}`);
  }
}
async function logout() {
  const result = await evaluate(`(async()=>{const r=await fetch("/api/auth/logout",{method:"POST",credentials:"same-origin",headers:{"X-Requested-With":"C15-QA"}}); return r.status})()`);
  if (result !== 204) throw new Error("The synthetic account session did not log out cleanly.");
  await call("Page.reload", { ignoreCache: true });
  await waitForLogin();
}
async function browserRequest(pathname, method = "GET", body = undefined) {
  return await evaluate(`(async()=>{const r=await fetch(${JSON.stringify(pathname)},{method:${JSON.stringify(method)},credentials:"same-origin",headers:{"Accept":"application/json","X-Requested-With":"C15-QA",...( ${body === undefined ? "false" : "true"} ? {"Content-Type":"application/json"}:{} )},${body === undefined ? "" : `body:JSON.stringify(${JSON.stringify(body)}),`}cache:"no-store"});let data=null;try{data=await r.json()}catch{}return {status:r.status,data}})()`);
}
function captureScreenshot(name) {
  return call("Page.captureScreenshot", { format: "png", captureBeyondViewport: true }).then(result => {
    const filepath = path.join(evidenceDir, name);
    writeFileSync(filepath, Buffer.from(result.data, "base64"));
    return filepath;
  });
}

const captureScript = `(() => {
  const originalFetch=window.fetch.bind(window);
  window.__c15Observed=[];
  window.fetch=async(input,init={})=>{
    const url=new URL(typeof input==="string"?input:input.url,location.href);
    const method=String(init.method||(input instanceof Request?input.method:"GET")).toUpperCase();
    const body=typeof init.body==="string"?(()=>{try{return JSON.parse(init.body)}catch{return null}})():null;
    const response=await originalFetch(input,init);
    const record={method,path:url.pathname,status:response.status};
    if(url.pathname.includes("/workflows/")&&url.pathname.endsWith("/runs")&&method==="POST"&&body?.idempotency_key) record.idempotency_key=body.idempotency_key;
    try { const clone=response.clone(); const payload=await clone.json(); if(url.pathname.endsWith("/runs")&&payload?.id) record.run_id=payload.id; if(url.pathname.includes("/personal-tasks")&&payload?.id) record.task_id=payload.id; } catch {}
    window.__c15Observed.push(record); return response;
  };
})();`;

try {
  const childEnv = {
    SYSTEMROOT: process.env.SYSTEMROOT, PATH: process.env.PATH, TEMP: process.env.TEMP,
    TMP: process.env.TMP, USERPROFILE: process.env.USERPROFILE, LOCALAPPDATA: process.env.LOCALAPPDATA,
  };
  browser = spawn(browserPath, [
    "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
    "--disable-background-networking", "--disable-component-update", "--disable-save-password-bubble",
    "--headless=new", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
    `--user-data-dir=${profile}`, "--window-size=1365,1000", "about:blank",
  ], { stdio: "ignore", windowsHide: true, env: childEnv });
  const activePortFile = path.join(profile, "DevToolsActivePort");
  const browserDeadline = Date.now() + 30_000;
  while (!existsSync(activePortFile) && Date.now() < browserDeadline && browser.exitCode === null) await delay(120);
  if (!existsSync(activePortFile)) throw new Error("The isolated Chromium profile did not start.");
  const debuggerPort = Number(readFileSync(activePortFile, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < browserDeadline) {
    try { page = (await (await fetch(`http://127.0.0.1:${debuggerPort}/json/list`)).json()).find(item => item.type === "page" && item.webSocketDebuggerUrl); } catch {}
    if (!page) await delay(120);
  }
  if (!page) throw new Error("The isolated Chromium page was unavailable.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("The local Chromium debugger socket failed.")), { once: true });
  });
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const item = pending.get(message.id); clearTimeout(item.timer); pending.delete(message.id);
    if (message.error) item.reject(new Error(`Browser command failed: ${message.error.message}`)); else item.resolve(message.result);
  });
  await call("Page.enable"); await call("Runtime.enable");
  await call("Page.addScriptToEvaluateOnNewDocument", { source: captureScript });
  await call("Emulation.setDeviceMetricsOverride", { width: 1365, height: 1000, deviceScaleFactor: 1, mobile: false });
  await go(base.href);
  await loginAs("manager");
  if (!await waitFor(`Boolean([...document.querySelectorAll("label")].find(label => label.querySelector(".field-label")?.textContent.trim() === "Workflow name"))`, 20_000)) {
    const page = await evaluate("document.body.innerText.slice(0,1000)");
    throw new Error(`The manager workflow form did not finish loading: ${page}`);
  }

  await setField("Workflow name", "Approved My Work assignment QA");
  await setField("Department", fixture.ids.department);
  if (!await waitFor(`Boolean([...document.querySelectorAll("select option")].find(option => option.value === ${JSON.stringify(fixture.ids.employee)}))`, 20_000)) {
    throw new Error("The active employee was missing from the manager's department action form.");
  }
  await setField("Trigger", "manual");
  await setField("Approved action", "assign_personal_task");
  await setField("Task assignee", fixture.ids.employee);
  await setField("My Work task title", "Review approved member access");
  await setField("Task instructions", "Compare the synthetic roster with the approved register.");
  await setField("Due in days (optional)", "3");
  await clickText("Create workflow");
  if (!await waitFor("window.__c15Observed.some(item => item.method === 'POST' && item.path === '/api/automation/workflows' && item.status === 201)", 25_000)) {
    throw new Error("The real API did not save the personal-task workflow from the current-source form.");
  }
  const workflowResult = await browserRequest("/api/automation/workflows");
  const workflow = workflowResult.data?.workflows?.find(item => item.name === "Approved My Work assignment QA");
  if (workflowResult.status !== 200 || !workflow || workflow.action_type !== "assign_personal_task") throw new Error("The persisted workflow action differed from its editor values.");
  facts.workflow_saved = true;
  facts.workflow_action = workflow.action_type;
  facts.workflow_id = workflow.id;
  facts.manager_editor_current_source = true;

  await logout();
  await loginAs("employee");
  const workflowCard = `document.body.innerText.includes("Approved My Work assignment QA")`;
  if (!await waitFor(workflowCard, 20_000)) throw new Error("The assigned employee did not see the department workflow request.");
  await clickText("Request run", { timeoutMs: 20_000 });
  if (!await waitFor(`window.__c15Observed.some(item => item.method === 'POST' && item.path === ${JSON.stringify(`/api/automation/workflows/${workflow.id}/runs`)} && item.status === 202)`, 25_000)) {
    throw new Error("The employee run request did not reach the current API.");
  }
  const observedRuns = await evaluate("window.__c15Observed.filter(item=>item.method==='POST'&&item.path.endsWith('/runs')&&item.idempotency_key&&item.run_id)");
  const runEvent = observedRuns?.find(item => item.path === `/api/automation/workflows/${workflow.id}/runs`);
  if (!runEvent) throw new Error("The employee run request did not return its durable idempotency/run identifiers.");
  const runId = runEvent.run_id;
  const runBefore = await browserRequest("/api/automation/runs");
  if (runBefore.status !== 200 || runBefore.data.runs.find(item => item.id === runId)?.status !== "pending_approval") throw new Error("The workflow did not wait for independent approval.");
  const beforeTasks = await browserRequest("/api/personal-tasks");
  if (beforeTasks.status !== 200 || beforeTasks.data.tasks.length !== 0) throw new Error("The task appeared before approval.");
  const duplicate = await browserRequest(`/api/automation/workflows/${workflow.id}/runs`, "POST", { idempotency_key: runEvent.idempotency_key });
  if (duplicate.status !== 202 || duplicate.data.id !== runId) throw new Error("Repeating the same browser request did not reuse its pending run.");
  const selfApproval = await browserRequest(`/api/automation/runs/${runId}/decision`, "POST", { approved: true, rationale: "Self-approval must be denied." });
  if (selfApproval.status !== 403) throw new Error("The requesting employee was able to approve the workflow run.");
  facts.request_waited_for_approval = true;
  facts.duplicate_request_reused_run = true;
  facts.requester_self_approval_denied = true;
  facts.task_count_before_approval = beforeTasks.data.tasks.length;

  await logout();
  await loginAs("manager");
  if (!await waitFor(`Boolean([...document.querySelectorAll("textarea")].find(area => area.closest("label")?.querySelector(".field-label")?.textContent.trim() === "Decision rationale"))`, 25_000)) {
    throw new Error("The independent manager approval control did not appear.");
  }
  await setField("Decision rationale", "Approved the synthetic My Work follow-up.");
  await clickText("Approve");
  if (!await waitFor(`window.__c15Observed.some(item => item.method === 'POST' && item.path === ${JSON.stringify(`/api/automation/runs/${runId}/decision`)} && item.status === 200)`, 25_000)) {
    throw new Error("The independent approval was not saved by the current API.");
  }
  const queued = await browserRequest("/api/automation/runs");
  if (queued.data.runs.find(item => item.id === runId)?.status !== "queued") throw new Error("The approved run was not queued for worker execution.");
  facts.independent_manager_approval = true;

  const python = path.join(repo, ".venv-verify-312", "Scripts", "python.exe");
  const workerEnv = {
    ...childEnv,
    DATABASE_URL: `postgresql+asyncpg://postgres@127.0.0.1:${runtime.postgres_port}/${runtime.database}`,
    APP_ENV: "testing", MASTER_ENCRYPTION_KEY: runtime.master_key, MASTER_ENCRYPTION_KEY_ID: "c15qa",
    RAG_DATA_DIR: path.join(path.dirname(stackPath), "data"), RAG_INGESTION_MODE: "worker",
    RAG_WORKER_ENABLED: "false", AGENT_QUEUE_WORKER_ENABLED: "false", LOG_LEVEL: "INFO",
    LOG_DIR: path.join(path.dirname(stackPath), "logs", "worker"), PYTHONPATH: repo,
    TEMP: path.join(path.dirname(stackPath), "tmp"), TMP: path.join(path.dirname(stackPath), "tmp"),
  };
  mkdirSync(workerEnv.LOG_DIR, { recursive: true });
  const worker = spawnSync(python, [path.join(repo, "scripts", "process_c15_workflow_once.py")], {
    cwd: path.join(path.dirname(stackPath), "api-cwd"), env: workerEnv, encoding: "utf8", timeout: 60_000, windowsHide: true,
  });
  if (worker.error || worker.status !== 0 || !worker.stdout.includes("C15_WORKFLOW_ITEM_PROCESSED=true")) {
    throw new Error("The current-source isolated workflow worker did not complete the approved item.");
  }
  facts.worker_current_source = true;

  const completed = await browserRequest("/api/automation/runs");
  if (completed.data.runs.find(item => item.id === runId)?.status !== "completed") throw new Error("The worker did not persist completed run history.");
  await logout();
  await loginAs("employee");
  await go(base.origin + "/admin/#/my-work");
  if (!await waitFor(`Boolean([...document.querySelectorAll("h2")].find(heading => heading.textContent.includes("Personal assignments")))`, 30_000)) {
    throw new Error("The employee My Work page did not load through the current-source UI.");
  }
  if (!await waitFor("document.body.innerText.includes('Review approved member access')", 25_000)) throw new Error("The approved personal assignment did not appear in My Work.");
  const employeeTasks = await browserRequest("/api/personal-tasks");
  const task = employeeTasks.data?.tasks?.find(item => item.title === "Review approved member access");
  if (employeeTasks.status !== 200 || !task || task.automation_run_id && task.automation_run_id !== runId) throw new Error("The employee My Work API did not expose the exact approved task.");
  const taskDetail = await browserRequest(`/api/personal-tasks/${task.id}`);
  if (taskDetail.status !== 200) throw new Error("The assigned employee could not open the approved task.");
  facts.employee_task_visible_in_my_work = true;
  facts.personal_task_id = task.id;

  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await delay(350);
  const mobile = await evaluate("({viewport:innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth})");
  if (mobile.document > mobile.viewport || mobile.body > mobile.viewport) throw new Error("The My Work task view overflows at 390px.");
  facts.mobile = mobile;
  facts.my_work_rendered_personal_task = true;
  const screenshotPath = await captureScreenshot("c15-approved-my-work-mobile.png");

  const taskItem = `(() => { const li=[...document.querySelectorAll("li.my-work-item")].find(item=>item.innerText.includes("Review approved member access")); const button=[...(li?.querySelectorAll("button")??[])].find(item=>item.textContent.trim()==="Complete"); if(!button||button.disabled)return false; button.click(); return true; })()`;
  if (!await evaluate(taskItem)) throw new Error("The employee could not complete the new My Work task from its rendered row.");
  if (!await waitFor(`window.__c15Observed.some(item=>item.method==="POST"&&item.path===${JSON.stringify(`/api/personal-tasks/${task.id}/complete`)}&&item.status===200)`, 20_000)) throw new Error("Completing the task did not reach the current API.");
  const completedTask = await browserRequest(`/api/personal-tasks/${task.id}`);
  if (completedTask.status !== 200 || completedTask.data.status !== "completed") throw new Error("The completed My Work status was not durable.");
  facts.employee_completed_task = true;

  await logout();
  await loginAs("outsider");
  await go(base.origin + "/admin/#/my-work");
  if (!await waitFor(`Boolean([...document.querySelectorAll("h2")].find(heading=>heading.textContent.includes("Personal assignments")))`, 25_000)) throw new Error("The unrelated employee My Work page did not load.");
  const outsiderList = await browserRequest("/api/personal-tasks");
  const outsiderDetail = await browserRequest(`/api/personal-tasks/${task.id}`);
  if (outsiderList.status !== 200 || outsiderList.data.tasks.some(item => item.id === task.id) || outsiderDetail.status !== 404) throw new Error("A user outside the department could access the assignment.");
  facts.outsider_denied = true;

  await logout();
  await loginAs("manager");
  const revoke = await browserRequest(`/api/departments/${fixture.ids.department}/members/${fixture.ids.employee}`, "DELETE");
  if (revoke.status !== 204) throw new Error("The scoped manager could not revoke the synthetic membership.");
  facts.membership_revoked = true;
  await logout();
  await loginAs("employee");
  await go(base.origin + "/admin/#/my-work");
  await waitFor(`Boolean([...document.querySelectorAll("h2")].find(heading=>heading.textContent.includes("Personal assignments")))`, 25_000);
  const revokedList = await browserRequest("/api/personal-tasks");
  const revokedDetail = await browserRequest(`/api/personal-tasks/${task.id}`);
  if (revokedList.status !== 200 || revokedList.data.tasks.some(item => item.id === task.id) || revokedDetail.status !== 404) throw new Error("Revocation did not immediately hide the department assignment.");
  facts.revocation_hid_task = true;

  observed.push(...await evaluate("window.__c15Observed"));
  const evidence = {
    accepted: true,
    source: "current dirty worktree, fresh Vite/API process, isolated PostgreSQL database, real application login/session and current automation worker service",
    frontend_url: stack.frontend_url,
    my_work_url: stack.my_work_url,
    api_health_url: stack.api_health_url,
    database: runtime.database,
    migration: "head (0065_workflow_personal_task_action)",
    source_hashes: stack.source_hashes,
    facts,
    api_events: observed,
    screenshot: path.basename(screenshotPath),
    captured_at: new Date().toISOString(),
  };
  writeFileSync(path.join(evidenceDir, "c15-personal-task-current-source-browser.json"), `${JSON.stringify(evidence, null, 2)}\n`, { flag: "wx" });
  console.log("C15_BROWSER_ACCEPTANCE=PASS");
  console.log("C15_FACTS=" + Object.keys(facts).filter(key => facts[key] === true).join(","));
  console.log("C15_MOBILE_WIDTHS=" + JSON.stringify(mobile));
} finally {
  if (socket) socket.close();
  if (browser && browser.exitCode === null) browser.kill();
}
