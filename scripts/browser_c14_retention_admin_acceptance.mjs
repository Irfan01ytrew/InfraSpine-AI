#!/usr/bin/env node
/** Current-source admin browser acceptance for explicit agent-run content retention. */
import { createHash } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const reviewUrl = new URL(process.env.INFRASPINE_RETENTION_REVIEW_URL ?? "");
const apiHealthUrl = process.env.INFRASPINE_RETENTION_API_HEALTH_URL;
const fixturePath = path.resolve(process.env.INFRASPINE_RETENTION_QA_FIXTURE ?? "");
const evidenceDir = path.resolve(process.env.INFRASPINE_RETENTION_EVIDENCE_DIR ?? "");
const python = process.env.INFRASPINE_QA_PYTHON;
if (reviewUrl.protocol !== "http:" || reviewUrl.hostname !== "127.0.0.1" || !apiHealthUrl || !existsSync(fixturePath) || !evidenceDir || !python) {
  throw new Error("Retention acceptance requires explicit loopback URLs and protected local QA paths.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
if (!fixture.admin?.email || !fixture.admin?.password) throw new Error("Protected synthetic Admin fixture is incomplete.");
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find(candidate => candidate && existsSync(candidate));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for isolated browser acceptance.");

mkdirSync(evidenceDir, { recursive: true });
const tempRoot = path.join(evidenceDir, "tmp");
mkdirSync(tempRoot, { recursive: true });
const profileDir = path.join(tempRoot, `retention-${Date.now()}`);
if (existsSync(profileDir)) throw new Error("The isolated browser profile already exists.");
mkdirSync(profileDir, { recursive: false });
const sourceFiles = [
  "app/api/routes/admin_agent_runs.py",
  "app/services/agent_run_retention.py",
  "migrations/versions/0067_agent_run_content_retention.py",
  "frontend/src/components/AgentCatalogPage.tsx",
  "frontend/src/components/AgentRunRetentionPanel.tsx",
  "frontend/src/api.ts",
  "frontend/src/types.ts",
  "frontend/src/styles.css",
];
const observedStatuses = [];
const facts = {};
let chrome;
let socket;
let commandSequence = 0;
const pending = new Map();

function call(method, params = {}, timeoutMs = 25_000) {
  const id = ++commandSequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Chromium timed out during ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 25_000) {
  const result = await call("Runtime.evaluate", { expression: `Promise.resolve().then(()=>(${expression})).then(value=>JSON.stringify(value))`, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("A retention browser action raised an exception.");
  const value = result.result?.value;
  return value === undefined ? undefined : JSON.parse(value);
}
async function waitFor(expression, label, timeoutMs = 35_000) {
  const stop = Date.now() + timeoutMs;
  while (Date.now() < stop) {
    if (await evaluate(expression)) return;
    await delay(120);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function clickButton(label) {
  const selector = `(()=>{const b=[...document.querySelectorAll("button")].find(x=>x.textContent.trim()===${JSON.stringify(label)});if(!b||b.disabled)return false;b.click();return true})()`;
  if (!await evaluate(selector)) throw new Error(`Expected enabled button was unavailable: ${label}`);
}
async function setInput(label, value) {
  const result = await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(`input[aria-label="${label}"]`)});if(!e)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event("input",{bubbles:true}));e.dispatchEvent(new Event("change",{bubbles:true}));return true})()`);
  if (!result) throw new Error(`Expected accessible input was unavailable: ${label}`);
}
async function apiRequest(route, method, body) {
  const result = await evaluate(`(async()=>{const response=await fetch(${JSON.stringify(route)},{method:${JSON.stringify(method)},credentials:"same-origin",cache:"no-store",headers:{Accept:"application/json","Content-Type":"application/json","X-Requested-With":"InfraSpineAdmin"},body:JSON.stringify(${JSON.stringify(body)})});let data=null;try{data=await response.json()}catch{}return {status:response.status,data}})()`);
  observedStatuses.push({ method, path: route, status: result.status });
  return result;
}
async function previewCount(expected) {
  await clickButton("Preview eligible runs");
  await waitFor(`document.querySelector(".agent-retention-preview")?.innerText.includes(${JSON.stringify(`${expected} eligible run`)})`, `dry-run preview count ${expected}`);
  const text = await evaluate("document.querySelector('.agent-retention-preview')?.innerText");
  if (!new RegExp(`Dry-run preview: ${expected} eligible runs?\\b`).test(text)) throw new Error("The retention preview count did not match the synthetic database state.");
  return text;
}

try {
  const uiHealth = await fetch(reviewUrl);
  const apiHealth = await fetch(apiHealthUrl);
  if (!uiHealth.ok || !apiHealth.ok) throw new Error("The isolated review UI/API is not healthy before the browser journey.");

  chrome = spawn(browserPath, [
    "--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profileDir}`, "about:blank",
  ], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profileDir, "DevToolsActivePort");
  const startBy = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < startBy) await delay(100);
  if (!existsSync(portFile)) throw new Error("Isolated Chromium did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(item => item.type === "page");
  if (!target?.webSocketDebuggerUrl) throw new Error("Chromium page target is unavailable.");
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer);
    pending.delete(message.id);
    message.error ? item.reject(new Error(`Chrome DevTools ${item.method} failed.`)) : item.resolve(message.result);
  });
  await call("Page.enable");
  await call("Runtime.enable");
  await call("Page.navigate", { url: reviewUrl.href });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "synthetic Admin login form");
  const submitted = await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]'),password=document.querySelector('form.login-form input[name="password"]'),form=document.querySelector('form.login-form');if(!email||!password||!form)return false;for(const [input,value] of [[email,${JSON.stringify(fixture.admin.email)}],[password,${JSON.stringify(fixture.admin.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(input,value);input.dispatchEvent(new Event("input",{bubbles:true}));input.dispatchEvent(new Event("change",{bubbles:true}))}form.requestSubmit();return true})()`);
  if (!submitted) throw new Error("Synthetic Admin login could not be submitted.");
  await waitFor("Boolean(document.querySelector('.shell'))", "authenticated Admin shell");
  const identity = await evaluate("(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin'});const u=r.ok?await r.json():null;return {status:r.status,role:u?.role}})()");
  if (identity.status !== 200 || identity.role !== "admin") throw new Error("The protected synthetic account did not authenticate as Admin.");
  const opened = await evaluate(`(()=>{const b=[...document.querySelectorAll("button.nav-item")].find(x=>x.textContent.trim()==="Agent profiles");if(!b)return false;b.click();return true})()`);
  if (!opened) throw new Error("Agent profiles navigation was unavailable to the synthetic Admin.");
  await waitFor("document.querySelector('main h1')?.textContent.trim()==='Agent profiles'", "Agent profiles screen");
  await waitFor("Boolean(document.querySelector('#agent-retention-title'))", "Admin retention controls");

  const unique = Date.now().toString(36);
  const group = await apiRequest("/api/admin/access/groups", "POST", { name: `QA C14 Retention ${unique}`, kind: "department" });
  if (group.status !== 201 || !group.data?.id) throw new Error("Could not create the disposable synthetic department.");
  const profile = await apiRequest("/api/agents", "POST", {
    name: `QA retention agent ${unique}`,
    department_group_id: group.data.id,
    agent_type: "knowledge_research",
    knowledge_scope: "requester_acl",
    tool_policy: ["knowledge_search"],
  });
  if (profile.status !== 201 || !profile.data?.id) throw new Error("Could not create the disposable synthetic agent profile.");
  const seeded = spawnSync(python, ["scripts/seed_c14_retention_ui_runs.py", profile.data.id], { cwd: process.cwd(), encoding: "utf8", windowsHide: true });
  if (seeded.status !== 0) throw new Error("Could not create encrypted synthetic expired-run fixtures.");

  const initialPreview = await previewCount(2);
  facts.previewCount = 2;
  facts.previewIsDryRun = true;
  const purgeButton = "Purge up to 1 run";
  const disabledBeforePhrase = await evaluate(`(()=>{const section=document.querySelector('.agent-retention');const batch=section?.querySelector('input[aria-label="Batch size"]');if(!batch)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(batch,"1");batch.dispatchEvent(new Event("input",{bubbles:true}));batch.dispatchEvent(new Event("change",{bubbles:true}));return section.querySelector('button.btn-danger')?.disabled===true})()`);
  if (!disabledBeforePhrase) throw new Error("Purge was not disabled before typed confirmation.");
  await setInput("Type PURGE to confirm", "PURGE");
  await clickButton(purgeButton);
  await waitFor("document.querySelector('.banner-success[role=status]')?.innerText.includes('1 remain eligible')", "first bounded purge result");
  facts.firstBatchPurged = 1;
  facts.remainingAfterFirstBatch = 1;

  await previewCount(1);
  await setInput("Type PURGE to confirm", "PURGE");
  await clickButton(purgeButton);
  await waitFor("document.querySelector('.banner-success[role=status]')?.innerText.includes('0 remain eligible')", "second bounded purge result");
  facts.secondBatchPurged = 1;
  facts.remainingAfterSecondBatch = 0;
  const zeroPreview = await previewCount(0);
  facts.finalPreview = 0;
  facts.purgeButtonHiddenWhenEmpty = !(await evaluate("Boolean(document.querySelector('.agent-retention button.btn-danger'))"));
  if (!facts.purgeButtonHiddenWhenEmpty) throw new Error("Purge was offered while the preview contained zero eligible runs.");
  if (!zeroPreview.includes("0 eligible runs")) throw new Error("The final dry-run did not show an empty eligible set.");

  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  facts.mobileWidths = await evaluate("({viewport:document.documentElement.clientWidth,root:document.documentElement.scrollWidth,body:document.body.scrollWidth,controls:document.querySelectorAll('.agent-retention input,.agent-retention button').length})");
  if (facts.mobileWidths.viewport !== 390 || facts.mobileWidths.root !== 390 || facts.mobileWidths.body !== 390) throw new Error("Retention controls overflow the 390px viewport.");
  const screenshot = path.join(evidenceDir, "c14-agent-run-retention-admin-mobile.png");
  const screenshotResult = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  writeFileSync(screenshot, Buffer.from(screenshotResult.data, "base64"));

  const uiHealthAfter = await fetch(reviewUrl);
  const apiHealthAfter = await fetch(apiHealthUrl);
  if (!uiHealthAfter.ok || !apiHealthAfter.ok) throw new Error("The isolated review UI/API did not remain healthy after acceptance.");
  const sourceSha256 = Object.fromEntries(sourceFiles.map(file => [file, createHash("sha256").update(readFileSync(file)).digest("hex")]));
  const evidence = {
    result: "PASS",
    acceptance: "Admin-only retention UI dry-run, bounded typed-confirmation purge, repeat preview and empty-set gate",
    reviewUrl: reviewUrl.href,
    apiHealthUrl,
    database: process.env.DATABASE_URL?.split("/").at(-1),
    migration: "0067_agent_run_content_retention",
    accountRole: identity.role,
    previewCount: facts.previewCount,
    purgeDisabledBeforeTypedConfirmation: disabledBeforePhrase,
    firstBatchPurged: facts.firstBatchPurged,
    remainingAfterFirstBatch: facts.remainingAfterFirstBatch,
    secondBatchPurged: facts.secondBatchPurged,
    remainingAfterSecondBatch: facts.remainingAfterSecondBatch,
    finalPreview: facts.finalPreview,
    purgeButtonHiddenWhenEmpty: facts.purgeButtonHiddenWhenEmpty,
    mobileWidths: facts.mobileWidths,
    uiHealthStatus: uiHealthAfter.status,
    apiHealthStatus: apiHealthAfter.status,
    sourceSha256,
    apiStatuses: observedStatuses,
  };
  const evidencePath = path.join(evidenceDir, "c14-agent-run-retention-admin-browser.json");
  writeFileSync(evidencePath, `${JSON.stringify(evidence, null, 2)}\n`, { mode: 0o600 });
  console.log(`C14_RETENTION_ADMIN_BROWSER=PASS; evidence=${evidencePath}; preview=2; purged=1+1; remaining=0; UI=${uiHealthAfter.status}; API=${apiHealthAfter.status}; mobile=390px`);
} finally {
  if (socket?.readyState === WebSocket.OPEN) {
    try { await call("Browser.close", {}, 3_000); } catch { socket.close(); }
  }
  if (chrome && chrome.exitCode === null) {
    chrome.kill();
    await Promise.race([new Promise(resolve => chrome.once("exit", resolve)), delay(3_000)]);
  }
  const safeTemp = path.resolve(tempRoot);
  const safeProfile = path.resolve(profileDir);
  if (safeProfile.startsWith(`${safeTemp}${path.sep}`) && path.basename(safeProfile).startsWith("retention-")) {
    rmSync(safeProfile, { recursive: true, force: true });
  }
}
