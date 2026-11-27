#!/usr/bin/env node
/** Current-source Admin browser acceptance for client/project workspace setup. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_F01_REVIEW_URL ?? "http://127.0.0.1:5177/admin/");
const fixturePath = process.env.INFRASPINE_F01_CREDENTIAL_FIXTURE;
const reviewRoot = process.env.INFRASPINE_F01_REVIEW_ROOT;
if (base.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(base.hostname) || !fixturePath || !reviewRoot) {
  throw new Error("F01 browser acceptance requires an isolated loopback review and protected synthetic fixture.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const admin = fixture.admin;
if (!admin?.email || !admin?.password) throw new Error("The protected synthetic Admin fixture is incomplete.");

const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for F01 browser acceptance.");

const run = Date.now().toString(36);
const clientName = `F01 browser client ${run}`;
const secondClientName = `F01 browser client two ${run}`;
const projectName = `F01 browser project ${run}`;
const evidenceDir = path.join(reviewRoot, "evidence");
mkdirSync(evidenceDir, { recursive: true });
const screenshotPath = path.join(evidenceDir, "f01-workspaces-mobile.png");
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-f01-workspaces-"));
let chrome;
let socket;
let commandId = 0;
const pending = new Map();

function cdp(method, params = {}, timeoutMs = 20_000) {
  const id = ++commandId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Browser acceptance timed out at ${method}.`));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression, timeoutMs = 20_000) {
  const response = await cdp("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (response.exceptionDetails) throw new Error("A workspace browser action failed in the page.");
  return response.result?.value;
}

async function waitFor(expression, label, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(150);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}

async function setField(sectionTitle, label, value, optionText = null) {
  const script = `(()=>{const s=[...document.querySelectorAll('section.card')].find(x=>x.querySelector('h2')?.textContent.trim()===${JSON.stringify(sectionTitle)});` +
    `const f=[...(s?.querySelectorAll('form.people-inline-form')??[])].find(x=>[...x.querySelectorAll('label.field')].some(y=>y.querySelector('.field-label')?.textContent.trim()===${JSON.stringify(label)}));` +
    `const l=[...(f?.querySelectorAll('label.field')??[])].find(x=>x.querySelector('.field-label')?.textContent.trim()===${JSON.stringify(label)});const e=l?.querySelector('input,select');if(!e)return false;` +
    (optionText === null
      ? `const p=e instanceof HTMLInputElement?HTMLInputElement.prototype:HTMLSelectElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,${JSON.stringify(value)});`
      : `const o=[...e.options].find(x=>x.textContent.trim()===${JSON.stringify(optionText)});if(!o)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,o.value);`) +
    `e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`;
  if (!await evaluate(script)) throw new Error(`The workspace form field '${label}' was not available.`);
}

async function setDetailsField(label, optionText) {
  const script = `(()=>{const f=[...document.querySelectorAll('form.people-inline-form')].find(x=>x.querySelector('button')?.textContent.trim()==='Save workspace details');` +
    `const l=[...(f?.querySelectorAll('label.field')??[])].find(x=>x.querySelector('.field-label')?.textContent.trim()===${JSON.stringify(label)});const e=l?.querySelector('select');if(!e)return false;` +
    `const o=[...e.options].find(x=>x.textContent.trim()===${JSON.stringify(optionText)});if(!o)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,o.value);` +
    `e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`;
  if (!await evaluate(script)) throw new Error(`Workspace details field '${label}' or requested option was unavailable.`);
}

async function setCompanyProfileField(labelText, value) {
  const script = `(()=>{const f=document.querySelector('form.deployment-form');const l=[...(f?.querySelectorAll('label')??[])].find(x=>[...x.childNodes].some(n=>n.nodeType===Node.TEXT_NODE&&n.textContent.trim()===${JSON.stringify(labelText)}));const e=l?.querySelector('input,select');if(!e)return false;` +
    `const p=e instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,${JSON.stringify(value)});` +
    `e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`;
  if (!await evaluate(script)) throw new Error(`First-run company profile field '${labelText}' was unavailable.`);
}

async function finishFirstRunSetup() {
  await waitFor("Boolean(document.querySelector('.first-run-setup .deployment-form'))", "first-run company setup form");
  await setCompanyProfileField("Company name", `F01 Synthetic Company ${run}`);
  await setCompanyProfileField("Industry", "General");
  await setCompanyProfileField("Product brand", "InfraSpine F01 QA");
  const submitted = await evaluate(`(()=>{const f=document.querySelector('.first-run-setup form.deployment-form');const b=[...(f?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Save company profile');if(!b||b.disabled)return false;b.click();return true})()`);
  if (!submitted) throw new Error("The synthetic first-run company profile could not be saved.");
  await waitFor("Boolean(document.querySelector('.shell'))", "post-setup Admin shell", 45_000);
}

async function createWorkspace(kind, name, parentName = null) {
  await setField("Create a workspace", "Workspace type", kind, kind === "client" ? "Client" : "Project");
  if (kind === "project") {
    await waitFor(`(()=>{const s=[...document.querySelectorAll('section.card')].find(x=>x.querySelector('h2')?.textContent.trim()==='Create a workspace');return [...(s?.querySelectorAll('label.field')??[])].some(x=>x.querySelector('.field-label')?.textContent.trim()==='Client')})()`, "project parent selector");
  }
  await setField("Create a workspace", "Name", name);
  if (kind === "project") await setField("Create a workspace", "Client", "", parentName ?? "No parent client");
  await waitFor(`(()=>{const s=[...document.querySelectorAll('section.card')].find(x=>x.querySelector('h2')?.textContent.trim()==='Create a workspace');const l=[...(s?.querySelectorAll('label.field')??[])].find(x=>x.querySelector('.field-label')?.textContent.trim()==='Workspace owner');return [...(l?.querySelector('select')?.options??[])].some(x=>x.textContent.trim()===${JSON.stringify(admin.email)})})()`, "active Admin owner option");
  await setField("Create a workspace", "Workspace owner", "", admin.email);
  const submitted = await evaluate(`(()=>{const s=[...document.querySelectorAll('section.card')].find(x=>x.querySelector('h2')?.textContent.trim()==='Create a workspace');const b=[...(s?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Create workspace');if(!b||b.disabled)return false;b.click();return true})()`);
  if (!submitted) throw new Error("The workspace create form could not be submitted.");
  await waitFor(`(()=>{const s=[...document.querySelectorAll('select')].find(x=>[...x.options].some(o=>o.textContent.includes(${JSON.stringify(name)})));return Boolean(s)})()`, `${kind} workspace creation`);
  const current = await evaluate(`(()=>{const s=[...document.querySelectorAll('label.field')].find(x=>x.querySelector('.field-label')?.textContent.trim()==='Client or project')?.querySelector('select');return s?.selectedOptions?.[0]?.textContent??''})()`);
  if (!current.includes(name)) throw new Error(`The newly created workspace '${name}' was not selected in the visible UI.`);
}

async function selectWorkspace(name) {
  const selected = await evaluate(`(()=>{const s=[...document.querySelectorAll('label.field')].find(x=>x.querySelector('.field-label')?.textContent.trim()==='Client or project')?.querySelector('select');const o=[...(s?.options??[])].find(x=>x.textContent.includes(${JSON.stringify(name)}));if(!s||!o)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(s,o.value);s.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!selected) throw new Error(`Could not select the workspace '${name}'.`);
  await waitFor(`(()=>{const s=[...document.querySelectorAll('label.field')].find(x=>x.querySelector('.field-label')?.textContent.trim()==='Client or project')?.querySelector('select');return s?.selectedOptions?.[0]?.textContent.includes(${JSON.stringify(name)})})()`, `${name} detail selection`);
  await waitFor(`Boolean([...document.querySelectorAll('section[aria-label="Customer workspace summary"] + dl dt')].some(x=>x.textContent.trim()==='Child projects'))`, `${name} summary`);
}

async function saveWorkspaceDetails(expectedDetailText) {
  const submitted = await evaluate(`(()=>{const f=[...document.querySelectorAll('form.people-inline-form')].find(x=>x.querySelector('button')?.textContent.trim()==='Save workspace details');if(!f)return false;f.requestSubmit();return true})()`);
  if (!submitted) throw new Error("Workspace details form was unavailable.");
  await waitFor(`document.querySelector('[aria-label="Workspace details"]')?.innerText.includes(${JSON.stringify(expectedDetailText)})`, "workspace details save");
}

async function workspaceText(label) {
  return evaluate(`(()=>{const dl=document.querySelector('section[aria-label="Customer workspace summary"] + dl');const d=[...(dl?.querySelectorAll('div')??[])].find(x=>x.querySelector('dt')?.textContent.trim()===${JSON.stringify(label)});return d?.querySelector('dd')?.textContent.trim()??null})()`);
}

try {
  chrome = spawn(browserPath, [
    "--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank",
  ], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profile, "DevToolsActivePort");
  const startupDeadline = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < startupDeadline) await delay(100);
  if (!existsSync(portFile)) throw new Error("Isolated Chrome did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!page?.webSocketDebuggerUrl) throw new Error("Isolated Chrome did not expose a page target.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
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
    message.error ? item.reject(new Error("Chrome DevTools command failed.")) : item.resolve(message.result);
  });
  await cdp("Page.enable");
  await cdp("Runtime.enable");
  await cdp("Page.navigate", { url: base.href });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "Admin login form");
  const loginFilled = await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const pass=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!pass||!form)return false;for(const [el,value] of [[email,${JSON.stringify(admin.email)}],[pass,${JSON.stringify(admin.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
  if (!loginFilled) throw new Error("Synthetic Admin login form could not be submitted.");
  await waitFor("Boolean(document.querySelector('.shell')||document.querySelector('.first-run-setup'))", "authenticated Admin or first-run setup", 40_000);
  if (await evaluate("Boolean(document.querySelector('.first-run-setup'))")) await finishFirstRunSetup();
  if (!await evaluate("Boolean(document.querySelector('.shell'))")) throw new Error("The Admin did not reach the workspace after sign-in and setup.");
  const open = await evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Client & project workspaces');if(!b)return false;b.click();return true})()`);
  if (!open) throw new Error("The Admin workspace navigation entry was unavailable.");
  await waitFor("document.querySelector('h1')?.textContent.trim()==='Client & project workspaces'", "workspace page");
  await waitFor("!document.querySelector('.loading')", "initial workspace data");

  await createWorkspace("client", clientName);
  if (!(await evaluate(`document.querySelector('[aria-label="Workspace details"]')?.innerText.includes(${JSON.stringify(admin.email)})`))) {
    throw new Error("The client owner was not shown after creation.");
  }
  await createWorkspace("project", projectName, clientName);
  if (!(await evaluate(`document.querySelector('[aria-label="Workspace details"]')?.innerText.includes(${JSON.stringify(clientName)})`))) {
    throw new Error("The project parent client was not shown after creation.");
  }

  await createWorkspace("client", secondClientName);
  await selectWorkspace(projectName);
  await setDetailsField("Parent client", secondClientName);
  await saveWorkspaceDetails(secondClientName);
  if (!(await evaluate(`document.querySelector('[aria-label="Workspace details"]')?.innerText.includes(${JSON.stringify(secondClientName)})`))) {
    throw new Error("The project could not be reparented through the UI.");
  }
  await selectWorkspace(secondClientName);
  await waitFor(`(()=>{const dl=document.querySelector('section[aria-label="Customer workspace summary"] + dl');const d=[...(dl?.querySelectorAll('div')??[])].find(x=>x.querySelector('dt')?.textContent.trim()==='Child projects');return d?.querySelector('dd')?.textContent.trim()==='1'})()`, "reparented project summary");
  const reparentedChildCount = await workspaceText("Child projects");
  if (reparentedChildCount !== "1") throw new Error("The client workspace report did not include its child project.");

  await selectWorkspace(projectName);
  await setDetailsField("Parent client", "No parent client");
  await saveWorkspaceDetails("None (standalone project)");
  if (!(await evaluate(`document.querySelector('[aria-label="Workspace details"]')?.innerText.includes('None (standalone project)')`))) {
    throw new Error("Explicit project unlinking was not reflected in the UI.");
  }
  await selectWorkspace(secondClientName);
  await waitFor(`(()=>{const dl=document.querySelector('section[aria-label="Customer workspace summary"] + dl');const d=[...(dl?.querySelectorAll('div')??[])].find(x=>x.querySelector('dt')?.textContent.trim()==='Child projects');return d?.querySelector('dd')?.textContent.trim()==='0'})()`, "unlinked project summary");
  const unlinkedChildCount = await workspaceText("Child projects");
  if (unlinkedChildCount !== "0") throw new Error("Workspace reporting retained a project after it was unlinked.");

  await selectWorkspace(projectName);
  const archived = await evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Archive workspace');if(!b)return false;b.click();return true})()`);
  if (!archived) throw new Error("The workspace archive control was unavailable.");
  await waitFor("document.querySelector('[aria-label=\"Workspace administration\"] .pill')?.textContent.trim()==='archived'", "archived project status");
  const reactivated = await evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Reactivate workspace');if(!b)return false;b.click();return true})()`);
  if (!reactivated) throw new Error("The workspace reactivate control was unavailable.");
  await waitFor("document.querySelector('[aria-label=\"Workspace administration\"] .pill')?.textContent.trim()==='active'", "active project status");

  await cdp("Emulation.setDeviceMetricsOverride", { width: 1365, height: 900, deviceScaleFactor: 1, mobile: false });
  const desktop = await evaluate(`(()=>({viewport:visualViewport?.width,body:document.body.scrollWidth,root:document.documentElement.scrollWidth,heading:document.querySelector('h1')?.textContent.trim(),alerts:document.querySelectorAll('.banner-error,[role="alert"]').length}))()`);
  if (desktop.viewport !== 1365 || desktop.body > 1365 || desktop.root > 1365 || desktop.heading !== "Client & project workspaces" || desktop.alerts !== 0) {
    throw new Error("The desktop workspace page had overflow, an error banner, or a missing heading.");
  }
  await cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await delay(250);
  const mobile = await evaluate(`(()=>({viewport:visualViewport?.width,body:document.body.scrollWidth,root:document.documentElement.scrollWidth,heading:document.querySelector('h1')?.textContent.trim(),alerts:document.querySelectorAll('.banner-error,[role="alert"]').length}))()`);
  if (mobile.viewport !== 390 || mobile.body > 390 || mobile.root > 390 || mobile.heading !== "Client & project workspaces" || mobile.alerts !== 0) {
    throw new Error(`The 390px workspace page did not fit the mobile viewport: ${JSON.stringify(mobile)}.`);
  }
  const screenshot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"), { flag: "wx" });
  console.log(`F01_CURRENT_SOURCE_WORKSPACE_BROWSER=PASS; client/project create; owner assignment; reparent/unlink; child report 1→0; archive/reactivate; desktop ${desktop.body}/${desktop.root}px; mobile ${mobile.body}/${mobile.root}px`);
  console.log(`F01_WORKSPACE_SCREENSHOT=${screenshotPath}`);
} finally {
  try { socket?.close(); } catch {}
  if (chrome && !chrome.killed) {
    chrome.kill();
    await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(2_000)]);
  }
  try { rmSync(profile, { recursive: true, force: true }); } catch {}
}
