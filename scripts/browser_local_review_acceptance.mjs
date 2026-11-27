#!/usr/bin/env node
/** Authenticated current-source local review journey against the isolated native QA stack. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { randomBytes } from "node:crypto";
import path from "node:path";
import { createServer } from "node:net";
import { setTimeout as delay } from "node:timers/promises";

const appUrl = process.env.INFRASPINE_LOCAL_REVIEW_URL ?? "http://127.0.0.1:5173/admin/";
const adminEmail = process.env.INFRASPINE_LOCAL_REVIEW_ADMIN_EMAIL;
const adminPassword = process.env.INFRASPINE_LOCAL_REVIEW_ADMIN_PASSWORD;
const fixturePath = process.env.INFRASPINE_LOCAL_REVIEW_FIXTURE;
const profile = process.env.INFRASPINE_LOCAL_REVIEW_PROFILE;
const pdfPath = process.env.INFRASPINE_LOCAL_REVIEW_PDF;
const reviewRoot = process.env.INFRASPINE_LOCAL_REVIEW_ROOT;
const downloadRoot = path.join(profile ?? "", "downloads");
const readProtectedFixture = () => JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const browserCandidates = process.platform === "win32"
  ? ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
    path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe")]
  : ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/microsoft-edge"];
const browserPath = process.env.INFRASPINE_BROWSER_PATH || browserCandidates.find((candidate) => candidate && existsSync(candidate));
const appHost = new URL(appUrl);
if (!adminEmail || !adminPassword || !fixturePath || !profile || !pdfPath || !reviewRoot) {
  throw new Error("The protected local review harness is missing a required setting.");
}
if (!(appHost.protocol === "http:" && ["127.0.0.1", "localhost", "::1"].includes(appHost.hostname))) {
  throw new Error("The local review browser must target loopback HTTP only.");
}
if (!browserPath || !existsSync(browserPath)) throw new Error("Chrome or Edge is unavailable for local acceptance.");
if (!existsSync(fixturePath) || !existsSync(pdfPath)) throw new Error("The protected synthetic fixture is unavailable.");
if (existsSync(profile)) throw new Error("The per-run browser profile already exists; choose a fresh profile path.");
mkdirSync(profile, { recursive: true });

const stamp = new Date().toISOString().replace(/[-:.]/g, "").replace("T", "-").replace("Z", "");
const employeeEmail = `review.employee.${Date.now()}@example.test`;
const employeePassword = randomBytes(36).toString("base64url");
const managerEmail = `review.manager.${Date.now()}@example.test`;
const managerPassword = randomBytes(36).toString("base64url");
const departmentName = `Local Review ${stamp}`;
const roleName = `Knowledge Reviewer ${stamp}`;
const taskTitle = `Review source follow-up ${stamp}`;
let browser;
let socket;
let sequence = 0;
let closedByRunner = false;
const pending = new Map();

function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Timed out during browser step ${method}.`));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await call("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,
  });
  if (result.exceptionDetails) throw new Error("A local review browser action raised an exception.");
  return result.result?.value;
}

async function waitFor(expression, timeoutMs = 25_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await delay(150);
  }
  return false;
}

async function fill(selector, value) {
  const ok = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:el instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!ok) throw new Error(`Expected local review form control is missing: ${selector}`);
}

async function fillAt(selector, index, value) {
  const ok = await evaluate(`(()=>{const el=[...document.querySelectorAll(${JSON.stringify(selector)})][${index}];if(!el)return false;const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!ok) throw new Error(`Expected local review form control is missing: ${selector}[${index}]`);
}

async function clickText(selector, text) {
  const ok = await evaluate(`(()=>{const el=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(text)});if(!el)return false;el.click();return true})()`);
  if (!ok) throw new Error(`Expected local review control is missing: ${text}`);
}

async function api(method, route, body = undefined) {
  const response = await evaluate(`(async()=>{const init={method:${JSON.stringify(method)},credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineAdmin'}};${body === undefined ? "" : `init.headers['Content-Type']='application/json';init.body=JSON.stringify(${JSON.stringify(body)});`}const r=await fetch(${JSON.stringify(route)},init);let data=null;try{data=await r.json()}catch{}return {status:r.status,data}})()`);
  if (response.status < 200 || response.status >= 300) {
    throw new Error(`Local API acceptance step returned HTTP ${response.status}: ${method} ${route}`);
  }
  return response.data;
}

async function apiStatus(method, route) {
  return evaluate(`(async()=>{const r=await fetch(${JSON.stringify(route)},{method:${JSON.stringify(method)},credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineAdmin'}});return r.status})()`);
}

async function signIn(email, password, role) {
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`)) {
    throw new Error("The local review sign-in page was not shown.");
  }
  await fill('form.login-form input[name="email"]', email);
  await fill('form.login-form input[name="password"]', password);
  await evaluate(`document.querySelector('form.login-form')?.requestSubmit()`);
  if (!await waitFor(`!!document.querySelector('.user-chip-role')`, 45_000)) {
    throw new Error("Synthetic local review sign-in failed.");
  }
  const identity = await api("GET", "/api/auth/me");
  if (identity.role !== role) throw new Error("The local review account returned an unexpected role.");
}

async function signOut() {
  await evaluate(`document.querySelector('button[aria-label="Sign out"]')?.click()`);
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`)) {
    throw new Error("The local review sign-out did not return to the sign-in page.");
  }
}

async function navigate(label, heading) {
  await clickText("button.nav-item", label);
  if (!await waitFor(`document.querySelector('main h1, h1')?.textContent.trim()===${JSON.stringify(heading)}`)) {
    throw new Error(`The local review page did not open: ${label}`);
  }
}

async function screenshot(name) {
  const result = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const output = path.join(reviewRoot, name);
  if (existsSync(output)) throw new Error(`Refusing to overwrite a saved review screenshot: ${name}`);
  writeFileSync(output, Buffer.from(result.data, "base64"), { flag: "wx" });
  return output;
}

async function connectBrowser() {
  const sanitizedEnv = { ...process.env };
  for (const name of [
    "INFRASPINE_LOCAL_REVIEW_ADMIN_EMAIL", "INFRASPINE_LOCAL_REVIEW_ADMIN_PASSWORD",
    "INFRASPINE_LOCAL_REVIEW_FIXTURE", "INFRASPINE_LOCAL_REVIEW_PDF",
    "INFRASPINE_LOCAL_REVIEW_PROFILE", "INFRASPINE_LOCAL_REVIEW_ROOT",
  ]) delete sanitizedEnv[name];
  browser = spawn(browserPath, [
    "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
    "--disable-background-networking", "--disable-component-update", "--disable-features=PasswordManagerOnboarding,PasswordManagerUI",
    "--disable-save-password-bubble", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
    `--user-data-dir=${profile}`, "about:blank",
  ], { stdio: "ignore", windowsHide: false, env: sanitizedEnv });

  const activePort = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activePort) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePort)) throw new Error("The isolated review browser did not expose its local debugging endpoint.");
  const debugPort = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline && browser.exitCode === null) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
      if (response.ok) page = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("The isolated review browser page did not become ready.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("The isolated review browser connection failed.")), { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const entry = pending.get(message.id);
    clearTimeout(entry.timer);
    pending.delete(message.id);
    if (message.error) entry.reject(new Error(`The local review browser command failed: ${entry.method}`));
    else entry.resolve(message.result);
  });
  await call("Page.enable");
  await call("Runtime.enable");
  await call("DOM.enable");
  mkdirSync(downloadRoot, { recursive: true });
  await call("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloadRoot, eventsEnabled: true });
  await call("Emulation.setDeviceMetricsOverride", { width: 1365, height: 900, deviceScaleFactor: 1, mobile: false });
  await call("Page.navigate", { url: appUrl });
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`, 45_000)) {
    throw new Error("The current-source local review login screen did not load.");
  }
  return { debugPort };
}

async function run() {
  await connectBrowser();
  await signIn(adminEmail, adminPassword, "admin");
  console.log("LOCAL_REVIEW_ADMIN_LOGIN=PASS");
  if (await waitFor(`!!document.querySelector('.first-run-setup form.deployment-form')`, 5_000)) {
    await fillAt('form.deployment-form input:not([type])', 0, `Synthetic Local Review ${stamp}`);
    await fillAt('form.deployment-form input:not([type])', 1, "Professional services");
    await fillAt('form.deployment-form input:not([type])', 2, `InfraSpine Review ${stamp.slice(-6)}`);
    await evaluate(`document.querySelector('.first-run-setup form.deployment-form')?.requestSubmit()`);
    if (!await waitFor(`!![...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.includes('People & access'))`, 45_000)) {
      throw new Error("The synthetic Admin could not complete required first-run company setup.");
    }
    console.log("LOCAL_REVIEW_FIRST_RUN_SETUP=PASS");
  }
  const adminNavigation = await waitFor(`!![...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.includes('People & access'))`);
  if (!adminNavigation) {
    const visibleControls = await evaluate(`([...document.querySelectorAll('button.nav-item')].map(x=>x.textContent.trim()).filter(Boolean))`);
    throw new Error(`The Admin People & access navigation was unavailable; visible navigation: ${JSON.stringify(visibleControls)}`);
  }

  await navigate("People & access", "People & access");
  await fill('input[aria-label="Department name"]', departmentName);
  await evaluate(`document.querySelector('input[aria-label="Department name"]')?.closest('form')?.requestSubmit()`);
  if (!await waitFor(`document.body.innerText.includes(${JSON.stringify(departmentName)})`)) {
    throw new Error("The Admin department form did not persist its synthetic department.");
  }
  const groupResponse = await api("GET", "/api/admin/access/groups");
  const group = groupResponse.groups.find((item) => item.name === departmentName && item.kind === "department");
  if (!group) throw new Error("The synthetic department was not present in the current API database.");

  const managerInvite = await api("POST", "/api/admin/users/invitations", {
    email: managerEmail,
    role: "manager",
    department_ids: [group.id],
  });
  if (managerInvite.role !== "manager" || !managerInvite.token) {
    throw new Error("The current-source Admin API did not issue a scoped Manager invitation.");
  }
  await signOut();
  await call("Page.navigate", { url: `${appUrl}#/onboarding?token=${encodeURIComponent(managerInvite.token)}` });
  if (!await waitFor(`document.querySelectorAll('.onboarding-card input[type="password"]').length===2`)) {
    throw new Error("The one-time Manager onboarding page did not load.");
  }
  await fillAt('.onboarding-card input[type="password"]', 0, managerPassword);
  await fillAt('.onboarding-card input[type="password"]', 1, managerPassword);
  await evaluate(`document.querySelector('.onboarding-card form')?.requestSubmit()`);
  if (!await waitFor(`document.querySelector('.onboarding-card h1')?.textContent==='Account ready'`, 30_000)) {
    throw new Error("The synthetic Manager invitation could not be accepted.");
  }
  const managerFixture = readProtectedFixture();
  managerFixture.managers ??= [];
  managerFixture.managers.push({ email: managerEmail, password: managerPassword, role: "manager" });
  writeFileSync(fixturePath, `${JSON.stringify(managerFixture, null, 2)}\n`, { encoding: "utf8" });
  await evaluate(`document.querySelector('.onboarding-card button')?.click()`);
  await signIn(managerEmail, managerPassword, "manager");
  const managed = await api("GET", "/api/departments/managed");
  if (!managed.groups.some((item) => item.id === group.id && item.name === departmentName)) {
    throw new Error("The Manager account did not retain its assigned department scope.");
  }
  await navigate("My departments", "My departments");
  if (!await waitFor(`document.body.innerText.includes(${JSON.stringify(departmentName)})`)) {
    throw new Error("The Manager My departments page did not show the assigned department.");
  }
  console.log("LOCAL_REVIEW_MANAGER_ONBOARDING_AND_DEPARTMENT_SCOPE=PASS");

  await signOut();
  await signIn(adminEmail, adminPassword, "admin");
  await navigate("People & access", "People & access");

  await fill('section.people-section input[type="email"]', employeeEmail);
  const departmentScopeReady = await waitFor(`(()=>{const section=[...document.querySelectorAll('section.people-section')].find(x=>x.querySelector('h2')?.textContent.trim()==='Invite an employee');return !!section&&[...section.querySelectorAll('label')].some(x=>x.textContent.includes(${JSON.stringify(departmentName)}))})()`, 20_000);
  if (!departmentScopeReady) throw new Error("The Admin invitation page did not finish loading its department scopes.");
  const inviteSelected = await evaluate(`(()=>{const section=[...document.querySelectorAll('section.people-section')].find(x=>x.querySelector('h2')?.textContent.trim()==='Invite an employee');if(!section)return false;const label=[...section.querySelectorAll('label')].find(x=>x.textContent.includes(${JSON.stringify(departmentName)}));const input=label?.querySelector('input[type="checkbox"]');if(!input)return false;if(!input.checked)input.click();return input.checked})()`);
  if (!inviteSelected) throw new Error("The employee invitation form did not expose the new department scope.");
  await delay(150);
  await evaluate(`(()=>{const section=[...document.querySelectorAll('section.people-section')].find(x=>x.querySelector('h2')?.textContent.trim()==='Invite an employee');section?.querySelector('form')?.requestSubmit()})()`);
  if (!await waitFor(`!!document.querySelector('.invite-link a')`, 30_000)) {
    throw new Error("The current-source Admin invitation form did not return its one-time acceptance link.");
  }
  const invitations = await api("GET", "/api/admin/users/invitations");
  const invitation = invitations.invitations.find((item) => item.email === employeeEmail);
  if (!invitation?.department_ids.includes(group.id)) {
    throw new Error("The current-source invitation did not persist the selected department membership.");
  }
  const invitationHref = await evaluate(`document.querySelector('.invite-link a')?.getAttribute('href')??''`);
  const inviteToken = new URL(invitationHref, appUrl).hash.match(/[?&]token=([^&]+)/)?.[1];
  if (!inviteToken) throw new Error("The local synthetic invitation link had no token.");

  await signOut();
  await call("Page.navigate", { url: `${appUrl}#/onboarding?token=${inviteToken}` });
  if (!await waitFor(`document.querySelectorAll('.onboarding-card input[type="password"]').length===2`)) {
    throw new Error("The one-time employee onboarding page did not load.");
  }
  await fillAt('.onboarding-card input[type="password"]', 0, employeePassword);
  await fillAt('.onboarding-card input[type="password"]', 1, employeePassword);
  await evaluate(`document.querySelector('.onboarding-card form')?.requestSubmit()`);
  if (!await waitFor(`document.querySelector('.onboarding-card h1')?.textContent==='Account ready'`, 30_000)) {
    throw new Error("The synthetic employee invitation could not be accepted.");
  }
  const fixture = readProtectedFixture();
  fixture.employees ??= [];
  fixture.employees.push({ email: employeeEmail, password: employeePassword, role: "employee" });
  writeFileSync(fixturePath, `${JSON.stringify(fixture, null, 2)}\n`, { encoding: "utf8" });
  await evaluate(`document.querySelector('.onboarding-card button')?.click()`);
  await signIn(employeeEmail, employeePassword, "employee");
  console.log("LOCAL_REVIEW_EMPLOYEE_INVITE_AND_ONBOARDING=PASS");

  const task = await api("POST", "/api/personal-tasks", {
    title: taskTitle,
    body: "Synthetic employee My Work acceptance task.",
    assignee_user_id: null,
    department_group_id: null,
    due_at: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
  });
  if (task.assignee_email !== employeeEmail || task.status !== "open") {
    throw new Error("Employee My Work did not persist the expected active personal assignment.");
  }
  await navigate("My work", "My work");
  if (!await waitFor(`document.body.innerText.includes(${JSON.stringify(taskTitle)})`)) {
    throw new Error("The employee My Work UI did not show its database-backed assignment.");
  }
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  const mobile = await evaluate(`({viewport:innerWidth,width:document.documentElement.scrollWidth})`);
  if (mobile.viewport !== 390 || mobile.width > 390) throw new Error("Employee My Work overflows the 390px review viewport.");
  const employeeShot = await screenshot(`employee-my-work-${stamp}.png`);
  await call("Emulation.setDeviceMetricsOverride", { width: 1365, height: 900, deviceScaleFactor: 1, mobile: false });
  console.log("LOCAL_REVIEW_EMPLOYEE_MY_WORK_390PX=PASS");

  await signOut();
  await signIn(adminEmail, adminPassword, "admin");
  const users = await api("GET", "/api/admin/users");
  const employee = users.users.find((user) => user.email === employeeEmail && user.role === "employee" && user.is_active);
  if (!employee || !employee.departments.some((item) => item.id === group.id)) {
    const diagnostic = users.users.filter((user) => user.email === employeeEmail).map((user) => ({
      role: user.role,
      isActive: user.is_active,
      departments: user.departments.map((item) => ({ id: item.id, name: item.name })),
    }));
    throw new Error(`The Admin employee list did not retain the active account and department membership: ${JSON.stringify({ matches: diagnostic, expectedDepartment: { id: group.id, name: group.name } })}`);
  }

  await navigate("People & access", "People & access");
  if (!await waitFor(`!!document.querySelector('section[aria-labelledby="role-template-heading"] form')`)) {
    throw new Error("The custom permission template form did not render for Admin.");
  }
  await fill('section[aria-labelledby="role-template-heading"] input[placeholder="Finance reviewer"]', roleName);
  await fill('section[aria-labelledby="role-template-heading"] input[placeholder="Purpose and approval scope"]', "Synthetic department-scoped knowledge review role.");
  await evaluate(`document.querySelector('section[aria-labelledby="role-template-heading"] form')?.requestSubmit()`);
  if (!await waitFor(`document.querySelector('section[aria-labelledby="role-template-heading"]')?.innerText.includes(${JSON.stringify(roleName)})`, 30_000)) {
    throw new Error("The custom permission template was not created through the Admin UI.");
  }
  const templateResponse = await api("GET", "/api/admin/role-templates");
  const template = templateResponse.templates.find((item) => item.name === roleName && item.is_active);
  if (!template) throw new Error("The Admin role-template API did not persist the created role.");

  const assignmentForm = await evaluate(`(()=>{const section=document.querySelector('section[aria-labelledby="role-template-heading"]');const forms=[...section.querySelectorAll('form')];return forms.at(-1)?.outerHTML??''})()`);
  if (!assignmentForm.includes("Assignment expires")) throw new Error("The role assignment form did not render.");
  const formReady = await evaluate(`(()=>{const section=document.querySelector('section[aria-labelledby="role-template-heading"]');const forms=[...section.querySelectorAll('form')];const f=forms.at(-1);const selects=[...f.querySelectorAll('select')];if(selects.length<2)return false;const set=(el,v)=>{Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))};set(selects[0],${JSON.stringify(template.id)});set(selects[1],${JSON.stringify(employeeEmail)});return true})()`);
  if (!formReady) throw new Error("The custom role assignment controls were incomplete.");
  await delay(150);
  const selectedScope = await evaluate(`(()=>{const section=document.querySelector('section[aria-labelledby="role-template-heading"]');const form=[...section.querySelectorAll('form')].at(-1);const label=[...form.querySelectorAll('label')].find(x=>x.textContent.includes(${JSON.stringify(departmentName)}));const input=label?.querySelector('input[type="checkbox"]');if(!input)return false;if(!input.checked)input.click();return input.checked})()`);
  if (!selectedScope) throw new Error("The department-scoped template could not be assigned in the Admin UI.");
  await delay(150);
  const assignmentFormState = await evaluate(`(()=>{const section=document.querySelector('section[aria-labelledby="role-template-heading"]');const form=[...section.querySelectorAll('form')].at(-1);return {selectValues:[...form.querySelectorAll('select')].map(x=>x.value),scopes:[...form.querySelectorAll('fieldset input[type="checkbox"]')].map(x=>({checked:x.checked,label:x.closest('label')?.textContent.trim()})),assignDisabled:[...form.querySelectorAll('button')].find(x=>x.textContent.includes('Assign template'))?.disabled??true}})()`);
  if (assignmentFormState.assignDisabled) throw new Error(`The department-scoped role form is not valid after selection: ${JSON.stringify(assignmentFormState)}`);
  await evaluate(`(()=>{const section=document.querySelector('section[aria-labelledby="role-template-heading"]');[...section.querySelectorAll('form')].at(-1)?.requestSubmit()})()`);
  const assignment = await waitFor(`[...document.querySelectorAll('section[aria-labelledby="role-template-heading"] table tbody tr')].some(x=>x.innerText.includes(${JSON.stringify(employeeEmail)})&&x.innerText.includes(${JSON.stringify(roleName)}))`, 30_000);
  if (!assignment) throw new Error("The custom role assignment did not appear in the persisted Admin UI list.");
  const assignmentRows = await api("GET", "/api/admin/role-template-assignments");
  if (!assignmentRows.assignments.some((item) => item.email === employeeEmail && item.template_name === roleName && !item.revoked_at)) {
    const assignmentUi = await evaluate(`(()=>{const section=document.querySelector('section[aria-labelledby="role-template-heading"]');return {alerts:[...document.querySelectorAll('[role="alert"],[role="status"]')].map(x=>x.innerText.trim()).filter(Boolean),rows:[...section.querySelectorAll('table tbody tr')].map(x=>x.innerText.trim())}})()`);
    throw new Error(`The custom role assignment was not persisted in PostgreSQL: ${JSON.stringify(assignmentUi)}`);
  }
  const adminPeopleShot = await screenshot(`admin-people-access-${stamp}.png`);
  console.log("LOCAL_REVIEW_CUSTOM_ROLE_SCOPED_ASSIGNMENT=PASS");

  await navigate("Knowledge", "Knowledge");
  const root = await call("DOM.getDocument", { depth: -1, pierce: true });
  const input = await call("DOM.querySelector", {
    nodeId: root.root.nodeId,
    selector: 'input[type="file"][multiple][accept*=".pdf"]',
  });
  if (!input.nodeId) throw new Error("The Knowledge upload control did not render.");
  await call("DOM.setFileInputFiles", { files: [pdfPath], nodeId: input.nodeId });

  let uploaded;
  const uploadDeadline = Date.now() + 20_000;
  while (Date.now() < uploadDeadline) {
    const documentList = await api("GET", "/api/rag/documents");
    uploaded = documentList.documents.find((item) => item.filename === path.basename(pdfPath));
    if (uploaded) break;
    await delay(250);
  }
  if (!uploaded) throw new Error("The current-source PDF upload did not create a document record.");
  const readyDeadline = Date.now() + 120_000;
  while (uploaded.status !== "ready" && uploaded.status !== "failed" && Date.now() < readyDeadline) {
    await delay(750);
    const current = await api("GET", `/api/rag/documents/${uploaded.id}`);
    uploaded = current;
  }
  if (uploaded.status !== "ready" || uploaded.chunk_count < 1) {
    throw new Error("The real local PDF worker did not finish with searchable chunks.");
  }
  if (!await waitFor(`document.body.innerText.includes(${JSON.stringify(path.basename(pdfPath))})&&document.body.innerText.includes('Ready')`, 30_000)) {
    throw new Error("The Knowledge library did not show the indexed PDF as Ready.");
  }
  console.log("LOCAL_REVIEW_PDF_WORKER_READY=PASS (real local embedder and FAISS; mock LLM only)");

  const extraction = await api("GET", `/api/rag/documents/${uploaded.id}/financial-extraction?profile=invoice&date_order=dmy`);
  const expectedInvoice = { invoice_number: "INV-204", document_date: "2026-09-30", vendor: "North Star Supplies", total: "20.00", currency: "USD" };
  for (const [field, value] of Object.entries(expectedInvoice)) {
    if (extraction.fields?.[field]?.value !== value) throw new Error(`Synthetic invoice extraction was incomplete for ${field}.`);
  }
  const reviewPayload = {
    expected_document_version: extraction.document_version,
    profile: "invoice",
    date_order: "dmy",
    fields: Object.fromEntries(Object.entries(extraction.fields).map(([name, field]) => [name, field.value])),
    line_items: extraction.line_items.map(({ description, quantity, unit_price, amount, currency }) => ({ description, quantity, unit_price, amount, currency })),
    reviewed_exception_indexes: extraction.exceptions.map((_, index) => index),
    rationale: "Verified against the synthetic invoice source page.",
  };
  reviewPayload.fields.invoice_number = "INV-204-CHECKED";
  const reviewedExtraction = await api("POST", `/api/rag/documents/${uploaded.id}/financial-extraction/review`, reviewPayload);
  if (reviewedExtraction.review_status !== "reviewed" || reviewedExtraction.review?.revision !== 1) {
    throw new Error("The synthetic invoice did not persist its human review revision.");
  }
  console.log("LOCAL_F06_CURRENT_SOURCE_ADMIN_REVIEWED_INVOICE=PASS");

  const question = "What is the service target for ticket F12-4817?";
  await fill('textarea[aria-label="Question"]', question);
  await evaluate(`document.querySelector('form.chat-composer')?.requestSubmit()`);
  if (!await waitFor(`document.querySelector('.sources .source')?.innerText.includes(${JSON.stringify(path.basename(pdfPath))})`, 90_000)) {
    throw new Error("The browser chat did not render a citation from the synthetic PDF.");
  }
  const cited = await evaluate(`(()=>({text:document.querySelector('.bubble-ai')?.innerText??'',source:document.querySelector('.sources .source')?.innerText??'',citation:!!document.querySelector('.bubble-ai .cite-chip')}))()`);
  if (!cited.citation || !cited.text.includes("48 hours") || !cited.source.includes(path.basename(pdfPath))) {
    throw new Error("The mock-mode answer failed the source-grounded citation check.");
  }
  const answerShot = await screenshot(`knowledge-cited-answer-${stamp}.png`);

  const opened = await evaluate(`(()=>{const button=document.querySelector('.sources .source-head');if(!button)return false;button.click();return true})()`);
  if (!opened || !await waitFor(`!!document.querySelector('.source-page-link')`, 20_000)) {
    throw new Error("The cited PDF source did not expose its original page preview.");
  }
  const previewUrl = await evaluate(`document.querySelector('.source-page-link')?.getAttribute('href')??''`);
  const previewResponse = await evaluate(`(async()=>{const r=await fetch(${JSON.stringify(previewUrl)});const blob=await r.blob();return {status:r.status,type:r.headers.get('content-type')??'',size:blob.size}})()`);
  if (previewResponse.status !== 200 || !previewResponse.type.startsWith("image/") || previewResponse.size < 100) {
    throw new Error("The original PDF page preview did not return a real image.");
  }
  await call("Page.navigate", { url: new URL(previewUrl, appUrl).href });
  if (!await waitFor(`document.contentType.startsWith('image/')`, 30_000)) {
    throw new Error("The browser could not display the original page preview.");
  }
  const previewShot = await screenshot(`original-pdf-preview-${stamp}.png`);
  const original = await evaluate(`(()=>({type:document.contentType,loaded:!!document.images[0]&&document.images[0].naturalWidth>0}))()`);
  if (!original.loaded) throw new Error("The original PDF page image did not render in the browser.");
  console.log("LOCAL_REVIEW_CITED_ANSWER_AND_ORIGINAL_PREVIEW=PASS");

  await call("Page.navigate", { url: appUrl });
  if (!await waitFor(`!!document.querySelector('.user-chip-role')`, 30_000)) {
    throw new Error("The Admin review session did not return to the application after previewing the source.");
  }
  await signOut();
  await signIn(employeeEmail, employeePassword, "employee");
  const adminRouteStatus = await apiStatus("GET", "/api/admin/users");
  const managerRouteStatus = await apiStatus("GET", "/api/departments/managed");
  if (adminRouteStatus !== 403 || managerRouteStatus !== 403) {
    throw new Error("The Employee account was not refused from Admin and Manager-only routes.");
  }
  const employeeExportControl = await evaluate(`([...document.querySelectorAll('button')].some(x=>x.textContent.trim()==='Export reviewed bookkeeping CSV'))`);
  const employeeCsv = await evaluate(`(async()=>{const r=await fetch('/api/rag/financial-extractions/bookkeeping.csv',{credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineAdmin'}});return {status:r.status}})()`);
  if (employeeExportControl || employeeCsv.status !== 403) throw new Error("Employee saw or could directly retrieve the reviewed bookkeeping export.");
  console.log("LOCAL_F06_CURRENT_SOURCE_EMPLOYEE_EXPORT_HIDDEN_AND_403=PASS");
  console.log("LOCAL_REVIEW_EMPLOYEE_ADMIN_AND_MANAGER_REFUSAL=PASS");
  await signOut();
  await signIn(managerEmail, managerPassword, "manager");
  await navigate("Knowledge", "Knowledge");
  const managerExportControl = await evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Export reviewed bookkeeping CSV');return !!b&&!b.disabled})()`);
  if (!managerExportControl) throw new Error("Manager did not receive the reviewed bookkeeping CSV export control.");
  const managerExportShot = await screenshot(`manager-bookkeeping-export-${stamp}.png`);
  await clickText("button", "Export reviewed bookkeeping CSV");
  const downloadDeadline = Date.now() + 20_000;
  let csvName;
  while (Date.now() < downloadDeadline) {
    csvName = readdirSync(downloadRoot).find((name) => name === "infraspine-bookkeeping-reviewed.csv");
    if (csvName) break;
    await delay(150);
  }
  if (!csvName) throw new Error("Manager export control did not download the reviewed CSV.");
  const csv = readFileSync(path.join(downloadRoot, csvName), "utf8");
  if (!csv.includes("INV-204-CHECKED")) throw new Error("Manager CSV did not include the reviewed synthetic invoice.");
  const managerCsvApi = await evaluate(`(async()=>{const r=await fetch('/api/rag/financial-extractions/bookkeeping.csv',{credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineAdmin'}});return {status:r.status,type:r.headers.get('content-type')??''}})()`);
  if (managerCsvApi.status !== 200 || !managerCsvApi.type.includes("text/csv")) throw new Error("Manager direct CSV export did not return HTTP 200 with CSV content.");
  console.log("LOCAL_F06_CURRENT_SOURCE_MANAGER_REVIEWED_CSV_DOWNLOAD_AND_200=PASS");
  const f06Evidence = {
    database: process.env.INFRASPINE_LOCAL_REVIEW_DATABASE ?? "not-provided",
    migration: process.env.INFRASPINE_LOCAL_REVIEW_MIGRATION ?? "not-provided",
    gitHead: process.env.INFRASPINE_LOCAL_REVIEW_SOURCE_HEAD ?? "not-provided",
    reviewUrl: appUrl,
    documentId: uploaded.id,
    documentVersion: extraction.document_version,
    managerExportStatus: managerCsvApi.status,
    employeeExportStatus: employeeCsv.status,
    managerCsvHasReviewedInvoice: true,
    managerScreenshot: managerExportShot,
    acceptedAtUtc: new Date().toISOString(),
  };
  const f06EvidencePath = path.join(reviewRoot, `f06-current-source-browser-${stamp}.json`);
  writeFileSync(f06EvidencePath, `${JSON.stringify(f06Evidence, null, 2)}\n`, { flag: "wx" });
  console.log("LOCAL_F06_CURRENT_SOURCE_EVIDENCE=" + f06EvidencePath);
  await signIn(adminEmail, adminPassword, "admin");

  console.log("LOCAL_REVIEW_AUTHENTICATED_CURRENT_SOURCE_BROWSER_TO_POSTGRESQL=PASS");
  console.log("LOCAL_REVIEW_SCREENSHOTS=" + JSON.stringify({ employeeMyWork: employeeShot, adminPeople: adminPeopleShot, citedAnswer: answerShot, originalPreview: previewShot }));
}

try {
  await run();
} catch (error) {
  console.error(`LOCAL_REVIEW_BROWSER_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (browser?.exitCode === null) {
    try {
      const activePort = path.join(profile, "DevToolsActivePort");
      if (existsSync(activePort)) {
        const debugPort = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
        await fetch(`http://127.0.0.1:${debugPort}/json/version`).catch(() => undefined);
      }
    } catch {}
    browser.kill();
    await Promise.race([new Promise((resolve) => browser.once("exit", resolve)), delay(5_000)]);
    closedByRunner = true;
  }
  if (closedByRunner) console.log("LOCAL_REVIEW_AUTOMATION_BROWSER_CLOSED=PASS");
}
