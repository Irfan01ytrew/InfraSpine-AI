#!/usr/bin/env node
/** Current-source browser check for the private assignment reminder email status. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const url = new URL(process.env.INFRASPINE_F10_EMAIL_URL ?? "http://127.0.0.1:5178/admin/");
const email = process.env.INFRASPINE_F10_EMAIL_USER;
const password = process.env.INFRASPINE_F10_EMAIL_PASSWORD;
const profileRoot = process.env.INFRASPINE_F10_EMAIL_PROFILE_ROOT;
const evidenceRoot = process.env.INFRASPINE_F10_EMAIL_EVIDENCE;
if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(url.hostname)) {
  throw new Error("F10 browser acceptance is restricted to loopback HTTP.");
}
if (!email || !password || !profileRoot || !evidenceRoot) {
  throw new Error("F10 browser acceptance configuration is incomplete.");
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
const stamp = new Date().toISOString().replace(/[-:.]/g, "");
const profile = path.join(profileRoot, `f10-email-${stamp}`);
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
    await delay(200);
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
  const response = await call("Runtime.evaluate", {
    expression, awaitPromise: true, returnByValue: true, userGesture: true,
  }, timeoutMs);
  if (response.exceptionDetails) throw new Error("Browser evaluation failed.");
  return response.result?.value;
}

try {
  const activePortPath = path.join(profile, "DevToolsActivePort");
  await waitFor(() => existsSync(activePortPath), "Chrome DevTools endpoint");
  const [port] = readFileSync(activePortPath, "utf8").trim().split(/\r?\n/);
  const targetResponse = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" });
  const target = await targetResponse.json();
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    const item = pending.get(message.id);
    if (item) {
      clearTimeout(item.timer);
      pending.delete(message.id);
      if (message.error) item.reject(new Error("Browser protocol call failed."));
      else item.resolve(message.result);
    }
  });
  await call("Page.enable");
  await call("Runtime.enable");
  await call("Page.navigate", { url: url.href });
  try {
    await waitFor(async () => evaluate("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))"), "sign-in form");
  } catch {
    const pageState = await evaluate("({href:location.href,title:document.title,state:document.readyState,text:(document.body?.innerText??'').slice(0,160)})");
    throw new Error(`Sign-in UI did not load: ${JSON.stringify(pageState)}`);
  }
  const signedIn = await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const password=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!password||!form)return false;const set=(el,value)=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))};set(email,${JSON.stringify(email)});set(password,${JSON.stringify(password)});form.requestSubmit();return true})()`);
  if (!signedIn) throw new Error("Sign-in form was unavailable.");
  await waitFor(async () => evaluate("(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;return (await r.json()).role==='user'})()"), "Employee authentication", 40_000);
  await waitFor(async () => evaluate("[...document.querySelectorAll('button.nav-item')].some(item=>item.textContent.trim()==='My work')"), "My work navigation");
  const opened = await evaluate(`(()=>{const button=[...document.querySelectorAll('button.nav-item')].find(item=>item.textContent.trim()==='My work');button?.click();return !!button})()`);
  if (!opened) throw new Error("My work navigation was unavailable.");
  await waitFor(async () => evaluate("[...document.querySelectorAll('h1')].some(item=>item.textContent.trim()==='My work')"), "My work page");
  await waitFor(async () => evaluate("!document.querySelector('.loading')"), "My work data");
  const apiResult = await evaluate(`(async()=>{const r=await fetch('/api/personal-tasks/reminders',{credentials:'same-origin',cache:'no-store'});return {status:r.status,body:await r.json()}})()`);
  if (apiResult.status !== 200 || !apiResult.body.reminders.some((item) => item.email_status === "sent")) {
    throw new Error("The authenticated reminder API did not return sent delivery status.");
  }
  const browserText = await evaluate("document.querySelector('[aria-label=\"Assignment deadline reminders\"]')?.innerText ?? ''");
  if (!browserText.includes("Email sent")) throw new Error("My Work did not render the email delivery status.");
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  const mobile = await evaluate("({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,text:document.querySelector('[aria-label=\"Assignment deadline reminders\"]')?.innerText??''})");
  if (mobile.width !== 390 || mobile.scrollWidth !== 390 || !mobile.text.includes("Email sent")) {
    throw new Error("The mobile My Work reminder layout failed its bounded-width check.");
  }
  const screenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const screenshotPath = path.join(evidenceRoot, `f10-email-status-${stamp}.png`);
  writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"));
  console.log("F10_EMAIL_API_RECIPIENT_STATUS=PASS");
  console.log("F10_EMAIL_MY_WORK_DELIVERY_STATUS=PASS");
  console.log("F10_EMAIL_MOBILE_390PX=PASS");
  console.log(`F10_EMAIL_BROWSER_ACCEPTANCE=PASS (${path.basename(screenshotPath)})`);
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
    catch { console.log("F10_BROWSER_PROFILE_CLEANUP_PENDING=true"); }
  }
}
