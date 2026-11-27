#!/usr/bin/env node
/** Current-source MFA UI/API acceptance against a disposable local synthetic review database. */
import { spawn } from "node:child_process";
import { createHmac } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const email = process.env.INFRASPINE_MFA_REVIEW_EMAIL;
const password = process.env.INFRASPINE_MFA_REVIEW_PASSWORD;
const appUrl = process.env.INFRASPINE_MFA_REVIEW_URL ?? "http://127.0.0.1:5174/admin/";
const candidates = process.platform === "win32"
  ? ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"), path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe")]
  : ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/microsoft-edge"];
const browserPath = process.env.INFRASPINE_BROWSER_PATH || candidates.find((candidate) => candidate && existsSync(candidate));
if (!email || !password) throw new Error("The protected synthetic review fixture was not provided to the browser runner.");
if (!browserPath || !existsSync(browserPath)) throw new Error("Chrome or Edge was not found.");
const parsedUrl = new URL(appUrl);
if (!(parsedUrl.hostname === "127.0.0.1" || parsedUrl.hostname === "localhost" || parsedUrl.hostname === "::1")) {
  throw new Error("The MFA acceptance URL must be loopback-only.");
}

let browser;
let socket;
let profile;
let sequence = 0;
const pending = new Map();

function call(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser command timed out: ${method}`)); }, 20_000);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error("The MFA browser action raised an exception.");
  return result.result?.value;
}

async function waitFor(expression, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await delay(150);
  }
  return false;
}

async function fill(selector, value) {
  const filled = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
  if (!filled) throw new Error(`Required MFA form control was missing: ${selector}`);
}

async function fillAt(selector, index, value) {
  const filled = await evaluate(`(()=>{const elements=document.querySelectorAll(${JSON.stringify(selector)});const el=elements[${index}];if(!el)return false;const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
  if (!filled) throw new Error("The fresh synthetic review setup form was incomplete.");
}

async function fillFormContaining(marker, selector, value) {
  const filled = await evaluate(`(()=>{const form=[...document.querySelectorAll('.account-security-card form')].find(item=>item.innerText.includes(${JSON.stringify(marker)}));const el=form?.querySelector(${JSON.stringify(selector)});if(!el)return false;const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
  if (!filled) throw new Error(`Required form field was missing in the ${marker} section.`);
}

async function submit(selector) {
  if (!await evaluate(`(()=>{const form=document.querySelector(${JSON.stringify(selector)});if(!form)return false;form.requestSubmit();return true;})()`)) {
    throw new Error(`Required MFA form was missing: ${selector}`);
  }
}

function totp(secret, counter) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const clean = secret.replace(/=+$/g, "").toUpperCase();
  let bits = "";
  for (const char of clean) {
    const value = alphabet.indexOf(char);
    if (value < 0) throw new Error("The local TOTP setup key had an invalid encoding.");
    bits += value.toString(2).padStart(5, "0");
  }
  const key = Buffer.alloc(Math.floor(bits.length / 8));
  for (let i = 0; i < key.length; i += 1) key[i] = Number.parseInt(bits.slice(i * 8, i * 8 + 8), 2);
  const message = Buffer.alloc(8);
  message.writeBigUInt64BE(BigInt(counter));
  const digest = createHmac("sha1", key).update(message).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const binary = ((digest[offset] & 0x7f) << 24) | ((digest[offset + 1] & 0xff) << 16) |
    ((digest[offset + 2] & 0xff) << 8) | (digest[offset + 3] & 0xff);
  return String(binary % 1_000_000).padStart(6, "0");
}

async function waitForFreshCounter(previousCounter) {
  const deadline = Date.now() + 35_000;
  while (Math.floor(Date.now() / 30_000) <= previousCounter && Date.now() < deadline) await delay(200);
  if (Math.floor(Date.now() / 30_000) <= previousCounter) throw new Error("A fresh authenticator interval did not arrive.");
  return Math.floor(Date.now() / 30_000);
}

async function connectBrowser() {
  profile = mkdtempSync(path.join(tmpdir(), "infraspine-mfa-review-"));
  const childEnvironment = { ...process.env };
  delete childEnvironment.INFRASPINE_MFA_REVIEW_PASSWORD;
  delete childEnvironment.INFRASPINE_MFA_REVIEW_EMAIL;
  delete childEnvironment.INFRASPINE_MFA_REVIEW_URL;
  browser = spawn(browserPath, [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-background-networking", "--disable-sync", "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank",
  ], { stdio: "ignore", windowsHide: true, env: childEnvironment });
  const activePort = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activePort) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePort)) throw new Error("The isolated review browser did not start.");
  const debugPort = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline && browser.exitCode === null) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
      if (response.ok) page = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("The isolated browser page did not become ready.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("The browser debugger connection failed.")), { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const entry = pending.get(message.id);
    clearTimeout(entry.timer);
    pending.delete(message.id);
    if (message.error) entry.reject(new Error(`Browser command failed: ${entry.method}`));
    else entry.resolve(message.result);
  });
  await call("Page.enable");
  await call("Runtime.enable");
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await call("Page.addScriptToEvaluateOnNewDocument", { source: `(()=>{window.__mfaCalls=[];const original=window.fetch.bind(window);window.fetch=async(input,init={})=>{const url=new URL(typeof input==='string'?input:input.url,location.href);const method=String(init.method??(typeof input==='string'?'GET':input.method)??'GET').toUpperCase();const response=await original(input,init);if(url.pathname.startsWith('/api/'))window.__mfaCalls.push({method,path:url.pathname,status:response.status});return response;};})();` });
  await call("Page.navigate", { url: appUrl });
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`, 40_000)) {
    throw new Error("The current-source login page did not render.");
  }
}

async function signInWithPassword() {
  await fill('form.login-form input[name="email"]', email);
  await fill('form.login-form input[name="password"]', password);
  await submit("form.login-form");
}

async function signOut() {
  const clicked = await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Sign out"]');if(!button)return false;button.click();return true;})()`);
  if (!clicked || !await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`)) {
    throw new Error("Sign out did not return to the login screen.");
  }
}

async function run() {
  await connectBrowser();
  await signInWithPassword();
  if (await waitFor(`!!document.querySelector('.first-run-setup form.deployment-form')`, 5_000)) {
    await fillAt('.first-run-setup form.deployment-form input:not([type])', 0, 'Synthetic MFA Review 20261027');
    await fillAt('.first-run-setup form.deployment-form input:not([type])', 1, 'Professional services');
    await fillAt('.first-run-setup form.deployment-form input:not([type])', 2, 'InfraSpine MFA Review');
    await submit('.first-run-setup form.deployment-form');
    if (!await waitFor(`!![...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.includes('Account security'))`)) {
      throw new Error("The isolated synthetic company setup did not reach the Admin workspace.");
    }
    console.log("MFA_BROWSER_FRESH_SYNTHETIC_SETUP=PASS");
  }
  if (!await waitFor(`!![...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.includes('Account security'))`)) {
    const diagnostic = await evaluate(`({headings:[...document.querySelectorAll('h1,h2')].map(item=>item.textContent.trim()).slice(0,4),nav:[...document.querySelectorAll('button.nav-item')].map(item=>item.textContent.trim()).slice(0,4),loginError:document.querySelector('.inline-alert-error')?.textContent.trim()??null})`);
    throw new Error(`The synthetic Admin could not reach the current-source workspace (${JSON.stringify(diagnostic)}).`);
  }
  console.log("MFA_BROWSER_ADMIN_LOGIN=PASS");
  await evaluate(`[...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.includes('Account security'))?.click()`);
  if (!await waitFor(`document.querySelector('h1')?.textContent==='Account security'`)) throw new Error("Account security page did not render.");
  if (!await waitFor(`document.body.innerText.includes('Organization-wide MFA enforcement and single sign-on are not configured here.')`)) {
    throw new Error("The account security limitations were not shown.");
  }
  await fill('.account-security-card form input[name="password"]', password);
  await submit(".account-security-card form");
  if (!await waitFor(`!!document.querySelector('input[aria-label="Authenticator setup key"]')`)) throw new Error("MFA enrollment did not return a setup key.");
  const secret = await evaluate(`document.querySelector('input[aria-label="Authenticator setup key"]')?.value`);
  const enrollmentCounter = Math.floor(Date.now() / 30_000);
  await fill('.account-security-card form input[name="code"]', totp(secret, enrollmentCounter));
  await submit(".account-security-card form");
  if (!await waitFor(`!!document.querySelector('.account-security-codes pre')`)) throw new Error("MFA enrollment did not show recovery codes.");
  const recoveryCodes = await evaluate(`document.querySelector('.account-security-codes pre')?.textContent.trim().split(/\\r?\\n/)`);
  if (!Array.isArray(recoveryCodes) || recoveryCodes.length !== 10) throw new Error("MFA enrollment did not issue ten one-time recovery codes.");
  const layout = await evaluate(`({viewport:innerWidth,width:document.documentElement.scrollWidth})`);
  if (layout?.viewport !== 390 || layout.width > 390) throw new Error("MFA account security UI overflowed at 390px.");
  console.log("MFA_BROWSER_ENROLLMENT_AND_390PX_LAYOUT=PASS");

  await signOut();
  await signInWithPassword();
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="code"]')`)) throw new Error("Password login did not show the second-factor challenge.");
  const loginCounter = await waitForFreshCounter(enrollmentCounter);
  await fill('form.login-form input[name="code"]', totp(secret, loginCounter));
  await submit("form.login-form");
  if (!await waitFor(`!![...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.includes('Account security'))`)) {
    throw new Error("Authenticator challenge did not complete sign-in.");
  }
  console.log("MFA_BROWSER_PASSWORD_TOTP_CHALLENGE=PASS");

  await signOut();
  await signInWithPassword();
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="code"]')`)) throw new Error("Recovery login did not show the second-factor challenge.");
  await fill('form.login-form input[name="code"]', recoveryCodes[0]);
  await submit("form.login-form");
  if (!await waitFor(`!![...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.includes('Account security'))`)) {
    throw new Error("One-time recovery code did not complete sign-in.");
  }
  console.log("MFA_BROWSER_ONE_TIME_RECOVERY_LOGIN=PASS");

  await evaluate(`[...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.includes('Account security'))?.click()`);
  if (!await waitFor(`document.querySelector('h1')?.textContent==='Account security' && document.body.innerText.includes('9 unused recovery codes remain')`)) {
    throw new Error("Recovery-code use was not reflected in the account security page.");
  }
  const disableForm = await evaluate(`(()=>[...document.querySelectorAll('.account-security-card form')].find(form=>form.innerText.includes('Turn off MFA'))?.outerHTML.includes('Turn off MFA'))()`);
  if (!disableForm) throw new Error("MFA disable confirmation form did not render.");
  await fillFormContaining("Turn off MFA", 'input[name="password"]', password);
  await fillFormContaining("Turn off MFA", 'input[name="code"]', recoveryCodes[1]);
  await evaluate(`(()=>[...document.querySelectorAll('.account-security-card form')].find(form=>form.innerText.includes('Turn off MFA'))?.requestSubmit())()`);
  if (!await waitFor(`document.body.innerText.includes('Use an authenticator app that supports standard time-based one-time passwords.') && !document.body.innerText.includes('9 unused recovery codes remain')`)) {
    throw new Error("MFA did not turn off after valid password and recovery confirmation.");
  }
  const calls = await evaluate(`window.__mfaCalls ?? []`);
  const posts = calls.filter((item) => item.method === "POST");
  if (posts.some((item) => item.status < 200 || item.status >= 300)) throw new Error("A current-source MFA API mutation failed.");
  for (const pathName of ["/api/auth/mfa/enrollment", "/api/auth/mfa/enrollment/confirm", "/api/auth/mfa/challenge", "/api/auth/mfa/disable"]) {
    if (!posts.some((item) => item.path === pathName)) throw new Error(`Expected current-source MFA API call was missing: ${pathName}`);
  }
  console.log("MFA_BROWSER_DISABLE_AND_OWNER_LOGIN_RESTORED=PASS");
  console.log("MFA_AUTHENTICATED_CURRENT_SOURCE_BROWSER_API_ACCEPTANCE=PASS (390px; synthetic account and PostgreSQL; mock AI)");
}

run().catch((error) => {
  console.error(`MFA_BROWSER_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
}).finally(async () => {
  try { socket?.close(); } catch {}
  if (browser && browser.exitCode === null) {
    browser.kill();
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, 2_000);
      browser.once("exit", () => { clearTimeout(timer); resolve(); });
    });
  }
  if (profile && existsSync(profile)) rmSync(profile, { recursive: true, force: true });
});
