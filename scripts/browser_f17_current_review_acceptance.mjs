#!/usr/bin/env node
/** Authenticated current-source F17 policy impact acceptance on isolated loopback QA. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_F17_URL ?? "http://127.0.0.1:5175/admin/");
const fixturePath = process.env.INFRASPINE_F17_FIXTURE;
const pdfPath = process.env.INFRASPINE_F17_PDF;
const root = process.env.INFRASPINE_F17_ROOT;
const statePath = process.env.INFRASPINE_F17_STATE;
if (base.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(base.hostname) || !fixturePath || !pdfPath || !root || !statePath) {
  throw new Error("F17 acceptance requires the protected synthetic loopback review configuration.");
}
const qa = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const employees = Array.isArray(qa.employees) ? qa.employees : [];
const managers = Array.isArray(qa.managers) ? qa.managers : [];
if (!qa.admin?.email || !qa.admin?.password || employees.length < 2 || !managers[0]?.email || !existsSync(pdfPath)) {
  throw new Error("The synthetic F17 role/source fixture is incomplete.");
}
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable.");

const statement = "F17 QA: Access changes require review within 14 days.";
const revisedStatement = "F17 QA: Access changes require review within 7 days.";
const processName = "F17 QA Access Review (synthetic)";
const manager = managers[0];
const firstEmployee = employees[0];
const secondEmployee = employees[1];
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-f17-current-"));
const evidence = path.join(root, "f17-current-review-evidence");
mkdirSync(evidence, { recursive: true });
let browser;
let socket;
let sequence = 0;
const pending = new Map();

function cdp(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error("Browser acceptance timed out at " + method));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 20_000) {
  const result = await cdp("Runtime.evaluate", {
    expression, awaitPromise: true, returnByValue: true, userGesture: true,
  }, timeoutMs);
  if (result.exceptionDetails) throw new Error("An F17 browser action failed.");
  return result.result?.value;
}
async function waitFor(expression, timeoutMs = 30_000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    if (await evaluate(expression)) return true;
    await delay(200);
  }
  return false;
}
async function api(method, route, body) {
  const bodyExpression = body === undefined ? "undefined" : JSON.stringify(body);
  const expression = "(async()=>{const o={method:" + JSON.stringify(method) +
    ",credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'XMLHttpRequest'}};" +
    "const b=" + bodyExpression + ";if(b!==undefined){o.headers['Content-Type']='application/json';o.body=JSON.stringify(b)}" +
    "const r=await fetch(" + JSON.stringify(route) + ",o);let j=null;try{j=await r.json()}catch{}" +
    "return{status:r.status,json:j}})()";
  return evaluate(expression);
}
function requireStatus(result, expected, label) {
  if (result.status !== expected) throw new Error(label + " returned HTTP " + result.status);
}
async function signIn(account, role) {
  const started = await evaluate("(()=>{const e=document.querySelector('form.login-form input[name=\"email\"]');" +
    "const p=document.querySelector('form.login-form input[name=\"password\"]');" +
    "const f=document.querySelector('form.login-form');if(!e||!p||!f)return false;" +
    "for(const [x,v] of [[e," + JSON.stringify(account.email) + "],[p," + JSON.stringify(account.password) + "]]){" +
    "Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(x,v);" +
    "x.dispatchEvent(new Event('input',{bubbles:true}));x.dispatchEvent(new Event('change',{bubbles:true}))}" +
    "f.requestSubmit();return true})()");
  if (!started) throw new Error("The synthetic login form did not render.");
  const authenticated = await waitFor("(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});" +
    "return r.ok&&(await r.json()).role===" + JSON.stringify(role) + "})()", 40_000);
  if (!authenticated) throw new Error("Synthetic " + role + " authentication failed.");
}
async function signOut() {
  const clicked = await evaluate("(()=>{const b=document.querySelector('button[aria-label=\"Sign out\"]');" +
    "b?.click();return Boolean(b)})()");
  if (!clicked || !await waitFor("Boolean(document.querySelector('form.login-form'))", 20_000)) {
    throw new Error("Synthetic browser sign-out failed.");
  }
}
async function openPage(hash, title) {
  await evaluate("window.location.hash=" + JSON.stringify(hash));
  if (!await waitFor("Boolean([...document.querySelectorAll('h1')].some(x=>x.textContent.trim()===" +
    JSON.stringify(title) + "))", 30_000)) throw new Error("The " + title + " page did not render.");
  await waitFor("!document.querySelector('.loading')", 10_000);
}
async function setField(rootExpression, labelText, value) {
  const expression = "(()=>{const root=" + rootExpression + ";if(!root)return false;" +
    "const l=[...root.querySelectorAll('label.field')].find(x=>x.querySelector('.field-label')?.textContent.trim()===" +
    JSON.stringify(labelText) + ");const e=l?.querySelector('input,select,textarea');if(!e)return false;" +
    "const p=e instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:" +
    "e instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;" +
    "Object.getOwnPropertyDescriptor(p,'value').set.call(e," + JSON.stringify(value) + ");" +
    "e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true})()";
  if (!await evaluate(expression)) throw new Error("A required F17 field was missing: " + labelText);
}
function processCardExpression() {
  return "[...document.querySelectorAll('.approval-card')].find(x=>x.querySelector('h3')?.textContent.trim()==" +
    JSON.stringify(processName) + ")";
}
function exceptionCardExpression() {
  return "[...document.querySelectorAll('.approval-card')].find(x=>x.innerText.includes(" +
    JSON.stringify(processName) + ")&&[...x.querySelectorAll('button')].some(b=>/^(Acknowledge|Resolve action|Reassign|Load assignees for reassignment)$/.test(b.textContent.trim())))";
}
async function clickInProcess(buttonText) {
  return evaluate("(()=>{const a=" + processCardExpression() + ";const b=[...(a?.querySelectorAll('button')??[])]" +
    ".find(x=>x.textContent.trim()===" + JSON.stringify(buttonText) + ");b?.click();return Boolean(b)})()");
}
async function clickInException(buttonText) {
  return evaluate("(()=>{const a=" + exceptionCardExpression() + ";const b=[...(a?.querySelectorAll('button')??[])]" +
    ".find(x=>x.textContent.trim()===" + JSON.stringify(buttonText) + ");b?.click();return Boolean(b)})()");
}
async function setInException(labelText, value) {
  await setField(exceptionCardExpression(), labelText, value);
}
async function uploadSource() {
  const filename = path.basename(pdfPath);
  let result = await api("GET", "/api/rag/documents");
  requireStatus(result, 200, "Document list");
  let document = (result.json?.documents ?? []).find(item => item.filename === filename);
  if (document) {
    for (let index = 0; index < 240; index++) {
      if (document.status === "ready") break;
      if (document.status === "failed") throw new Error("The existing synthetic F17 source failed processing.");
      await delay(500);
      result = await api("GET", "/api/rag/documents/" + document.id);
      document = result.json;
    }
    if (document?.status === "ready" && Number(document.chunk_count) > 0) return document;
    throw new Error("The resumed synthetic F17 source is not indexed.");
  }
  const tree = await cdp("DOM.getDocument", { depth: -1, pierce: true });
  const input = await cdp("DOM.querySelector", {
    nodeId: tree.root.nodeId, selector: 'input[type="file"][multiple][accept*=".pdf"]',
  });
  if (!input.nodeId) throw new Error("The Knowledge PDF upload control is unavailable.");
  await cdp("DOM.setFileInputFiles", { files: [pdfPath], nodeId: input.nodeId });
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    result = await api("GET", "/api/rag/documents");
    document = (result.json?.documents ?? []).find(item => item.filename === filename);
    if (document) break;
    await delay(250);
  }
  if (!document) throw new Error("The synthetic F17 source did not create a document row.");
  for (let index = 0; index < 240; index++) {
    if (document.status === "ready" || document.status === "failed") break;
    await delay(500);
    result = await api("GET", "/api/rag/documents/" + document.id);
    document = result.json;
  }
  if (document.status !== "ready" || Number(document.chunk_count) < 1) {
    throw new Error("The local worker did not index the synthetic policy source.");
  }
  return document;
}
async function grant(email, permission) {
  const current = await api("GET", "/api/admin/access/grants");
  requireStatus(current, 200, "Admin permission grants");
  if ((current.json?.grants ?? []).some(item =>
    item.email.toLowerCase() === email.toLowerCase() &&
    item.permission === permission && item.category === "policy" && !item.revoked_at)) return;
  const created = await api("POST", "/api/admin/access/grants", {
    email, permission, category: "policy", scope_group_id: null,
  });
  requireStatus(created, 201, "Synthetic " + permission + " grant");
}
async function policySetup(documentId) {
  await grant(manager.email, "knowledge.review");
  await grant(secondEmployee.email, "knowledge.publish");
  const list = await api("GET", "/api/knowledge");
  requireStatus(list, 200, "Knowledge list");
  let entry = (list.json?.items ?? []).find(item =>
    item.current?.statement === statement || item.current?.statement === revisedStatement ||
    item.current?.statement?.startsWith("F17 QA: Access changes require review within "));
  if (!entry) {
    const created = await api("POST", "/api/knowledge", {
      statement, category: "policy", visibility: "shared", scope_group_id: null,
      evidence: [{ document_id: documentId, quote: "Access changes require review within 14 days." }],
    });
    requireStatus(created, 201, "Synthetic source-cited policy");
    entry = created.json;
  }
  const entryId = entry.id;
  let detail = await api("GET", "/api/knowledge/" + entryId);
  requireStatus(detail, 200, "Synthetic policy detail");
  let revision = (detail.json?.revisions ?? []).find(item => item.revision_number === 1);
  if (!revision) throw new Error("The source-backed initial policy revision was unavailable.");
  if (revision.status === "draft") {
    const submitted = await api("POST", "/api/knowledge/" + entryId + "/submit");
    requireStatus(submitted, 200, "Initial policy submission");
    revision = submitted.json.revisions.find(item => item.revision_number === 1);
  }
  if (revision.status !== "published") {
    if (revision.status === "submitted") {
      await signOut();
      await signIn(manager, "manager");
      const reviewed = await api("POST", "/api/governance/knowledge/" + revision.id + "/decisions", {
        stage: "review", rationale: "Compared the exact synthetic source quote and policy scope.",
      });
      requireStatus(reviewed, 200, "Independent manager policy review");
    }
    await signOut();
    await signIn(secondEmployee, "employee");
    const published = await api("POST", "/api/governance/knowledge/" + revision.id + "/decisions", {
      stage: "publish", rationale: "Independently approved the source-backed synthetic policy.",
    });
    requireStatus(published, 200, "Independent employee policy publication");
    await signOut();
    await signIn(qa.admin, "admin");
  }
  detail = await api("GET", "/api/knowledge/" + entryId);
  requireStatus(detail, 200, "Published policy detail");
  let pendingRevision = (detail.json?.revisions ?? []).find(item => item.revision_number === 2);
  if (!pendingRevision) {
    const created = await api("POST", "/api/knowledge/" + entryId + "/revisions", {
      statement: revisedStatement, category: "policy", visibility: "shared", scope_group_id: null,
      evidence: [{ document_id: documentId, quote: "Access changes require review within 14 days." }],
    });
    requireStatus(created, 200, "Synthetic policy change revision");
    pendingRevision = created.json.revisions.find(item => item.revision_number === 2);
  }
  if (pendingRevision.status === "draft") {
    const submitted = await api("POST", "/api/knowledge/" + entryId + "/submit");
    requireStatus(submitted, 200, "Pending policy revision submission");
  }
  return entryId;
}
async function listProcesses() {
  const result = await api("GET", "/api/governance/processes");
  requireStatus(result, 200, "Policy process list");
  return result.json?.items ?? [];
}
async function listExceptions() {
  const result = await api("GET", "/api/governance/policy-exceptions");
  requireStatus(result, 200, "Policy exception list");
  return result.json?.items ?? [];
}
async function waitException(processId, entryId, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const item = (await listExceptions()).find(row => row.process_id === processId && row.entry_id === entryId);
    if (item) return item;
    await delay(300);
  }
  throw new Error("The policy action did not persist in the review database.");
}
async function connectBrowser() {
  const environment = {
    SYSTEMROOT: process.env.SYSTEMROOT, PATH: process.env.PATH, TEMP: process.env.TEMP,
    TMP: process.env.TMP, USERPROFILE: process.env.USERPROFILE, LOCALAPPDATA: process.env.LOCALAPPDATA,
  };
  browser = spawn(browserPath, [
    "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
    "--disable-background-networking", "--disable-component-update",
    "--disable-features=PasswordManagerOnboarding,PasswordManagerUI", "--disable-save-password-bubble",
    "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
    "--user-data-dir=" + profile, base.href,
  ], { stdio: "ignore", windowsHide: false, env: environment });
  const activeFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activeFile) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activeFile)) throw new Error("F17 isolated Chrome did not start.");
  const port = Number(readFileSync(activeFile, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline) {
    try {
      const response = await fetch("http://127.0.0.1:" + port + "/json/list");
      if (response.ok) page = (await response.json()).find(item => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("F17 browser page was unavailable.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("F17 browser debugger failed.")), { once: true });
  });
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const call = pending.get(message.id);
    clearTimeout(call.timer);
    pending.delete(message.id);
    if (message.error) call.reject(new Error("Browser command failed: " + call.method));
    else call.resolve(message.result);
  });
  await cdp("Page.enable");
  await cdp("Runtime.enable");
  await cdp("DOM.enable");
  await cdp("Page.setDeviceMetricsOverride", { width: 1365, height: 900, deviceScaleFactor: 1, mobile: false });
  await cdp("Page.navigate", { url: base.href });
  if (!await waitFor("Boolean(document.querySelector('form.login-form'))", 45_000)) {
    throw new Error("F17 review login did not render.");
  }
}
async function createProcessInUi(entryId) {
  const processes = await listProcesses();
  let process = processes.find(item => item.name === processName &&
    (item.policies ?? []).some(policy => policy.entry_id === entryId));
  if (process) return process;
  if (!await waitFor("Boolean(document.querySelector('.approval-section form'))")) {
    throw new Error("The policy impact map form was unavailable to the reviewer.");
  }
  const form = "document.querySelector('.approval-section form')";
  await setField(form, "Process name", processName);
  await setField(form, "Published policy", entryId);
  await setField(form, "Process description", "Synthetic access review process linked to a source-backed pending policy change.");
  const clicked = await evaluate("(()=>{const f=document.querySelector('.approval-section form');" +
    "const b=[...(f?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Create impact map');" +
    "b?.click();return Boolean(b)})()");
  if (!clicked) throw new Error("The impact map creation control was unavailable.");
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    process = (await listProcesses()).find(item => item.name === processName &&
      (item.policies ?? []).some(policy => policy.entry_id === entryId));
    if (process) return process;
    await delay(300);
  }
  throw new Error("The Manager UI impact map did not persist.");
}
async function assignThroughUi(process, entryId, assigneeEmail, existing) {
  if (existing) return existing;
  if (!await clickInProcess("Load in-scope assignees")) throw new Error("Manager could not load in-scope assignees.");
  if (!await waitFor("Boolean(" + processCardExpression() + "?.querySelector('.conflict-side .form-grid select'))")) {
    throw new Error("The scoped exception assignment form did not render.");
  }
  const setAssigneeExpression = "(()=>{const card=" + processCardExpression() +
    ";const select=card?.querySelector('.conflict-side .form-grid select');if(!select)return false;" +
    "const option=[...select.options].find(item=>item.textContent.toLowerCase().includes(" +
    JSON.stringify(assigneeEmail.toLowerCase()) + "));if(!option)return false;" +
    "Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,option.value);" +
    "select.dispatchEvent(new Event('input',{bubbles:true}));select.dispatchEvent(new Event('change',{bubbles:true}));return true})()";
  if (!await evaluate(setAssigneeExpression)) throw new Error("The in-scope employee was not offered by the policy source ACL.");
  await setField(processCardExpression(), "Assignment rationale",
    "Assign the policy change to the in-scope synthetic employee for review.");
  if (!await clickInProcess("Assign exception")) throw new Error("Manager could not assign the policy action through the UI.");
  return waitException(process.id, entryId, 30_000);
}
async function acknowledgeThroughUi(item, expectedEmail) {
  if (item.assigned_to.email.toLowerCase() !== expectedEmail.toLowerCase()) {
    throw new Error("The exception is not assigned to the authenticated synthetic employee.");
  }
  if (item.status === "acknowledged" || item.status === "resolved") return item;
  if (!item.can_acknowledge) throw new Error("The assigned employee could not acknowledge the policy action.");
  await setInException("Action rationale", "I reviewed the source-backed policy change and accepted the action.");
  if (!await clickInException("Acknowledge")) throw new Error("The employee acknowledgement control was unavailable.");
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const next = (await listExceptions()).find(row => row.id === item.id);
    if (next?.status === "acknowledged") return next;
    await delay(300);
  }
  throw new Error("The employee acknowledgement did not persist.");
}
async function resolveThroughUi(item, expectedEmail) {
  if (item.assigned_to.email.toLowerCase() !== expectedEmail.toLowerCase()) {
    throw new Error("The reassigned policy action is not owned by the authenticated synthetic employee.");
  }
  if (item.status === "resolved") return item;
  if (!item.can_resolve) throw new Error("The new assignee could not resolve the acknowledged policy action.");
  await setInException("Action rationale", "The revised access review procedure is implemented and verified.");
  if (!await clickInException("Resolve action")) throw new Error("The new assignee resolution control was unavailable.");
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const next = (await listExceptions()).find(row => row.id === item.id);
    if (next?.status === "resolved") return next;
    await delay(300);
  }
  throw new Error("The new assignee resolution did not persist.");
}
async function reassignThroughUi(item, newAssigneeEmail) {
  if (item.assigned_to.email.toLowerCase() === newAssigneeEmail.toLowerCase() &&
      item.events.some(event => event.action === "reassigned")) return item;
  if (!item.can_reassign) throw new Error("The Manager review permission did not allow reassignment.");
  if (!await clickInException("Load assignees for reassignment")) throw new Error("Manager could not load reassignment candidates.");
  if (!await waitFor("Boolean(" + exceptionCardExpression() + "?.querySelector('label.field select'))")) {
    throw new Error("The scoped reassignment selector did not render.");
  }
  const selectExpression = "(()=>{const card=" + exceptionCardExpression() +
    ";const label=[...(card?.querySelectorAll('label.field')??[])].find(x=>x.querySelector('.field-label')?.textContent.trim()==='New assignee');" +
    "const select=label?.querySelector('select');const option=[...(select?.options??[])].find(item=>item.textContent.toLowerCase().includes(" +
    JSON.stringify(newAssigneeEmail.toLowerCase()) + "));if(!select||!option)return false;" +
    "Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,option.value);" +
    "select.dispatchEvent(new Event('input',{bubbles:true}));select.dispatchEvent(new Event('change',{bubbles:true}));return true})()";
  if (!await evaluate(selectExpression)) throw new Error("The new eligible employee was not offered for reassignment.");
  await setInException("Action rationale", "Move the acknowledged policy action to the second in-scope synthetic employee.");
  if (!await clickInException("Reassign")) throw new Error("The Manager reassignment control was unavailable.");
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const next = (await listExceptions()).find(row => row.id === item.id);
    if (next?.assigned_to.email.toLowerCase() === newAssigneeEmail.toLowerCase() &&
        next.events.some(event => event.action === "reassigned")) return next;
    await delay(300);
  }
  throw new Error("The policy reassignment did not persist.");
}
async function saveEvidence(item, process) {
  const evidenceId = path.basename(statePath, ".json");
  await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true }).then(image => {
    writeFileSync(path.join(evidence, evidenceId + "-employee-acknowledged.png"), Buffer.from(image.data, "base64"), { flag: "wx" });
  });
  const responsive = await cdp("Page.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  void responsive;
  const fits = await evaluate("document.documentElement.scrollWidth<=window.innerWidth+1");
  if (!fits) throw new Error("F17 employee exception view overflows the 390px viewport.");
  const mobileImage = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  writeFileSync(path.join(evidence, evidenceId + "-employee-acknowledged-390px.png"), Buffer.from(mobileImage.data, "base64"), { flag: "wx" });
  const state = {
    process_id: process.id, policy_entry_id: item.entry_id, exception_id: item.id,
    status: item.status, assigned_to_user_id: item.assigned_to.user_id,
    event_actions: item.events.map(event => event.action),
    manager_reassignment_visible: item.events.some(event => event.action === "reassigned"),
  };
  writeFileSync(statePath, JSON.stringify(state, null, 2) + "\n", { flag: "wx" });
}
async function run() {
  await connectBrowser();
  await signIn(qa.admin, "admin");
  await openPage("/knowledge", "Knowledge");
  const document = await uploadSource();
  const entryId = await policySetup(document.id);
  console.log("LOCAL_F17_CURRENT_SOURCE_PUBLISHED_POLICY_AND_PENDING_REVISION=PASS");

  await signOut();
  await signIn(manager, "manager");
  await openPage("/approvals", "Approvals");
  if (!await waitFor("document.querySelector('.approval-section')?.innerText.includes(" +
    JSON.stringify(revisedStatement) + ")", 30_000)) {
    throw new Error("The Manager could not see the source-cited submitted policy change.");
  }
  const process = await createProcessInUi(entryId);
  const assignees = await api("GET", "/api/governance/processes/" + process.id +
    "/assignees?entry_id=" + encodeURIComponent(entryId));
  requireStatus(assignees, 200, "Source-readable policy assignees");
  const eligible = assignees.json?.items ?? [];
  const eligibleEmails = new Set(eligible.map(item => item.email.toLowerCase()));
  if (!eligibleEmails.has(firstEmployee.email.toLowerCase()) ||
      !eligibleEmails.has(secondEmployee.email.toLowerCase())) {
    throw new Error("Both synthetic employees must be source-readable eligible assignees.");
  }
  const secondEmployeeAssignee = eligible.find(item => item.email.toLowerCase() === secondEmployee.email.toLowerCase());
  if (!secondEmployeeAssignee?.user_id) throw new Error("The second synthetic employee's eligible account ID is missing.");
  let exception = (await listExceptions()).find(item => item.process_id === process.id && item.entry_id === entryId);
  exception = await assignThroughUi(process, entryId, firstEmployee.email, exception);
  console.log("LOCAL_F17_CURRENT_MANAGER_PROCESS_AND_EMPLOYEE_ASSIGNMENT=PASS");

  if (exception.assigned_to.email.toLowerCase() === firstEmployee.email.toLowerCase()) {
    await signOut();
    await signIn(firstEmployee, "employee");
    await openPage("/approvals", "Approvals");
    const inbox = (await listExceptions()).find(item => item.id === exception.id);
    if (!inbox || !inbox.can_acknowledge) throw new Error("The assigned Employee did not receive the policy action.");
    if (!inbox.events.some(event => event.action === "acknowledged")) {
      const forbidden = await api("POST", "/api/governance/policy-exceptions/" + exception.id + "/actions", {
        action: "reassign", assigned_to: secondEmployeeAssignee.user_id,
        rationale: "A non-reviewer must not reassign another employee's action.",
      });
      requireStatus(forbidden, 403, "Employee reassignment refusal");
      exception = await acknowledgeThroughUi(inbox, firstEmployee.email);
    } else {
      exception = inbox;
    }
    await signOut();
    await signIn(manager, "manager");
    await openPage("/approvals", "Approvals");
    exception = (await listExceptions()).find(item => item.id === exception.id);
  }

  exception = await reassignThroughUi(exception, secondEmployee.email);
  if (!exception.events.some(event => event.action === "acknowledged")) {
    throw new Error("The first employee acknowledgement was not retained in history.");
  }
  console.log("LOCAL_F17_CURRENT_MANAGER_REASSIGNMENT_AND_HISTORY=PASS");

  await signOut();
  await signIn(firstEmployee, "employee");
  await openPage("/approvals", "Approvals");
  const staleInbox = await listExceptions();
  if (staleInbox.some(item => item.id === exception.id)) throw new Error("The previous assignee retained inbox access.");
  const staleAction = await api("POST", "/api/governance/policy-exceptions/" + exception.id + "/actions", {
    action: "acknowledge", rationale: "The previous owner must no longer act after reassignment.",
  });
  requireStatus(staleAction, 403, "Revoked old-assignee access");
  console.log("LOCAL_F17_CURRENT_OLD_ASSIGNEE_ACCESS_REVOKED=PASS");

  await signOut();
  await signIn(secondEmployee, "employee");
  await openPage("/approvals", "Approvals");
  const secondInbox = (await listExceptions()).find(item => item.id === exception.id);
  if (!secondInbox || (!secondInbox.can_acknowledge && !secondInbox.can_resolve)) {
    throw new Error("The new assignee did not receive the open or acknowledged policy action.");
  }
  exception = secondInbox.status === "acknowledged"
    ? await resolveThroughUi(secondInbox, secondEmployee.email)
    : await acknowledgeThroughUi(secondInbox, secondEmployee.email);
  if (exception.status !== "resolved" ||
      exception.events.map(event => event.action).join(",") !== "assigned,acknowledged,reassigned,resolved") {
    throw new Error("The persisted F17 lifecycle did not contain the expected four append-only events.");
  }
  console.log("LOCAL_F17_CURRENT_NEW_ASSIGNEE_RESOLUTION=PASS");
  await saveEvidence(exception, process);
  console.log("LOCAL_F17_CURRENT_BROWSER_TO_POSTGRESQL=PASS");
  console.log("LOCAL_F17_CURRENT_REVIEW_ACCEPTANCE=PASS");
}

try {
  await run();
} catch (error) {
  console.error("LOCAL_F17_CURRENT_REVIEW_ACCEPTANCE=FAIL " + (error?.message ?? "unknown failure"));
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (browser?.exitCode === null) {
    browser.kill();
    await Promise.race([new Promise(resolve => browser.once("exit", resolve)), delay(5_000)]);
  }
  try { rmSync(profile, { recursive: true, force: true }); } catch {}
}

