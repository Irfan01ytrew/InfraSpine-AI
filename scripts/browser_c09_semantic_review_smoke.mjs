#!/usr/bin/env node
/** Browser smoke for the isolated current-source C09 review app; credentials stay in the protected fixture. */
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_C09_SMOKE_URL ?? "http://127.0.0.1:5179/admin/");
const fixturePath = process.env.INFRASPINE_C09_SMOKE_FIXTURE;
const reviewRoot = process.env.INFRASPINE_C09_SMOKE_ROOT;
if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || !fixturePath || !reviewRoot) {
  throw new Error("C09 browser smoke requires its loopback URL and protected synthetic fixture.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const admin = fixture.admin;
if (!admin?.email || !admin?.password) throw new Error("The protected C09 Admin fixture is incomplete.");
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for C09 browser smoke.");

const profile = mkdtempSync(path.join(reviewRoot, "tmp", "browser-c09-"));
const evidenceDir = path.join(reviewRoot, "evidence");
mkdirSync(evidenceDir, { recursive: true });
const screenshotPath = path.join(evidenceDir, "c09-current-source-review-390.png");
let chrome;
let socket;
let sequence = 0;
const pending = new Map();

function command(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser timed out at ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 20_000) {
  const response = await command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (response.exceptionDetails) throw new Error("A C09 review app browser action failed.");
  return response.result?.value;
}
async function waitFor(expression, label, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(120);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}

try {
  chrome = spawn(browserPath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < deadline) await delay(100);
  if (!existsSync(portFile)) throw new Error("Isolated Chrome did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!page?.webSocketDebuggerUrl) throw new Error("Isolated Chrome did not expose a page target.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error("Chrome DevTools command failed.")) : item.resolve(message.result);
  });
  await command("Page.enable");
  await command("Runtime.enable");
  await command("Page.navigate", { url: base.href });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "current-source login form");
  const submitted = await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const pass=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!pass||!form)return false;for(const [el,value] of [[email,${JSON.stringify(admin.email)}],[pass,${JSON.stringify(admin.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
  if (!submitted) throw new Error("Synthetic Admin login form was unavailable.");
  await waitFor("Boolean(document.querySelector('.shell')||document.querySelector('.first-run-setup h1'))", "authenticated Admin page", 40_000);
  const completedSetup = await evaluate("Boolean(document.querySelector('.first-run-setup h1')?.textContent.trim()==='Company setup')");
  if (completedSetup) {
    await waitFor("Boolean(document.querySelector('form.deployment-form input:not([type])'))", "loaded company setup form");
    const saved = await evaluate(`(()=>{const form=document.querySelector('form.deployment-form');const textInputs=[...(form?.querySelectorAll('input:not([type]),input[type="text"]')??[])];if(!form||textInputs.length<3)return false;for(const [el,value] of [[textInputs[0],'C09 Semantic Review Fixture'],[textInputs[1],'Synthetic QA'],[textInputs[2],'InfraSpine QA']]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
    if (!saved) throw new Error("Synthetic company setup form was unavailable.");
  }
  await waitFor("Boolean(document.querySelector('.shell'))", "authenticated current-source shell", 40_000);
  const adminIdentity = await evaluate("(async()=>{const response=await fetch('/api/auth/me');const data=response.ok?await response.json():null;return {status:response.status,role:data?.role}})()");
  if (adminIdentity?.status !== 200 || adminIdentity.role !== "admin") throw new Error("Current-source Admin session failed verification.");
  const navClicked = await evaluate(`(()=>{const button=[...document.querySelectorAll('button.nav-item')].find((item)=>item.textContent.trim()==='Approvals');if(!button)return false;button.click();return true})()`);
  if (!navClicked) throw new Error("Approvals navigation was unavailable in the current-source app.");
  await waitFor("document.querySelector('h1')?.textContent.trim()==='Approvals'", "Approvals page");
  await waitFor("!document.querySelector('.loading')", "loaded Approvals view");
  await command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await delay(300);
  const dimensions = await evaluate("({viewport:document.documentElement.clientWidth,body:document.body.clientWidth,root:document.documentElement.scrollWidth})");
  if (!dimensions || dimensions.viewport !== 390 || dimensions.body > 390) throw new Error("Approvals page exceeded the 390px mobile body width.");
  const screenshot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"));
  const sourceFiles = ["app/rag/conflict_detection.py", "app/rag/embeddings.py", "app/services/knowledge.py", "app/api/routes/knowledge.py", "frontend/src/components/ApprovalsPage.tsx"];
  const evidence = {
    result: "PASS",
    reviewUrl: base.href,
    apiUrl: "http://127.0.0.1:63008",
    database: "infraspine_c09semanticreview_20261028_test",
    migration: "current Alembic head",
    browser: "isolated headless Chromium",
    checks: ["synthetic Admin login", ...(completedSetup ? ["first-run company setup saved"] : []), "authenticated /api/auth/me role", "Approvals page render", "390px mobile body width"],
    adminRole: adminIdentity.role,
    mobile390: dimensions,
    screenshot: path.relative(process.cwd(), screenshotPath).replaceAll("\\", "/"),
    sourceSha256: Object.fromEntries(sourceFiles.map((file) => [file, createHash("sha256").update(readFileSync(file)).digest("hex")])),
  };
  writeFileSync(path.join(evidenceDir, "c09-current-source-review-app.json"), JSON.stringify(evidence, null, 2) + "\n", { mode: 0o600 });
  console.log("C09_CURRENT_SOURCE_REVIEW_BROWSER=PASS; Admin session; Approvals UI; mobile 390px");
} finally {
  socket?.close();
  if (chrome && chrome.exitCode === null) { chrome.kill(); await new Promise((resolve) => chrome.once("exit", resolve)); }
  try { await import("node:fs/promises").then(({ rm }) => rm(profile, { recursive: true, force: true })); } catch { /* only this harness's own temporary profile */ }
}
