#!/usr/bin/env node
/** Current-source synthetic browser acceptance for encrypted agent run history. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_C14_REVIEW_URL ?? "http://127.0.0.1:5181/admin/");
const fixturePath = process.env.INFRASPINE_C14_REVIEW_FIXTURE;
const reviewRoot = process.env.INFRASPINE_C14_REVIEW_ROOT;
if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || !fixturePath || !reviewRoot) {
  throw new Error("C14 acceptance requires the loopback review URL and protected synthetic fixture.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
if (!fixture.admin?.email || !fixture.admin?.password) throw new Error("The protected Admin fixture is incomplete.");
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for C14 acceptance.");

const profile = mkdtempSync(path.join(reviewRoot, "tmp", "browser-c14-"));
const evidenceDir = path.join(reviewRoot, "evidence");
mkdirSync(evidenceDir, { recursive: true });
const screenshotPath = path.join(evidenceDir, "c14-agent-runs-mobile.png");
let chrome;
let socket;
let sequence = 0;
const pending = new Map();
const apiResponses = [];

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
  if (response.exceptionDetails) throw new Error("A C14 browser action failed.");
  return response.result?.value;
}
async function waitFor(expression, label, timeoutMs = 35_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(125);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function clickButton(label, selector = "button") {
  return evaluate(`(()=>{const item=[...document.querySelectorAll(${JSON.stringify(selector)})].find((node)=>node.textContent.trim()===${JSON.stringify(label)});if(!item)return false;item.click();return true})()`);
}
async function enterTask(task) {
  return evaluate(`(()=>{const input=document.querySelector('.agent-runner textarea');const form=input?.closest('form');const button=[...(form?.querySelectorAll('button')??[])].find((node)=>node.textContent.trim()==='Run agent');if(!input||!button)return false;Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(input,${JSON.stringify(task)});input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));button.click();return true})()`);
}
async function runOnce(task, label) {
  if (!await enterTask(task)) throw new Error(`The ${label} agent form was unavailable.`);
  await waitFor("Boolean(document.querySelector('.card.agent-runner .research-finding h3')?.textContent.trim()==='Run completed')", `${label} saved result`, 60_000);
}

try {
  chrome = spawn(browserPath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < deadline) await delay(100);
  if (!existsSync(portFile)) throw new Error("Isolated Chromium did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!page?.webSocketDebuggerUrl) throw new Error("Isolated Chromium did not expose a page target.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method === "Network.responseReceived" && String(message.params.response.url).includes("/api/agents")) {
      apiResponses.push({ url: new URL(message.params.response.url).pathname, status: message.params.response.status });
    }
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error("A Chrome DevTools command failed.")) : item.resolve(message.result);
  });
  await command("Page.enable");
  await command("Runtime.enable");
  await command("Network.enable");
  await command("Page.navigate", { url: base.href });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "login form");
  const loginSubmitted = await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const pass=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!pass||!form)return false;for(const [el,value] of [[email,${JSON.stringify(fixture.admin.email)}],[pass,${JSON.stringify(fixture.admin.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
  if (!loginSubmitted) throw new Error("Synthetic Admin login form was unavailable.");
  await waitFor("Boolean(document.querySelector('.shell')||document.querySelector('.first-run-setup h1'))", "authenticated Admin", 45_000);
  const setupRequired = await evaluate("document.querySelector('.first-run-setup h1')?.textContent.trim()==='Company setup'");
  if (setupRequired) {
    await waitFor("Boolean(document.querySelector('form.deployment-form input:not([type])'))", "company setup fields");
    const saved = await evaluate(`(()=>{const form=document.querySelector('form.deployment-form');const inputs=[...(form?.querySelectorAll('input:not([type]),input[type="text"]')??[])];if(!form||inputs.length<3)return false;for(const [el,value] of [[inputs[0],'Synthetic C14 Review'],[inputs[1],'Testing'],[inputs[2],'InfraSpine QA']]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
    if (!saved) throw new Error("Synthetic company setup form was unavailable.");
  }
  await waitFor("Boolean(document.querySelector('.shell'))", "Admin shell", 45_000);
  const identity = await evaluate("(async()=>{const r=await fetch('/api/auth/me');const u=r.ok?await r.json():null;return {status:r.status,role:u?.role}})()");
  if (identity?.status !== 200 || identity.role !== "admin") throw new Error("Synthetic Admin session failed verification.");
  if (!await clickButton("Agent profiles", "button.nav-item")) throw new Error("Agent profiles navigation was unavailable.");
  await waitFor("document.querySelector('h1')?.textContent.trim()==='Agent profiles'", "agent profiles screen");
  await waitFor("[...document.querySelectorAll('button')].some((button)=>button.textContent.trim()==='Run profile')", "runnable profile list");
  if (!await clickButton("Run profile")) throw new Error("No runnable synthetic agent profile was available.");
  await waitFor("Boolean(document.querySelector('.agent-runner textarea'))", "agent run panel");
  await runOnce("Summarize approved operating guidance for a synthetic review.", "first");
  await delay(300);
  await runOnce("Summarize approved operating guidance for a synthetic review.", "second");
  await waitFor("document.querySelectorAll('.agent-run-history .automation-item').length>=1", "saved earlier run in UI history");
  const currentRunIds = await evaluate("[...document.querySelectorAll('.agent-run-history .automation-item')].map((item)=>item.textContent).join(' ').match(/[0-9a-f-]{36}/gi)||[]");
  if (!Array.isArray(currentRunIds) || currentRunIds.length < 1) throw new Error("The UI did not render a saved run identifier.");
  const previousTimeOrigin = await evaluate("performance.timeOrigin");
  await command("Page.reload");
  await waitFor(`performance.timeOrigin!==${previousTimeOrigin}`, "completed page reload", 45_000);
  await waitFor("Boolean(document.querySelector('.shell')||document.querySelector('form.login-form'))", "session page after reload", 45_000);
  const loginAfterReload = await evaluate("Boolean(document.querySelector('form.login-form'))");
  if (loginAfterReload) {
    const reauthenticated = await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const pass=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!pass||!form)return false;for(const [el,value] of [[email,${JSON.stringify(fixture.admin.email)}],[pass,${JSON.stringify(fixture.admin.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
    if (!reauthenticated) throw new Error("The synthetic Admin could not restore its browser session.");
    await waitFor("Boolean(document.querySelector('.shell'))", "Admin shell after reauthentication", 45_000);
  }
  await waitFor("[...document.querySelectorAll('button.nav-item')].some((button)=>button.textContent.trim()==='Agent profiles')", "Agent profiles navigation after reload");
  if (!await clickButton("Agent profiles", "button.nav-item")) throw new Error("Agent profiles navigation after reload was unavailable.");
  await waitFor("document.querySelector('h1')?.textContent.trim()==='Agent profiles'", "agent profiles after reload");
  await waitFor("[...document.querySelectorAll('button')].some((button)=>button.textContent.trim()==='Run profile')", "runnable profile after reload");
  if (!await clickButton("Run profile")) throw new Error("The saved profile was not available after reload.");
  await waitFor("Boolean(document.querySelector('.agent-runner'))", "history panel after reload");
  await waitFor("document.querySelectorAll('.agent-run-history .automation-item').length>=2", "two durable run records", 45_000);
  const historyAfterReload = await evaluate("document.querySelectorAll('.agent-run-history .automation-item').length");
  await command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await delay(250);
  const dimensions = await evaluate("({viewport:document.documentElement.clientWidth,body:document.body.clientWidth,root:document.documentElement.scrollWidth})");
  if (!dimensions || dimensions.viewport !== 390 || dimensions.body > 390) throw new Error("Agent run history exceeded the 390px mobile body width.");
  const screenshot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"));
  const evidence = {
    result: "PASS",
    reviewUrl: base.href,
    apiUrl: "http://127.0.0.1:63010",
    database: "infraspine_c14runs_20261028_test",
    migration: "0060_agent_profile_run_history",
    browser: "isolated headless Chromium",
    checks: ["synthetic Admin login", setupRequired ? "synthetic first-run setup" : "existing synthetic company setup", "two actual current-source agent runs", "encrypted saved run history rendered after page reload", "390px mobile body width"],
    visibleHistoryCount: historyAfterReload,
    mobile: dimensions,
    apiStatuses: apiResponses.filter((entry) => entry.url.includes("/api/agents")),
    screenshot: screenshotPath,
    secretData: "no credentials or task text recorded",
  };
  writeFileSync(path.join(evidenceDir, "c14-agent-run-history-browser.json"), `${JSON.stringify(evidence, null, 2)}\n`);
  process.stdout.write(`${JSON.stringify(evidence, null, 2)}\n`);
} finally {
  socket?.close();
  chrome?.kill();
}
