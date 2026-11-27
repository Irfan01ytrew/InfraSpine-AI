#!/usr/bin/env node
/** Resume C15 browser acceptance from the already completed synthetic workflow run. */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const repo = process.cwd();
const stackPath = path.resolve(process.env.C15_STACK_STATE ?? ".verification-local-review-20261029-c15-mywork-r3/stack-state.json");
const readJson = filepath => JSON.parse(readFileSync(filepath, "utf8").replace(/^\uFEFF/, ""));
const stack = readJson(stackPath);
const fixture = readJson(path.resolve(stack.credential_fixture));
const runtime = readJson(path.resolve(stack.runtime_state));
const evidenceDir = path.join(path.dirname(stackPath), "evidence");
const profile = path.join(path.dirname(stackPath), `browser-profile-c15-tail-${Date.now()}`);
if (!stack.frontend_url.startsWith("http://127.0.0.1:") || existsSync(profile)) throw new Error("C15 tail acceptance requires an unused isolated browser profile and loopback app.");
mkdirSync(profile, { recursive: false });
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find(item => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable.");

const pending = new Map();
let browser;
let socket;
let sequence = 0;
const observed = [];
const facts = {};
function call(method, params = {}, timeoutMs = 25_000) {
  const id = ++sequence;
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
async function waitFor(expression, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await delay(120);
  }
  return false;
}
async function setField(labelText, value) {
  const ok = await evaluate(`(() => {const l=[...document.querySelectorAll("label")].find(x=>x.querySelector(".field-label")?.textContent.trim()===${JSON.stringify(labelText)});const e=l?.querySelector("input,textarea");if(!e)return false;Object.getOwnPropertyDescriptor(e instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,"value").set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event("input",{bubbles:true}));e.dispatchEvent(new Event("change",{bubbles:true}));return true})()`);
  if (!ok) throw new Error(`C15 login field not found: ${labelText}`);
}
async function loginAs(person) {
  if (!await waitFor(`Boolean([...document.querySelectorAll("label")].find(label=>label.querySelector(".field-label")?.textContent.trim()==="Email"))`, 20_000)) throw new Error("C15 login form did not appear.");
  await setField("Email", fixture[person].email);
  await setField("Password", fixture[person].password);
  const clicked = await evaluate(`(() => {const b=[...document.querySelectorAll("button")].find(x=>x.textContent.trim()==="Sign in");if(!b)return false;b.click();return true})()`);
  if (!clicked || !await waitFor(`(async()=>{if(document.querySelector("form.login-form"))return false;try{const r=await fetch("/api/auth/me",{credentials:"same-origin",cache:"no-store"});if(!r.ok)return false;return (await r.json()).email===${JSON.stringify(fixture[person].email)}}catch{return false}})()`, 35_000)) {
    const page = await evaluate("document.body.innerText.slice(0,500)");
    throw new Error(`C15 ${person} account did not enter the app: ${page}`);
  }
}
async function logout() {
  const status = await evaluate(`(async()=>{const r=await fetch("/api/auth/logout",{method:"POST",credentials:"same-origin",headers:{"X-Requested-With":"C15-QA"}});return r.status})()`);
  if (status !== 204) throw new Error("C15 session logout failed.");
  await call("Page.reload", { ignoreCache: true });
  if (!await waitFor(`Boolean([...document.querySelectorAll("label")].find(label=>label.querySelector(".field-label")?.textContent.trim()==="Email"))`, 20_000)) throw new Error("C15 login screen did not return after session logout.");
}
async function request(pathname, method = "GET", body = undefined) {
  const expression = `(async()=>{const init={method:${JSON.stringify(method)},credentials:"same-origin",cache:"no-store",headers:{"Accept":"application/json","X-Requested-With":"C15-QA"}};${body === undefined ? "" : `init.headers["Content-Type"]="application/json";init.body=JSON.stringify(${JSON.stringify(body)});`}const r=await fetch(${JSON.stringify(pathname)},init);let data=null;try{data=await r.json()}catch{}return {status:r.status,data}})()`;
  const result = await evaluate(expression);
  observed.push({ method, path: pathname, status: result.status });
  return result;
}
function psql(query) {
  const psqlPath = "C:\\Program Files\\PostgreSQL\\18\\bin\\psql.exe";
  const url = `postgresql://postgres@127.0.0.1:${runtime.postgres_port}/${runtime.database}`;
  const result = spawnSync(psqlPath, [url, "-X", "-v", "ON_ERROR_STOP=1", "-A", "-t", "-F", "|", "-c", query], { encoding: "utf8", timeout: 12_000, windowsHide: true });
  if (result.error || result.status !== 0) throw new Error("C15 read-only PostgreSQL evidence query failed.");
  return result.stdout.trim();
}
async function screenshot(filename) {
  const result = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  const target = path.join(evidenceDir, filename);
  writeFileSync(target, Buffer.from(result.data, "base64"), { flag: "wx" });
  return target;
}

try {
  const env = { SYSTEMROOT: process.env.SYSTEMROOT, PATH: process.env.PATH, TEMP: process.env.TEMP, TMP: process.env.TMP, USERPROFILE: process.env.USERPROFILE, LOCALAPPDATA: process.env.LOCALAPPDATA };
  browser = spawn(browserPath, ["--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync", "--disable-background-networking", "--disable-component-update", "--disable-save-password-bubble", "--headless=new", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "--window-size=1365,1000", "about:blank"], { stdio: "ignore", windowsHide: true, env });
  const active = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(active) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(active)) throw new Error("Isolated Chromium did not start.");
  const port = Number(readFileSync(active, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline) {
    try { page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(item => item.type === "page" && item.webSocketDebuggerUrl); } catch {}
    if (!page) await delay(100);
  }
  if (!page) throw new Error("Chromium browser target did not appear.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", () => reject(new Error("Chromium debugger connection failed.")), { once: true }); });
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const item = pending.get(message.id); clearTimeout(item.timer); pending.delete(message.id);
    if (message.error) item.reject(new Error(`Browser command failed: ${message.error.message}`)); else item.resolve(message.result);
  });
  await call("Page.enable"); await call("Runtime.enable");
  await call("Emulation.setDeviceMetricsOverride", { width: 1365, height: 1000, deviceScaleFactor: 1, mobile: false });
  await call("Page.navigate", { url: stack.my_work_url });
  await loginAs("employee");
  if (!await waitFor(`Boolean([...document.querySelectorAll("h2")].find(h=>h.textContent.includes("Personal assignments")))`, 25_000)) throw new Error("Employee My Work assignments section did not render.");
  const tasksResponse = await request("/api/personal-tasks");
  const task = tasksResponse.data?.tasks?.find(item => item.title === "Review approved member access");
  if (tasksResponse.status !== 200 || !task || task.status !== "completed" || !await waitFor("document.body.innerText.includes('Review approved member access')")) throw new Error("The completed synthetic assignment was not visible to its assignee.");
  const workflowResult = await request("/api/automation/workflows");
  const workflow = workflowResult.data?.workflows?.find(item => item.name === "Approved My Work assignment QA");
  const runResult = await request("/api/automation/runs");
  const run = runResult.data?.runs?.find(item => workflow && item.workflow_id === workflow.id);
  if (!workflow || workflow.action_type !== "assign_personal_task" || !run || run.status !== "completed") throw new Error("The employee could not read the completed approved workflow run.");
  const runId = run.id;
  facts.assignee_saw_completed_task_in_my_work = true;
  facts.assignee_task_status = task.status;
  facts.task_id = task.id;

  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await delay(250);
  const mobile = await evaluate("({viewport:innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth})");
  if (mobile.document > mobile.viewport || mobile.body > mobile.viewport) throw new Error("My Work overflows at 390px.");
  facts.mobile = mobile;
  const mobileScreenshot = await screenshot(`c15-mywork-access-tail-${Date.now()}.png`);

  const runRow = psql(`SELECT idempotency_key || '|' || status || '|' || attempts FROM automation_runs WHERE id = '${runId}'`);
  const pieces = runRow.split("|");
  if (pieces.length !== 3 || pieces[1] !== "completed" || pieces[2] !== "1") throw new Error("Current PostgreSQL run evidence did not match the completed My Work item.");
  const idempotencyKey = pieces[0].split(":").at(-1);
  const duplicate = await request(`/api/automation/workflows/${workflow.id}/runs`, "POST", { idempotency_key: idempotencyKey });
  if (duplicate.status !== 202 || duplicate.data.id !== runId) throw new Error("A repeat browser request created a second workflow run instead of reusing the original.");
  const selfApproval = await request(`/api/automation/runs/${runId}/decision`, "POST", { approved: true, rationale: "Self-approval remains prohibited." });
  if (selfApproval.status !== 403) throw new Error("The requesting employee was not denied an approval attempt.");
  facts.duplicate_request_reused_completed_run = true;
  facts.requester_approval_denied = true;

  await logout();
  await loginAs("outsider");
  await call("Page.navigate", { url: stack.my_work_url });
  if (!await waitFor(`Boolean([...document.querySelectorAll("h2")].find(h=>h.textContent.includes("Personal assignments")))`, 25_000)) throw new Error("Unrelated user's My Work page did not render.");
  const outsiderTasks = await request("/api/personal-tasks");
  const outsiderDetail = await request(`/api/personal-tasks/${task.id}`);
  if (outsiderTasks.status !== 200 || outsiderTasks.data.tasks.some(item=>item.id===task.id) || outsiderDetail.status !== 404) throw new Error("A user outside the assigned department could access the task.");
  facts.outsider_hidden_and_404 = true;

  await logout();
  await loginAs("manager");
  const revoke = await request(`/api/departments/${fixture.ids.department}/members/${fixture.ids.employee}`, "DELETE");
  if (revoke.status !== 204) throw new Error("Department manager membership revocation failed.");
  facts.manager_revoked_membership = true;
  await logout();
  await loginAs("employee");
  await call("Page.navigate", { url: stack.my_work_url });
  if (!await waitFor(`Boolean([...document.querySelectorAll("h2")].find(h=>h.textContent.includes("Personal assignments")))`, 25_000)) throw new Error("Revoked employee My Work page did not render.");
  const revokedList = await request("/api/personal-tasks");
  const revokedDetail = await request(`/api/personal-tasks/${task.id}`);
  if (revokedList.status !== 200 || revokedList.data.tasks.some(item=>item.id===task.id) || revokedDetail.status !== 404) throw new Error("Membership revocation did not immediately hide the assignment.");
  facts.revocation_immediately_hid_task = true;

  const runEvidence = psql(`SELECT r.requester_user_id::text || '|' || COALESCE(a.approver_user_id::text,'') || '|' || a.decision || '|' || r.status || '|' || r.attempts || '|' || count(p.id) FROM automation_runs r LEFT JOIN automation_approvals a ON a.run_id=r.id LEFT JOIN personal_tasks p ON p.automation_run_id=r.id WHERE r.id='${runId}' GROUP BY r.id,a.approver_user_id,a.decision`);
  const linked = runEvidence.split("|");
  if (linked.length !== 6 || linked[2] !== "approved" || linked[3] !== "completed" || linked[4] !== "1" || linked[5] !== "1") throw new Error("PostgreSQL approval, run and unique task-link readback did not agree.");
  facts.postgres_approval_run_task_link = { requester_user_id: linked[0], approver_user_id: linked[1], approval: linked[2], run_status: linked[3], worker_attempts: Number(linked[4]), linked_personal_tasks: Number(linked[5]) };

  const evidence = {
    accepted: true,
    source: "current dirty worktree, fresh API/Vite processes, real browser sessions and the C15 current-source worker run retained in PostgreSQL",
    resumed_after_initial_live_run: true,
    prior_browser_run_reached_outsider_stage_after_workflow_and_employee_task_checks: true,
    frontend_url: stack.frontend_url,
    my_work_url: stack.my_work_url,
    api_health_url: stack.api_health_url,
    database: runtime.database,
    migration: stack.migration,
    source_hashes: stack.source_hashes,
    facts,
    tail_api_events: observed,
    screenshot: path.basename(mobileScreenshot),
    captured_at: new Date().toISOString(),
  };
  const evidencePath = path.join(evidenceDir, "c15-personal-task-current-source-browser.json");
  writeFileSync(evidencePath, `${JSON.stringify(evidence, null, 2)}\n`, { flag: "wx" });
  console.log("C15_BROWSER_ACCEPTANCE=PASS");
  console.log("C15_MOBILE_WIDTHS=" + JSON.stringify(mobile));
  console.log("C15_FACTS=" + Object.keys(facts).filter(key=>!['mobile','task_id','assignee_task_status','postgres_approval_run_task_link'].includes(key)).join(","));
} finally {
  if (socket) socket.close();
  if (browser && browser.exitCode === null) browser.kill();
}
