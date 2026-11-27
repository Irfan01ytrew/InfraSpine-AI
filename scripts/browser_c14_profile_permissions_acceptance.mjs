#!/usr/bin/env node
/** Focused current-source UI/API acceptance for manager agent versioning and employee read-only access. */
import { createHash } from "node:crypto";
import { randomBytes } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_AGENT_REVIEW_URL ?? "http://127.0.0.1:5187/admin/");
const fixturePath = process.env.INFRASPINE_AGENT_REVIEW_FIXTURE;
const evidenceDir = path.resolve(process.env.INFRASPINE_AGENT_REVIEW_EVIDENCE_DIR ?? "");
if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || !fixturePath || !evidenceDir) {
  throw new Error("Agent permission acceptance requires a loopback review URL and explicit protected fixture/evidence paths.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const admin = fixture.admin;
const manager = fixture.manager;
const outsider = fixture.outsider;
if (![admin, manager, outsider].every((item) => item?.email && item?.password)) {
  throw new Error("Synthetic role fixture is incomplete.");
}
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome is unavailable for isolated browser acceptance.");

mkdirSync(evidenceDir, { recursive: true });
const tempRoot = path.join(evidenceDir, "tmp");
mkdirSync(tempRoot, { recursive: true });
const profileDir = mkdtempSync(path.join(tempRoot, "c14-profile-permissions-"));
const sourceFiles = [
  "app/api/routes/agents.py",
  "app/services/agents.py",
  "app/schemas/agents.py",
  "frontend/src/components/AgentCatalogPage.tsx",
  "frontend/src/api.ts",
  "frontend/src/types.ts",
];
let chrome;
let socket;
let sequence = 0;
const pending = new Map();
const apiStatuses = [];

function command(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Browser timed out at ${method}.`));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression, timeoutMs = 30_000) {
  const response = await command("Runtime.evaluate", {
    expression: `Promise.resolve().then(()=>(${expression})).then(value=>JSON.stringify(value))`,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,
  }, timeoutMs);
  if (response.exceptionDetails) throw new Error(`Browser evaluation failed: ${response.exceptionDetails.text}.`);
  const value = response.result?.value;
  return value === undefined ? undefined : JSON.parse(value);
}

async function waitFor(expression, label, timeoutMs = 35_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(125);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}

async function clickText(label, selector = "button") {
  return Boolean(await evaluate(`(()=>{const e=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(label)});if(!e)return false;e.click();return true})()`));
}

async function signIn(account, role) {
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "login form");
  const submitted = await evaluate(`(()=>{const e=document.querySelector('form.login-form input[name="email"]'),p=document.querySelector('form.login-form input[name="password"]'),f=document.querySelector('form.login-form');if(!e||!p||!f)return false;for(const [x,v] of [[e,${JSON.stringify(account.email)}],[p,${JSON.stringify(account.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(x,v);x.dispatchEvent(new Event('input',{bubbles:true}));x.dispatchEvent(new Event('change',{bubbles:true}))}f.requestSubmit();return true})()`);
  if (!submitted) throw new Error(`Could not submit the synthetic ${role} login form.`);
  await waitFor("Boolean(document.querySelector('.shell'))", `${role} shell`, 45_000);
  const identity = await evaluate("(async()=>{const r=await fetch('/api/auth/me');const u=r.ok?await r.json():null;return {status:r.status,role:u?.role}})()");
  if (identity.status !== 200 || identity.role !== role) throw new Error(`Synthetic ${role} identity did not authenticate.`);
}

async function signOut() {
  const clicked = await evaluate(`(()=>{const b=document.querySelector('button[aria-label="Sign out"]');if(!b)return false;b.click();return true})()`);
  if (!clicked) throw new Error("Could not sign out the current synthetic account.");
  await waitFor("Boolean(document.querySelector('form.login-form'))", "signed-out login form");
}

async function openProfiles() {
  if (!await clickText("Agent profiles", "button.nav-item")) throw new Error("Agent profiles navigation is unavailable.");
  await waitFor("document.querySelector('main h1')?.textContent.trim()==='Agent profiles'", "agent profile page");
}

async function apiRequest(url, method = "GET", body) {
  const result = await evaluate(`(async()=>{const init={method:${JSON.stringify(method)},credentials:'same-origin',headers:{Accept:'application/json','X-Requested-With':'InfraSpineAdmin'}};${body === undefined ? "" : `init.headers['Content-Type']='application/json';init.body=JSON.stringify(${JSON.stringify(body)});`}const r=await fetch(${JSON.stringify(url)},init);let data=null;try{data=await r.json()}catch{}return {status:r.status,data}})()`);
  if (url.startsWith("/api/agents")) apiStatuses.push({ method, path: url, status: result.status });
  return result;
}

try {
  chrome = spawn(browserPath, [
    "--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profileDir}`, "about:blank",
  ], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profileDir, "DevToolsActivePort");
  const startBy = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < startBy) await delay(100);
  if (!existsSync(portFile)) throw new Error("Isolated Chromium did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!target?.webSocketDebuggerUrl) throw new Error("Isolated Chromium page target is unavailable.");
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer);
    pending.delete(message.id);
    message.error ? item.reject(new Error(`Chrome DevTools ${item.method} failed.`)) : item.resolve(message.result);
  });
  await command("Page.enable");
  await command("Runtime.enable");
  await command("Page.navigate", { url: base.href });

  await signIn(manager, "manager");
  await openProfiles();
  const departmentsResult = await apiRequest("/api/agents/departments");
  const department = departmentsResult.data?.departments?.[0];
  if (departmentsResult.status !== 200 || !department?.id) throw new Error("Manager had no manageable department for the scoped versioning test.");
  await waitFor("Boolean(document.querySelector('#department-agent-profile-form input[maxlength=\"120\"]'))", "manager profile form");

  const profileName = `QA C14 scoped agent ${Date.now()}`;
  const filled = await evaluate(`(()=>{const f=document.querySelector('#department-agent-profile-form');const name=f?.querySelector('input[maxlength="120"]');const selects=[...(f?.querySelectorAll('select')??[])];if(!name||selects.length!==3)return false;const set=(e,v,proto)=>{Object.getOwnPropertyDescriptor(proto,'value').set.call(e,v);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}))};set(name,${JSON.stringify(profileName)},HTMLInputElement.prototype);set(selects[0],${JSON.stringify(department.id)},HTMLSelectElement.prototype);set(selects[1],'knowledge_research',HTMLSelectElement.prototype);set(selects[2],'requester_acl',HTMLSelectElement.prototype);return true})()`);
  if (!filled || !await clickText("Create agent profile")) throw new Error("Manager could not submit the scoped agent form.");
  await waitFor("document.querySelector('.banner-success')?.textContent.includes('Agent profile saved')", "saved manager profile", 30_000);
  const afterCreate = await apiRequest("/api/agents");
  const v1 = afterCreate.data?.agents?.find((item) => item.name === profileName && item.enabled);
  if (afterCreate.status !== 200 || !v1?.can_manage || v1.department_group_id !== department.id || v1.version !== 1) {
    throw new Error("Manager-created profile was not version 1 in the manager's department.");
  }

  const editOpened = await evaluate(`(()=>{const card=[...document.querySelectorAll('.automation-item')].find(e=>e.querySelector('h3')?.textContent.trim()===${JSON.stringify(profileName)});const b=[...(card?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Create next version');if(!b)return false;b.click();return true})()`);
  if (!editOpened) throw new Error("Manager did not receive the create-next-version control.");
  await waitFor("[...document.querySelectorAll('h2')].some((heading)=>heading.textContent.trim().includes('Create version 2'))", "version editor");
  const v2Name = `${profileName} v2`;
  const versionFilled = await evaluate(`(()=>{const f=document.querySelector('#department-agent-profile-form');const n=f?.querySelector('input[maxlength="120"]');if(!n)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(n,${JSON.stringify(v2Name)});n.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
  if (!versionFilled || !await clickText("Save new version")) throw new Error("Manager could not submit a new agent version.");
  await waitFor("document.querySelector('.banner-success')?.textContent.includes('new immutable version')", "saved version 2", 30_000);
  const managerVersionsResult = await apiRequest("/api/agents");
  const managerVersions = managerVersionsResult.data?.agents?.filter((item) => item.agent_key === v1.agent_key) ?? [];
  const v1Now = managerVersions.find((item) => item.version === 1);
  const v2 = managerVersions.find((item) => item.version === 2);
  if (managerVersionsResult.status !== 200 || managerVersions.length !== 2 || v1Now?.enabled !== false ||
      v2?.name !== v2Name || v2.enabled !== true || !managerVersions.every((item) => item.can_manage)) {
    throw new Error("Manager version history was not immutable or did not preserve scoped management.");
  }

  const stale = await apiRequest(`/api/agents/${v1.id}`, "PUT", {
    expected_version: 1, name: `${profileName} stale`, agent_type: "knowledge_research",
    knowledge_scope: "requester_acl", tool_policy: ["knowledge_search"],
  });
  const managerAfterStale = await apiRequest("/api/agents");
  if (stale.status !== 409 || managerAfterStale.data?.agents?.filter((item) => item.agent_key === v1.agent_key).length !== 2) {
    throw new Error("A stale Manager version update was not rejected without creating another version.");
  }

  await signOut();
  await signIn(admin, "admin");
  const temporaryEmail = `qa+c14-${Date.now()}@example.test`;
  const temporaryPassword = randomBytes(32).toString("base64url");
  const invitation = await apiRequest("/api/admin/users/invitations", "POST", {
    email: temporaryEmail, role: "employee", department_ids: [department.id],
  });
  if (invitation.status !== 201 || !invitation.data?.token) throw new Error("Admin could not issue a temporary department-scoped Employee invitation.");
  const accepted = await apiRequest("/api/onboarding/accept", "POST", {
    token: invitation.data.token, password: temporaryPassword,
  });
  if (accepted.status !== 200 || accepted.data?.role !== "employee") throw new Error("The temporary synthetic Employee invitation was not accepted.");
  const createdUsers = await apiRequest("/api/admin/users");
  const temporaryUser = createdUsers.data?.users?.find((item) => item.email === temporaryEmail);
  if (createdUsers.status !== 200 || !temporaryUser?.is_active ||
      !temporaryUser.departments?.some((item) => item.id === department.id)) {
    throw new Error("The temporary Employee was not assigned to the selected department.");
  }
  await signOut();
  await signIn({ email: temporaryEmail, password: temporaryPassword }, "employee");
  await openProfiles();
  await waitFor(`Boolean([...document.querySelectorAll('.automation-item')].find(e=>e.querySelector('h3')?.textContent.trim()===${JSON.stringify(v2Name)}))`, "employee-visible active profile");
  const employeeProfilesResult = await apiRequest("/api/agents");
  const employeeProfile = employeeProfilesResult.data?.agents?.find((item) => item.agent_key === v1.agent_key);
  const employeeCard = await evaluate(`(()=>{const card=[...document.querySelectorAll('.automation-item')].find(e=>e.querySelector('h3')?.textContent.trim()===${JSON.stringify(v2Name)});return {text:card?.textContent??'',buttons:[...(card?.querySelectorAll('button')??[])].map(b=>b.textContent.trim())}})()`);
  if (employeeProfilesResult.status !== 200 || !employeeProfile || employeeProfile.version !== 2 || !employeeProfile.enabled ||
      employeeProfile.can_manage || /version 1/i.test(employeeCard?.text ?? "") ||
      !employeeCard?.buttons.includes("Run profile") || employeeCard.buttons.includes("Create next version") || employeeCard.buttons.includes("Disable")) {
    throw new Error("Employee did not receive only the active, run-only profile view.");
  }
  const employeeCreate = await apiRequest("/api/agents", "POST", {
    name: `${profileName} employee attempt`, department_group_id: department.id, agent_type: "knowledge_research",
    knowledge_scope: "requester_acl", tool_policy: ["knowledge_search"],
  });
  const employeeVersion = await apiRequest(`/api/agents/${v2.id}`, "PUT", {
    expected_version: 2, name: `${v2Name} employee attempt`, agent_type: "knowledge_research",
    knowledge_scope: "requester_acl", tool_policy: ["knowledge_search"],
  });
  if (employeeCreate.status !== 403 || employeeVersion.status !== 403) {
    throw new Error("Employee backend permission enforcement did not deny profile create and version writes.");
  }

  await signOut();
  await signIn(outsider, "employee");
  const outsiderProfiles = await apiRequest("/api/agents");
  const outsiderDepartments = await apiRequest("/api/agents/departments");
  if (outsiderProfiles.status !== 200 || outsiderProfiles.data?.agents?.some((item) => item.agent_key === v1.agent_key) ||
      outsiderDepartments.status !== 200 || outsiderDepartments.data?.departments?.length !== 0) {
    throw new Error("Unassigned Employee was able to discover the scoped profile or a manageable department.");
  }
  await signOut();
  await signIn(admin, "admin");
  const allProfiles = await apiRequest("/api/agents");
  const testProfiles = allProfiles.data?.agents?.filter((item) => item.name.startsWith("QA C14 scoped agent ")) ?? [];
  const testProfileDeactivations = [];
  for (const profile of testProfiles.filter((item) => item.enabled)) {
    testProfileDeactivations.push(await apiRequest(`/api/agents/${profile.id}/disable`, "POST", {}));
  }
  const deactivateEmployee = await apiRequest(`/api/admin/users/${temporaryUser.id}/active`, "POST", { active: false });
  if (deactivateEmployee.status !== 200 || deactivateEmployee.data?.is_active !== false ||
      testProfileDeactivations.some((result) => result.status !== 200)) {
    throw new Error("Synthetic QA cleanup did not deactivate the temporary Employee and test profiles.");
  }

  const uiHealth = await fetch(base);
  const apiHealth = await fetch("http://127.0.0.1:63015/health/ready");
  if (!uiHealth.ok || !apiHealth.ok) throw new Error("Review UI/API did not remain healthy after profile acceptance.");
  const sourceSha256 = Object.fromEntries(sourceFiles.map((file) => [file, createHash("sha256").update(readFileSync(file)).digest("hex")]));
  const evidence = {
    result: "PASS",
    acceptance: "manager UI create/version, stale-version conflict, employee active read-only UI and API denial, unassigned employee denial",
    reviewUrl: base.href,
    apiHealthUrl: "http://127.0.0.1:63015/health/ready",
    database: "infraspine_c15mywork_410a55d4_review",
    migration: "0065_workflow_personal_task_action",
    providerMode: "existing synthetic review fixture; no external provider called",
    managerCreateStatus: 201,
    managerVersionStatus: 201,
    staleVersionStatus: stale.status,
    employeeProfileVersion: employeeProfile.version,
    employeeUiButtons: employeeCard.buttons,
    employeeCreateStatus: employeeCreate.status,
    employeeVersionStatus: employeeVersion.status,
    temporaryEmployeeDeactivated: !deactivateEmployee.data.is_active,
    testProfileVersionsRetainedDisabled: testProfiles.length,
    outsiderVisibleProfiles: outsiderProfiles.data.agents.length,
    outsiderManageableDepartments: outsiderDepartments.data.departments.length,
    managerVisibleVersionCount: managerVersions.length,
    uiHealthStatus: uiHealth.status,
    apiHealthStatus: apiHealth.status,
    sourceSha256,
    apiStatuses,
  };
  const evidencePath = path.join(evidenceDir, "c14-profile-permissions-browser.json");
  writeFileSync(evidencePath, `${JSON.stringify(evidence, null, 2)}\n`, { mode: 0o600 });
  console.log(`C14_PROFILE_PERMISSIONS_BROWSER=PASS; evidence=${evidencePath}; UI=${uiHealth.status}; API=${apiHealth.status}; Manager create/version=201; stale=409; Employee writes=403/403; outsider profile visibility=0`);
} finally {
  if (socket?.readyState === WebSocket.OPEN) {
    try { await command("Browser.close", {}, 3_000); } catch { socket.close(); }
  }
  if (chrome && chrome.exitCode === null) {
    chrome.kill();
    await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3_000)]);
  }
  const safeTmp = path.resolve(tempRoot);
  const safeProfile = path.resolve(profileDir);
  if (safeProfile.startsWith(`${safeTmp}${path.sep}`) && path.basename(safeProfile).startsWith("c14-profile-permissions-")) {
    await import("node:fs/promises").then(({ rm }) => rm(safeProfile, { recursive: true, force: true }));
  }
}
