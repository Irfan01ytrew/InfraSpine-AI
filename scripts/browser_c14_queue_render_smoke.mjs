#!/usr/bin/env node
/** Read-only visual regression check for the saved agent queue result and C14 text. */
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = "http://127.0.0.1:5186/admin/";
const root = process.env.INFRASPINE_C14_REVIEW_ROOT;
const fixturePath = process.env.INFRASPINE_C14_REVIEW_FIXTURE;
const previous = JSON.parse(readFileSync(path.join(root ?? "", "evidence", "c14-durable-agent-queue-browser.json"), "utf8"));
if (!root || !fixturePath || previous.result !== "PASS" || previous.queueStatus !== "completed") throw new Error("A saved C14 queue result is required for this read-only rendering check.");
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const employee = fixture.finance_employee;
const chromePath = ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe")].find((item) => item && existsSync(item));
if (!chromePath) throw new Error("Chrome is unavailable for the read-only rendering check.");

const tmp = path.join(root, "tmp"); const evidenceDir = path.join(root, "evidence");
mkdirSync(tmp, { recursive: true }); mkdirSync(evidenceDir, { recursive: true });
const profile = mkdtempSync(path.join(tmp, "browser-c14-render-"));
const screenshotPath = path.join(evidenceDir, "c14-agent-queue-employee-390-after-copy.png");
let chrome; let socket; let sequence = 0; const pending = new Map();
function command(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => { const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser timed out at ${method}.`)); }, 20_000); pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params })); });
}
async function evaluate(expression) {
  const response = await command("Runtime.evaluate", { expression: `Promise.resolve().then(()=>(${expression})).then(value=>JSON.stringify(value))`, awaitPromise: true, returnByValue: true, userGesture: true });
  if (response.exceptionDetails) throw new Error("Read-only browser evaluation failed.");
  return JSON.parse(response.result?.value ?? "null");
}
async function waitFor(expression, label) {
  const deadline = Date.now() + 35_000;
  while (Date.now() < deadline) { if (await evaluate(expression)) return; await delay(125); }
  throw new Error(`Timed out waiting for ${label}.`);
}
try {
  chrome = spawn(chromePath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profile, "DevToolsActivePort"); const startBy = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < startBy) await delay(100);
  if (!existsSync(portFile)) throw new Error("Isolated Chromium did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!target?.webSocketDebuggerUrl) throw new Error("Isolated Chromium page is unavailable.");
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => { const message = JSON.parse(event.data); const item = pending.get(message.id); if (!item) return; clearTimeout(item.timer); pending.delete(message.id); message.error ? item.reject(new Error("Chrome DevTools call failed.")) : item.resolve(message.result); });
  await command("Page.enable"); await command("Runtime.enable"); await command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true, screenWidth: 390, screenHeight: 844 });
  await command("Page.navigate", { url: base });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "login form");
  const login = await evaluate(`(()=>{const e=document.querySelector('form.login-form input[name="email"]'),p=document.querySelector('form.login-form input[name="password"]'),f=document.querySelector('form.login-form');if(!e||!p||!f)return false;for(const [x,v] of [[e,${JSON.stringify(employee.email)}],[p,${JSON.stringify(employee.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(x,v);x.dispatchEvent(new Event('input',{bubbles:true}));x.dispatchEvent(new Event('change',{bubbles:true}))}f.requestSubmit();return true})()`);
  if (!login) throw new Error("Synthetic Employee login failed.");
  await waitFor("Boolean(document.querySelector('.shell'))", "authenticated Employee shell");
  if (!await evaluate(`(()=>{const b=[...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.trim()==='Agent profiles');if(!b)return false;b.click();return true})()`)) throw new Error("Agent profile navigation is unavailable.");
  await waitFor("document.querySelector('main h1')?.textContent.trim()==='Agent profiles'", "Agent profiles screen");
  await waitFor("Boolean([...document.querySelectorAll('.automation-item')].find(x=>x.textContent.includes('Queued knowledge procedure'))) ", "saved department profile");
  const opened = await evaluate(`(()=>{const row=[...document.querySelectorAll('.automation-item')].find(x=>x.textContent.includes('Queued knowledge procedure'));const b=[...(row?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Run profile');if(!b)return false;b.click();return true})()`);
  if (!opened) throw new Error("The saved queue profile is unavailable.");
  await waitFor(`Boolean([...document.querySelectorAll('.agent-run-history .automation-item')].find(x=>x.textContent.includes(${JSON.stringify(previous.runId)})))`, "saved completed run history");
  const render = await evaluate(`(()=>{const page=document.body.innerText;const row=[...document.querySelectorAll('.agent-run-history .automation-item')].find(x=>x.textContent.includes(${JSON.stringify(previous.runId)}));return {warningTextCorrect:document.querySelector('.agent-catalog-note')?.textContent.includes("employee's document ACL")===true,noMojibake:!/(â€™|Â·|â€¦)/.test(page),profileTextCorrect:page.includes('Profile version 1 · C11 Finance'),savedRunVisible:Boolean(row),savedRunCompleted:row?.textContent.includes('completed')===true,answerVisible:row?.textContent.includes('rotated every 90 days')===true,root:document.documentElement.scrollWidth,body:document.body.scrollWidth,viewport:document.documentElement.clientWidth}})()`);
  if (!render.warningTextCorrect || !render.noMojibake || !render.profileTextCorrect || !render.savedRunVisible || !render.savedRunCompleted || !render.answerVisible || render.root > 390 || render.body > 390) throw new Error("The saved C14 result or corrected UI text did not render cleanly.");
  await evaluate(`(()=>{[...document.querySelectorAll('.agent-run-history .automation-item')].find(x=>x.textContent.includes(${JSON.stringify(previous.runId)}))?.scrollIntoView({block:'center'});return true})()`);
  const shot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(screenshotPath, Buffer.from(shot.data, "base64"));
  const files = ["frontend/src/components/AgentCatalogPage.tsx"];
  const sourceSha256 = Object.fromEntries(files.map((file) => [file, createHash("sha256").update(readFileSync(file)).digest("hex")]));
  const evidence = { result: "PASS", reviewUrl: base, runId: previous.runId, readOnly: true, checks: render, screenshot: path.relative(process.cwd(), screenshotPath).replaceAll("\\", "/"), sourceSha256ByFile: sourceSha256 };
  writeFileSync(path.join(evidenceDir, "c14-queue-render-smoke.json"), JSON.stringify(evidence, null, 2) + "\n", { mode: 0o600 });
  console.log("C14_QUEUE_SAVED_RESULT_RENDER=PASS; corrected copy, durable answer and mobile 390px; no new run created");
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (chrome && chrome.exitCode === null) { chrome.kill(); await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3_000)]); }
  const safeTmp = path.resolve(tmp); const safeProfile = path.resolve(profile);
  if (safeProfile.startsWith(`${safeTmp}${path.sep}`) && path.basename(safeProfile).startsWith("browser-c14-render-")) await import("node:fs/promises").then(({ rm }) => rm(safeProfile, { recursive: true, force: true }));
}
