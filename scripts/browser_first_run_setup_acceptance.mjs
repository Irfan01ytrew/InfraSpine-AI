#!/usr/bin/env node
/** First-run setup gate, branding and role boundary acceptance on a synthetic install. */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.argv[2] ?? "http://127.0.0.1/");
const fixturePath = process.env.INFRASPINE_FIRST_RUN_FIXTURE;
if (!fixturePath) throw new Error("Set INFRASPINE_FIRST_RUN_FIXTURE to the ACL-protected synthetic account fixture.");
if (!['http:', 'https:'].includes(base.protocol) || !['localhost', '127.0.0.1', '::1'].includes(base.hostname)) {
  throw new Error("First-run browser acceptance is restricted to a loopback URL.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const admin = fixture.admin;
const employee = fixture.employee;
for (const account of [admin, employee]) {
  if (typeof account?.email !== "string" || typeof account?.password !== "string") {
    throw new Error("The protected fixture must contain synthetic Admin and Employee accounts.");
  }
}
const expected = fixture.company ?? {
  company_name: "InfraSpine Synthetic QA",
  industry: "General",
  brand_name: "InfraSpine QA",
  primary_color: "#145A61",
};

function findBrowser() {
  const configured = process.env.INFRASPINE_BROWSER_PATH;
  const candidates = configured ? [configured] : process.platform === "win32"
    ? [
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
        path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
      ]
    : ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/microsoft-edge"];
  const found = candidates.find((candidate) => candidate && existsSync(candidate));
  if (!found) throw new Error("Chrome, Chromium or Edge was not found.");
  return found;
}

function connectDebugger(endpoint) {
  const socket = new WebSocket(endpoint);
  const pending = new Map();
  let sequence = 0;
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const entry = pending.get(message.id);
      clearTimeout(entry.timer);
      pending.delete(message.id);
      if (message.error) entry.reject(new Error("Browser command failed."));
      else entry.resolve(message.result);
    }
  });
  const opened = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("Browser debugging connection failed.")), { once: true });
  });
  return {
    socket,
    ready: () => opened,
    command(method, params = {}) {
      const id = ++sequence;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser command timed out: ${method}`)); }, 15_000);
        pending.set(id, { resolve, reject, timer });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
  };
}

const profileDir = mkdtempSync(path.join(tmpdir(), "infraspine-first-run-browser-"));
const browser = spawn(findBrowser(), [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-background-networking", "--disable-sync", "--remote-debugging-port=0",
  `--user-data-dir=${profileDir}`, base.href,
], { stdio: "ignore", windowsHide: true });
let cdp;

async function evaluate(expression, timeoutMs = 15_000) {
  const result = await cdp.command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("Browser acceptance action raised a page exception.");
  return result.result?.value;
}

async function waitFor(expression, timeoutMs = 20_000) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (await evaluate(expression)) return true;
    await delay(200);
  }
  return false;
}

async function signIn(account, role) {
  const started = await evaluate(`(()=>{const form=document.querySelector('form.login-form');if(!form)return false;const fields={email:${JSON.stringify(account.email)},password:${JSON.stringify(account.password)}};for(const [name,value] of Object.entries(fields)){const input=form.querySelector('[name="'+name+'"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,value);input.dispatchEvent(new Event('input',{bubbles:true}))}form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return true})()`);
  if (!started || !await waitFor(`(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin'});if(!r.ok)return false;const u=await r.json();return u.role===${JSON.stringify(role)}})()`)) {
    throw new Error(`Synthetic ${role} login or session check failed.`);
  }
}

async function signOut() {
  if (!await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Sign out"]');if(!button)return false;button.click();return true})()`)) {
    throw new Error("Sign-out control was missing.");
  }
  if (!await waitFor(`Boolean(document.querySelector('form.login-form'))`)) throw new Error("Sign-out did not return to the login page.");
}

async function expectStatus(expression, expectedStatus, label) {
  const result = await evaluate(expression);
  if (result !== expectedStatus) throw new Error(`${label} returned HTTP ${result}; expected ${expectedStatus}.`);
}

async function run() {
  const activePortFile = path.join(profileDir, "DevToolsActivePort");
  const startDeadline = Date.now() + 30_000;
  while (!existsSync(activePortFile) && Date.now() < startDeadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePortFile)) throw new Error("Browser did not publish its local debugging port.");
  const port = Number(readFileSync(activePortFile, "utf8").split(/\r?\n/)[0]);
  let target;
  const targetDeadline = Date.now() + 30_000;
  while (!target && Date.now() < targetDeadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      if (response.ok) target = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!target) await delay(150);
  }
  if (!target) throw new Error("Browser page target did not become ready.");
  cdp = connectDebugger(target.webSocketDebuggerUrl);
  await cdp.ready();
  await cdp.command("Page.enable");
  await cdp.command("Runtime.enable");
  if (!await waitFor(`Boolean(document.querySelector('form.login-form'))`)) throw new Error("Candidate login form did not render.");

  await signIn(employee, "employee");
  if (!await waitFor(`document.body.innerText.includes('Company setup is required')`)) {
    throw new Error("The pre-setup Employee guidance screen did not render.");
  }
  const employeeGate = await evaluate(`(()=>({message:document.body.innerText.includes('Company setup is required'),shell:Boolean(document.querySelector('.shell'))}))()`);
  if (!employeeGate.message || employeeGate.shell) throw new Error("An Employee could access the workspace before company setup.");
  await expectStatus(`(async()=>{const r=await fetch('/api/admin/deployment/company',{method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},body:JSON.stringify(${JSON.stringify(expected)})});return r.status})()`, 403, "Pre-setup Employee profile update");
  console.log("FIRST_RUN_EMPLOYEE_WAIT_AND_ADMIN_DENIAL=PASS");
  await signOut();

  await signIn(admin, "admin");
  if (!await waitFor(`document.querySelector('h1')?.textContent?.trim()==='Company setup'&&document.body.innerText.includes('Setup required')`)) {
    throw new Error("The first-run Admin setup screen did not render.");
  }
  const adminGate = await evaluate(`(()=>({heading:document.querySelector('h1')?.textContent?.trim(),required:document.body.innerText.includes('Setup required'),shell:Boolean(document.querySelector('.shell'))}))()`);
  if (adminGate.heading !== "Company setup" || !adminGate.required || adminGate.shell) {
    throw new Error(`Admin first-run gate mismatch (heading=${adminGate.heading}; required=${adminGate.required}; shell=${adminGate.shell}).`);
  }

  const filled = await evaluate(`(()=>{const forms=[...document.querySelectorAll('.deployment-form')];const form=forms[0];if(!form)return false;const labels=[...form.querySelectorAll('label')];const find=(prefix)=>labels.find(x=>x.textContent.trim().startsWith(prefix))?.querySelector('input');const set=(input,value)=>{if(!input)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,value);input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));return true};return set(find('Company name'),${JSON.stringify(expected.company_name)})&&set(find('Industry'),${JSON.stringify(expected.industry)})&&set(find('Product brand'),${JSON.stringify(expected.brand_name)})&&set(find('Brand color'),${JSON.stringify(expected.primary_color)})})()`);
  if (!filled) throw new Error("Could not fill the first-run company profile form.");
  const saved = await evaluate(`(()=>{const button=[...document.querySelectorAll('button')].find(x=>x.textContent.includes('Save company profile'));if(!button)return false;button.click();return true})()`);
  if (!saved) throw new Error("First-run Save company profile button was missing.");
  if (!await waitFor(`document.title===${JSON.stringify(expected.brand_name)}&&Boolean(document.querySelector('.shell'))`, 30_000)) {
    throw new Error("Saved branding was not applied to the signed-in application shell.");
  }
  await cdp.command("Page.reload");
  if (!await waitFor(`document.title===${JSON.stringify(expected.brand_name)}&&document.querySelector('.sidebar .brand')?.textContent?.includes(${JSON.stringify(expected.brand_name)})`, 30_000)) {
    throw new Error("Company branding did not persist after browser reload.");
  }
  const loadedColor = await evaluate(`getComputedStyle(document.documentElement).getPropertyValue('--primary').trim()`);
  if (typeof loadedColor !== "string" || loadedColor.toLowerCase() !== expected.primary_color.toLowerCase()) {
    throw new Error(`The saved company brand color did not persist after reload (loaded=${loadedColor}).`);
  }
  console.log("FIRST_RUN_ADMIN_SETUP_AND_BRANDING_RELOAD=PASS");

  await signOut();
  await signIn(employee, "employee");
  if (!await waitFor(`Boolean(document.querySelector('.shell'))`)) throw new Error("Employee workspace did not render after setup completed.");
  const normalShell = await evaluate(`(()=>({shell:Boolean(document.querySelector('.shell')),setupNav:[...document.querySelectorAll('button.nav-item')].some(x=>x.textContent.trim()==='Company setup')}))()`);
  if (!normalShell.shell || normalShell.setupNav) throw new Error("Employee shell showed an Admin-only setup route.");
  await cdp.command("Runtime.evaluate", { expression: `location.hash='#/deployment'` });
  await waitFor(`Boolean(document.querySelector('.shell'))`);
  const deniedRoute = await evaluate(`!document.querySelector('h1')?.textContent?.includes('Company setup')`);
  if (!deniedRoute) throw new Error("Employee reached the Admin setup page by direct route.");
  await expectStatus(`(async()=>{const r=await fetch('/api/admin/deployment/company',{method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},body:JSON.stringify(${JSON.stringify(expected)})});return r.status})()`, 403, "Post-setup Employee profile update");
  console.log("FIRST_RUN_POST_SETUP_EMPLOYEE_BOUNDARY=PASS");
}

try {
  await run();
  console.log("FIRST_RUN_BROWSER_ACCEPTANCE=PASS");
} catch (error) {
  console.error(`FIRST_RUN_BROWSER_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
} finally {
  cdp?.socket.close();
  if (browser.exitCode === null) {
    browser.kill();
    await Promise.race([new Promise((resolve) => browser.once("exit", resolve)), delay(3_000)]);
  }
  const safeProfile = path.resolve(profileDir);
  const tempRoot = `${path.resolve(tmpdir())}${path.sep}`;
  if (safeProfile.startsWith(tempRoot) && path.basename(safeProfile).startsWith("infraspine-first-run-browser-")) {
    rmSync(safeProfile, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
  }
}
