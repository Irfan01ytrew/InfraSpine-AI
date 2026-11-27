#!/usr/bin/env node
/** Focused authenticated browser check for Admin text-file import and partner retrieval. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const url = new URL(process.env.INFRASPINE_F12_UPLOAD_URL ?? "http://127.0.0.1:5178/admin/#/partner-workspaces");
const adminEmail = process.env.INFRASPINE_F12_ADMIN_USER;
const adminPassword = process.env.INFRASPINE_F12_ADMIN_PASSWORD;
const partnerPassword = process.env.INFRASPINE_F12_PARTNER_PASSWORD;
const profileRoot = process.env.INFRASPINE_F12_UPLOAD_PROFILE_ROOT;
const evidenceRoot = process.env.INFRASPINE_F12_UPLOAD_EVIDENCE;
if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(url.hostname)) {
  throw new Error("F12 file upload acceptance is restricted to loopback HTTP.");
}
if (!adminEmail || !adminPassword || !partnerPassword || !profileRoot || !evidenceRoot) {
  throw new Error("F12 file upload acceptance configuration is incomplete.");
}

const chrome = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((candidate) => candidate && existsSync(candidate));
if (!chrome) throw new Error("Chrome or Edge is unavailable for local browser acceptance.");

mkdirSync(profileRoot, { recursive: true });
mkdirSync(evidenceRoot, { recursive: true });
const stamp = Date.now().toString(36);
const profile = path.join(profileRoot, `f12-file-upload-${stamp}`);
const resolvedProfileRoot = path.resolve(profileRoot) + path.sep;
if (!path.resolve(profile).startsWith(resolvedProfileRoot)) throw new Error("Unsafe browser profile path.");
const browser = spawn(chrome, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank",
], { stdio: "ignore", windowsHide: true });
let socket;
const pending = new Map();
let sequence = 0;

async function waitFor(predicate, label, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await delay(180);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}

function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out during ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression, timeoutMs = 20_000) {
  const response = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (response.exceptionDetails) throw new Error("Browser evaluation failed.");
  return response.result?.value;
}

async function fill(selector, value) {
  return evaluate(`(()=>{const element=document.querySelector(${JSON.stringify(selector)});if(!element)return false;const proto=element instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(element,${JSON.stringify(value)});element.dispatchEvent(new Event('input',{bubbles:true}));element.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
}

try {
  const activePortPath = path.join(profile, "DevToolsActivePort");
  const start = Date.now() + 15_000;
  while (!existsSync(activePortPath) && Date.now() < start) await delay(100);
  if (!existsSync(activePortPath)) throw new Error("Chrome DevTools endpoint did not start.");
  const [debugPort] = readFileSync(activePortPath, "utf8").trim().split(/\r?\n/);
  let target;
  const targetDeadline = Date.now() + 15_000;
  while (!target && Date.now() < targetDeadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
      if (response.ok) target = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!target) await delay(100);
  }
  if (!target) throw new Error("Chrome page target did not become ready.");
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("Browser debugger connection failed.")), { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    if (message.error) item.reject(new Error("Browser protocol call failed.")); else item.resolve(message.result);
  });

  await call("Page.enable"); await call("Runtime.enable");
  await call("Page.navigate", { url: url.href });
  await waitFor(() => evaluate("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))"), "Admin login form");
  await fill('form.login-form input[name="email"]', adminEmail);
  await fill('form.login-form input[name="password"]', adminPassword);
  await evaluate("document.querySelector('form.login-form')?.requestSubmit()");
  await waitFor(() => evaluate("(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;return (await r.json()).role==='admin'})()"), "Admin authentication", 40_000);
  const setup = await evaluate(`(async()=>{const r=await fetch('/api/admin/deployment/company',{method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'},body:JSON.stringify({company_name:'F12 Upload Acceptance',industry:'Services',brand_name:'F12 Upload',primary_color:'#146B61',setup_complete:true})});return r.status})()`);
  if (setup !== 200) throw new Error("Could not finish synthetic first-run company setup for the browser fixture.");
  await evaluate("window.location.hash='#/partner-workspaces'");
  await call("Page.reload");
  await waitFor(() => evaluate("Boolean(document.querySelector('form.login-form input[name=\"email\"]')) || Boolean(document.querySelector('form.partner-admin-source-upload input[type=file]'))"), "review console reload");
  try {
    await waitFor(() => evaluate("Boolean(document.querySelector('form.partner-admin-source-upload input[type=file]'))"), "partner workspace source controls");
  } catch {
    const state = await evaluate("({hash:location.hash,heading:document.querySelector('header.page-header h1')?.textContent.trim()??null,partnerNav:[...document.querySelectorAll('nav button')].some((item)=>item.innerText.includes('External partner access')),uploadForms:document.querySelectorAll('form.partner-admin-source-upload').length})");
    throw new Error(`Partner source controls unavailable (${JSON.stringify(state)}).`);
  }

  const filename = `f12-service-hours-${stamp}.md`;
  const sourceText = "Northwind field service is available Monday through Friday from 09:15 to 17:45 UTC. Emergency dispatch uses extension 7314.";
  const attached = await evaluate(`(()=>{const input=document.querySelector('form.partner-admin-source-upload input[type=file]');if(!input)return false;const transfer=new DataTransfer();transfer.items.add(new File([${JSON.stringify(sourceText)}],${JSON.stringify(filename)},{type:'text/markdown'}));input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!attached) throw new Error("Partner source file input was not available.");
  await evaluate("document.querySelector('form.partner-admin-source-upload')?.requestSubmit()");
  await waitFor(() => evaluate(`[...document.querySelectorAll('.partner-source-list li')].some((item)=>item.innerText.includes(${JSON.stringify(filename.replace(/\.md$/, ""))}))`), "uploaded file in source list");
  const sourceSafe = await evaluate(`(()=>{const row=[...document.querySelectorAll('.partner-source-list li')].find((item)=>item.innerText.includes(${JSON.stringify(filename.replace(/\.md$/, ""))}));return Boolean(row && !row.innerText.includes(${JSON.stringify(sourceText)}))})()`);
  if (!sourceSafe) throw new Error("The Admin source list exposed uploaded content.");
  console.log("F12_PARTNER_FILE_UPLOAD_ADMIN_UI=PASS");

  const suffix = stamp;
  const email = `f12-upload-${suffix}@example.test`;
  await fill('.partner-admin-inline-form input[type="email"]', email);
  await evaluate("document.querySelector('.partner-admin-inline-form')?.requestSubmit()");
  await waitFor(() => evaluate("Boolean(document.querySelector('.partner-invite-link a[href*=\"/partner?token=\"]'))"), "one-time invitation link");
  const invitationHref = await evaluate("document.querySelector('.partner-invite-link a')?.href??''");
  const invitation = new URL(invitationHref);
  const partnerUrl = new URL(url.href); partnerUrl.hash = invitation.hash;
  await call("Page.navigate", { url: partnerUrl.href });
  await waitFor(() => evaluate("document.querySelector('h1')?.textContent.trim()==='Set up partner access'"), "partner invitation form");
  await fill('form.partner-auth-form input[name="password"]', partnerPassword);
  await fill('form.partner-auth-form input[name="confirm_password"]', partnerPassword);
  await evaluate("document.querySelector('form.partner-auth-form')?.requestSubmit()");
  await waitFor(() => evaluate("document.querySelector('h1')?.textContent.trim()==='Ask your workspace'"), "partner portal", 45_000);
  await fill("#partner-question", "When is Northwind field service available?");
  await evaluate("document.querySelector('form.partner-composer')?.requestSubmit()");
  await waitFor(() => evaluate("document.querySelectorAll('article.partner-exchange').length===1"), "file-backed answer", 45_000);
  const answer = await evaluate("document.querySelector('article.partner-exchange')?.innerText??''");
  if (!answer.includes("09:15 to 17:45 UTC") || !answer.includes(filename.replace(/\.md$/, "")) || !answer.includes("[S1]")) {
    throw new Error("The imported file did not produce a cited partner answer.");
  }
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  const mobile = await evaluate("({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})");
  if (mobile.width !== 390 || mobile.scrollWidth !== 390) throw new Error("Partner portal overflowed at 390px.");
  const screenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const screenshotPath = path.join(evidenceRoot, `f12-file-upload-${stamp}.png`);
  writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"));
  console.log("F12_PARTNER_FILE_CITED_ANSWER=PASS");
  console.log("F12_PARTNER_FILE_390PX=PASS");
  console.log(`F12_PARTNER_FILE_UPLOAD_BROWSER_ACCEPTANCE=PASS (${path.basename(screenshotPath)})`);
} catch (error) {
  console.error(`F12_PARTNER_FILE_UPLOAD_BROWSER_ACCEPTANCE=FAIL ${error?.message ?? "unknown error"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (browser && browser.exitCode === null) {
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, 5000);
      browser.once("exit", () => { clearTimeout(timer); resolve(); });
      browser.kill();
    });
  }
  const resolvedProfile = path.resolve(profile);
  if (resolvedProfile.startsWith(resolvedProfileRoot)) {
    try { rmSync(resolvedProfile, { recursive: true, force: true, maxRetries: 4, retryDelay: 500 }); }
    catch { console.log("F12_FILE_BROWSER_PROFILE_CLEANUP_PENDING=true"); }
  }
}
