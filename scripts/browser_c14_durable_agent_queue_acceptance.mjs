#!/usr/bin/env node
/** Current-source UI/API acceptance for the durable read-only department agent queue. */
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_C14_REVIEW_URL ?? "http://127.0.0.1:5186/admin/");
const reviewRoot = process.env.INFRASPINE_C14_REVIEW_ROOT;
const fixturePath = process.env.INFRASPINE_C14_REVIEW_FIXTURE;
const sourceEvidence = JSON.parse(readFileSync(path.join(reviewRoot ?? "", "evidence", "c13-training-fixture.json"), "utf8"));
const groupId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const profileName = "Queued knowledge procedure";
if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || !reviewRoot || !fixturePath || sourceEvidence.result !== "PASS") {
  throw new Error("C14 queue acceptance requires the loopback stack and protected synthetic source fixture.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const admin = fixture.admin; const employee = fixture.finance_employee; const outsider = fixture.unassigned_employee;
if (![admin, employee, outsider].every((item) => item?.email && item?.password)) throw new Error("Synthetic account fixture is incomplete.");
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome is unavailable for current-source C14 browser acceptance.");

const tmp = path.join(reviewRoot, "tmp"); const evidenceDir = path.join(reviewRoot, "evidence");
mkdirSync(tmp, { recursive: true }); mkdirSync(evidenceDir, { recursive: true });
const profile = mkdtempSync(path.join(tmp, "browser-c14-agent-queue-"));
const screenshotPath = path.join(evidenceDir, "c14-agent-queue-employee-390.png");
let chrome; let socket; let sequence = 0;
const pending = new Map(); const apiResponses = [];
function command(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser timed out at ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 30_000) {
  const result = await command("Runtime.evaluate", {
    expression: `Promise.resolve().then(()=>(${expression})).then(value=>JSON.stringify(value))`,
    awaitPromise: true, returnByValue: true, userGesture: true,
  }, timeoutMs);
  if (result.exceptionDetails) throw new Error(`C14 browser evaluation failed at ${result.exceptionDetails.text}.`);
  const encoded = result.result?.value;
  return encoded === undefined ? undefined : JSON.parse(encoded);
}
async function waitFor(expression, label, timeoutMs = 35_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) { if (await evaluate(expression)) return; await delay(125); }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function clickText(label, selector = "button") {
  return Boolean(await evaluate(`(()=>{const e=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(label)});if(!e)return false;e.click();return true})()`));
}
async function signIn(account) {
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "synthetic login form");
  const submitted = await evaluate(`(()=>{const e=document.querySelector('form.login-form input[name="email"]'),p=document.querySelector('form.login-form input[name="password"]'),f=document.querySelector('form.login-form');if(!e||!p||!f)return false;for(const [x,v] of [[e,${JSON.stringify(account.email)}],[p,${JSON.stringify(account.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(x,v);x.dispatchEvent(new Event('input',{bubbles:true}));x.dispatchEvent(new Event('change',{bubbles:true}))}f.requestSubmit();return true})()`);
  if (!submitted) throw new Error("Synthetic login form could not be submitted.");
  await waitFor("Boolean(document.querySelector('.shell'))", "authenticated application shell", 45_000);
  const identity = await evaluate("(async()=>{const r=await fetch('/api/auth/me');const u=r.ok?await r.json():null;return {status:r.status,role:u?.role}})()");
  if (identity.status !== 200) throw new Error("Synthetic account authentication failed.");
  return identity.role;
}
async function signOut() {
  if (!await evaluate(`(()=>{const b=document.querySelector('button[aria-label="Sign out"]');if(!b)return false;b.click();return true})()`)) throw new Error("Synthetic account could not sign out.");
  await waitFor("Boolean(document.querySelector('form.login-form'))", "signed-out login form");
}
async function openProfiles() {
  if (!await clickText("Agent profiles", "button.nav-item")) throw new Error("Agent profiles navigation is unavailable.");
  await waitFor("document.querySelector('main h1')?.textContent.trim()==='Agent profiles'", "agent profiles screen");
}
async function apiRequest(pathname, method = "GET", body) {
  return evaluate(`(async()=>{const r=await fetch(${JSON.stringify(pathname)},{method:${JSON.stringify(method)},credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'},${body === undefined ? "" : `body:JSON.stringify(${JSON.stringify(body)}),`}});let data=null;try{data=await r.json()}catch{}return {status:r.status,data}})()`);
}

try {
  chrome = spawn(browserPath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profile, "DevToolsActivePort"); const startBy = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < startBy) await delay(100);
  if (!existsSync(portFile)) throw new Error("Isolated C14 Chromium did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!target?.webSocketDebuggerUrl) throw new Error("Chromium page target is unavailable.");
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method === "Network.responseReceived") {
      const url = message.params.response.url;
      if (url.includes("/api/agents")) apiResponses.push({ path: new URL(url).pathname, status: message.params.response.status });
    }
    const item = pending.get(message.id); if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error(`Chrome DevTools ${item.method} failed.`)) : item.resolve(message.result);
  });
  await command("Page.enable"); await command("Runtime.enable"); await command("Network.enable");
  await command("Emulation.setDeviceMetricsOverride", { width: 1365, height: 1000, deviceScaleFactor: 1, mobile: false });
  await command("Page.navigate", { url: base.href });

  if (await signIn(admin) !== "admin") throw new Error("Synthetic Admin role did not authenticate.");
  await openProfiles();
  let profileResponse = await apiRequest("/api/agents");
  let agentProfile = profileResponse.data?.agents?.find((item) => item.name === profileName && item.department_group_id === groupId && item.enabled);
  if (!agentProfile) {
    await waitFor("Boolean(document.querySelector('#department-agent-profile-form'))", "Admin agent profile form");
    const filled = await evaluate(`(()=>{const f=document.querySelector('#department-agent-profile-form');const n=f?.querySelector('input');const selects=[...(f?.querySelectorAll('select')??[])];if(!f||!n||selects.length<3)return false;const set=(e,v,proto)=>{Object.getOwnPropertyDescriptor(proto,'value').set.call(e,v);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}))};set(n,${JSON.stringify(profileName)},HTMLInputElement.prototype);set(selects[0],${JSON.stringify(groupId)},HTMLSelectElement.prototype);set(selects[1],'knowledge_research',HTMLSelectElement.prototype);set(selects[2],'requester_acl',HTMLSelectElement.prototype);return true})()`);
    if (!filled || !await clickText("Create agent profile")) throw new Error("Admin could not create the synthetic queue profile.");
    await waitFor("Boolean(document.querySelector('.banner-success'))", "saved Admin profile notice", 30_000);
    profileResponse = await apiRequest("/api/agents");
    agentProfile = profileResponse.data?.agents?.find((item) => item.name === profileName && item.department_group_id === groupId && item.enabled);
  }
  if (profileResponse.status !== 200 || !agentProfile || agentProfile.tool_policy.indexOf("knowledge_search") < 0) throw new Error("The department-scoped profile is not runnable.");
  await signOut();

  if (await signIn(employee) !== "employee") throw new Error("Synthetic Finance Employee role did not authenticate.");
  await openProfiles();
  await waitFor(`Boolean([...document.querySelectorAll('.automation-item')].find(row=>row.textContent.includes(${JSON.stringify(profileName)})))`, "department-visible queue profile");
  const before = await apiRequest(`/api/agents/${agentProfile.id}/runs`);
  if (before.status !== 200) throw new Error("Employee cannot read the scoped run history.");
  const priorIds = before.data.runs.map((run) => run.run_id);
  const selected = await evaluate(`(()=>{const row=[...document.querySelectorAll('.automation-item')].find(item=>item.textContent.includes(${JSON.stringify(profileName)}));const b=[...(row?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Run profile');if(!b)return false;b.click();return true})()`);
  if (!selected) throw new Error("Finance Employee could not select the current department profile.");
  await waitFor("Boolean(document.querySelector('.agent-runner textarea'))", "agent run form");
  const task = "How often must recovery tokens be rotated?";
  const submitted = await evaluate(`(()=>{const input=document.querySelector('.agent-runner textarea');const form=input?.closest('form');const button=[...(form?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Queue agent run');if(!input||!button)return false;Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(input,${JSON.stringify(task)});input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));button.click();return true})()`);
  if (!submitted) throw new Error("The current UI did not submit an internal run to its durable queue.");

  let current; const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    const history = await apiRequest(`/api/agents/${agentProfile.id}/runs`);
    current = history.data?.runs?.find((run) => !priorIds.includes(run.run_id));
    if (current && ["completed", "failed"].includes(current.status)) break;
    await delay(750);
  }
  if (!current || current.status !== "completed" || !current.result) throw new Error("The agent worker did not complete the queued run within 90 seconds.");
  if (current.attempts !== 1 || !current.result.citations?.some((citation) => citation.document_id === sourceEvidence.documentId)) throw new Error("The queued result did not retain its authorized source citation.");
  await waitFor("Boolean(document.querySelector('.card.agent-runner .research-finding h3')?.textContent.trim()==='Run completed')", "completed queued result in UI", 30_000);

  await signOut();
  if (await signIn(outsider) !== "employee") throw new Error("Synthetic unassigned Employee role did not authenticate.");
  const outsiderRun = await apiRequest(`/api/agents/runs/${current.run_id}`);
  if (outsiderRun.status !== 403) throw new Error("An unassigned employee could read another department's queued run.");
  await signOut();
  if (await signIn(employee) !== "employee") throw new Error("Finance Employee could not restore its review session.");
  await openProfiles();
  const rowReload = await evaluate(`(()=>{const row=[...document.querySelectorAll('.automation-item')].find(item=>item.textContent.includes(${JSON.stringify(profileName)}));const b=[...(row?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Run profile');if(!b)return false;b.click();return true})()`);
  if (!rowReload) throw new Error("Saved agent profile was missing after page reload.");
  await waitFor(`Boolean([...document.querySelectorAll('.agent-run-history .automation-item')].find(item=>item.textContent.includes(${JSON.stringify(current.run_id)})))`, "durable completed run after navigation", 30_000);

  await command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true, screenWidth: 390, screenHeight: 844 });
  await delay(250);
  const mobile = await evaluate("({viewport:document.documentElement.clientWidth,root:document.documentElement.scrollWidth,body:document.body.scrollWidth})");
  if (mobile.viewport !== 390 || mobile.root > 390 || mobile.body > 390) throw new Error("C14 run history overflows the 390px mobile layout.");
  const screenshot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"));

  const sourceFiles = ["app/api/routes/agents.py", "app/services/agent_worker.py", "app/services/agents.py", "app/db/models.py", "app/main.py", "app/cli.py", "frontend/src/components/AgentCatalogPage.tsx", "frontend/src/api.ts", "frontend/src/types.ts", "migrations/versions/0064_durable_agent_run_queue.py"];
  const sourceSha256 = Object.fromEntries(sourceFiles.map((file) => [file, createHash("sha256").update(readFileSync(file)).digest("hex")]));
  const evidence = {
    result: "PASS", reviewUrl: base.href, apiHealthUrl: "http://127.0.0.1:63014/health/ready",
    database: "infraspine_c11dept_20261029_test", migration: "0064_durable_agent_run_queue",
    providerMode: "mock mode with pinned local MiniLM retrieval; no external provider called",
    profileId: agentProfile.id, departmentId: groupId, runId: current.run_id,
    queueStatus: current.status, attempts: current.attempts, retryAvailable: current.retry_available,
    citation: current.result.citations.map(({ source_id, document_id, filename, page_number, chunk_index }) => ({ source_id, document_id, filename, page_number, chunk_index })),
    outsiderRunStatus: outsiderRun.status, mobile390: mobile,
    screenshot: path.relative(process.cwd(), screenshotPath).replaceAll("\\", "/"),
    apiStatuses: apiResponses, sourceSha256ByFile: sourceSha256,
  };
  writeFileSync(path.join(evidenceDir, "c14-durable-agent-queue-browser.json"), JSON.stringify(evidence, null, 2) + "\n", { mode: 0o600 });
  const apiHealth = await fetch("http://127.0.0.1:63014/health/ready"); const uiHealth = await fetch(base);
  if (!apiHealth.ok || !uiHealth.ok) throw new Error("C14 passed, but the current-source UI/API did not remain healthy.");
  console.log("C14_CURRENT_SOURCE_DURABLE_AGENT_QUEUE_BROWSER=PASS; Admin profile; employee queues, polls and reads cited run; outsider denied; mobile 390px");
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (chrome && chrome.exitCode === null) { chrome.kill(); await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3_000)]); }
  const safeTmp = path.resolve(tmp); const safeProfile = path.resolve(profile);
  if (safeProfile.startsWith(`${safeTmp}${path.sep}`) && path.basename(safeProfile).startsWith("browser-c14-agent-queue-")) {
    await import("node:fs/promises").then(({ rm }) => rm(safeProfile, { recursive: true, force: true }));
  }
}
