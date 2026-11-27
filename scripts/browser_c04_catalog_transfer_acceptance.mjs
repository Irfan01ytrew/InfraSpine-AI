#!/usr/bin/env node
/** Current-source People/role-template and both stewardship-transfer UI journeys. */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const fixturePath = process.env.INFRASPINE_C04_REVIEW_FIXTURE;
const evidencePath = process.env.INFRASPINE_C04_REVIEW_EVIDENCE;
const base = process.env.INFRASPINE_C04_REVIEW_URL ?? "http://127.0.0.1:5183/admin/";
if (!fixturePath || !evidencePath || !existsSync(fixturePath)) throw new Error("Protected synthetic C04 review fixtures are unavailable.");
const url = new URL(base);
if (url.hostname !== "127.0.0.1" || !/^https?:$/.test(url.protocol)) throw new Error("C04 browser acceptance is limited to loopback HTTP.");
const fixture = JSON.parse(readFileSync(fixturePath, "utf8"));
const admin = fixture.admin;
const manager = fixture.managers?.[0];
const activeDeparting = fixture.employees?.find((item) => item.email === "c04-departing-active@synthetic.test");
const inactiveDeparting = fixture.employees?.find((item) => item.email === "c04-departing-inactive@synthetic.test");
if (!admin?.email || !admin?.password || !manager?.email || !activeDeparting || !inactiveDeparting) {
  throw new Error("Protected C04 synthetic role fixture is incomplete.");
}

const chromePath = process.env.INFRASPINE_BROWSER_PATH ?? [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((candidate) => candidate && existsSync(candidate));
if (!chromePath || !existsSync(chromePath)) throw new Error("Local Chrome or Edge was not found.");

let chrome;
let socket;
let profile;
let sequence = 0;
const pending = new Map();
let stage = "startup";

function cdp(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser command timed out: ${method}`)); }, 25_000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await cdp("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error("C04 browser action raised a page exception.");
  return result.result?.value;
}

async function waitFor(expression, label, timeoutMs = 25_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(150);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}

async function setValue(selector, value) {
  const result = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
  if (!result) throw new Error(`C04 form field was unavailable: ${selector}`);
}

async function click(expression, label) {
  if (!await evaluate(expression)) throw new Error(`C04 UI control was unavailable: ${label}`);
}

async function checkLabel(containerSelector, labelText) {
  const checked = await evaluate(`(()=>{const root=document.querySelector(${JSON.stringify(containerSelector)});const label=[...(root?.querySelectorAll('label')??[])].find(x=>x.innerText.trim().includes(${JSON.stringify(labelText)}));const input=label?.querySelector('input[type=checkbox]');if(!input)return false;if(!input.checked)input.click();return input.checked;})()`);
  if (!checked) throw new Error(`C04 permission/scope checkbox was unavailable: ${labelText}`);
}

async function choose(rootSelector, selectIndex, optionLabel) {
  const chosen = await evaluate(`(()=>{const root=document.querySelector(${JSON.stringify(rootSelector)});const selects=root instanceof HTMLSelectElement?[root]:[...(root?.querySelectorAll('select')??[])];const el=selects[${selectIndex}];if(!el)return false;const option=[...el.options].find(item=>item.textContent.trim()===${JSON.stringify(optionLabel)}||item.textContent.trim().startsWith(${JSON.stringify(optionLabel)}));if(!option)return false;const setter=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set;setter.call(el,option.value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
  if (!chosen) throw new Error(`C04 selector option was unavailable: ${optionLabel}`);
}

async function clickAccountButton(email, label) {
  return click(`(()=>{const row=[...document.querySelectorAll('tr')].find(item=>item.innerText.includes(${JSON.stringify(email)}));const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.innerText.trim()===${JSON.stringify(label)});if(!button||button.disabled)return false;button.click();return true;})()`, `${label} for synthetic account`);
}

async function confirmDialog(label) {
  await waitFor(`Boolean(document.querySelector('[role="dialog"]'))`, "confirmation dialog");
  await click(`(()=>{const dialog=document.querySelector('[role="dialog"]');const button=[...(dialog?.querySelectorAll('button')??[])].find(item=>item.innerText.trim().toLowerCase()===${JSON.stringify(label.toLowerCase())});if(!button)return false;button.click();return true;})()`, "confirmation action");
}

async function waitForApiEvent(pathSuffix, status, afterIndex = 0) {
  await waitFor(`(window.__c04Events??[]).slice(${afterIndex}).some(item=>item.path.endsWith(${JSON.stringify(pathSuffix)})&&item.status===${status})`, `API ${status} ${pathSuffix}`);
  return evaluate(`(window.__c04Events??[]).slice(${afterIndex}).filter(item=>item.path.endsWith(${JSON.stringify(pathSuffix)}))`);
}

const instrumentation = `(()=>{const original=window.fetch.bind(window);window.__c04Events=[];window.fetch=async(input,init={})=>{const response=await original(input,init);try{const url=new URL(typeof input==='string'?input:input.url,location.href);if(url.pathname.startsWith('/api/admin/users/')&&(url.pathname.endsWith('/active')||url.pathname.endsWith('/knowledge-transfer')))window.__c04Events.push({path:url.pathname,status:response.status});}catch{}return response;};})();`;

try {
  stage = "isolated browser launch";
  profile = mkdtempSync(path.join(tmpdir(), "infraspine-c04-chrome-"));
  chrome = spawn(chromePath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const activePortFile = path.join(profile, "DevToolsActivePort");
  const launchDeadline = Date.now() + 30_000;
  while (!existsSync(activePortFile) && Date.now() < launchDeadline) await delay(100);
  if (!existsSync(activePortFile)) throw new Error("Isolated Chrome did not create its local DevTools endpoint.");
  const devtoolsPort = readFileSync(activePortFile, "utf8").split(/\r?\n/)[0];
  const page = (await (await fetch(`http://127.0.0.1:${devtoolsPort}/json/list`)).json()).find((item) => item.type === "page");
  if (!page) throw new Error("The isolated Chrome page was unavailable.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
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
  await cdp("Emulation.setDeviceMetricsOverride", { width: 1365, height: 1000, deviceScaleFactor: 1, mobile: false });
  await cdp("Page.addScriptToEvaluateOnNewDocument", { source: instrumentation });
  await cdp("Page.navigate", { url: base });

  stage = "Admin sign-in";
  await waitFor(`Boolean(document.querySelector('form.login-form input[name="email"]'))`, "synthetic Admin sign-in form");
  await setValue('form.login-form input[name="email"]', admin.email);
  await setValue('form.login-form input[name="password"]', admin.password);
  await click(`(()=>{const form=document.querySelector('form.login-form');if(!form)return false;form.requestSubmit();return true;})()`, "sign in");
  await waitFor(`Boolean(document.querySelector('.shell'))`, "Admin workspace");
  if (await evaluate(`!document.querySelector('.sidebar')?.innerText.includes(${JSON.stringify("Administrator")})`)) throw new Error("The synthetic Admin role did not reach the workspace.");
  await evaluate(`window.location.hash='#/people'`);
  await waitFor(`document.querySelector('h1')?.innerText==='People & access'`, "People and access page");
  await waitFor(`document.querySelector('section[aria-labelledby="role-template-heading"]')?.innerText.includes('Custom permission templates')`, "catalog-driven role-template section");
  await waitFor(`document.querySelector('section[aria-labelledby="role-template-heading"]')?.innerText.includes('data.steward')`, "steward permission from catalog");
  console.log("C04_CURRENT_SOURCE_PEOPLE_AND_PERMISSION_CATALOG_UI=PASS");

  stage = "role template creation and assignment";
  const templateRoot = 'section[aria-labelledby="role-template-heading"]';
  const templateExists = await evaluate(`document.querySelector(${JSON.stringify(templateRoot)})?.innerText.includes('C04 Scoped Knowledge Steward')`);
  if (!templateExists) {
    await setValue(`${templateRoot} form:first-of-type input`, "C04 Scoped Knowledge Steward");
    await setValue(`${templateRoot} form:first-of-type select`, "manager");
    await checkLabel(`${templateRoot} form:first-of-type`, "data.steward");
    await checkLabel(`${templateRoot} form:first-of-type`, "financial");
    await click(`(()=>{const form=document.querySelector(${JSON.stringify(templateRoot)}+' form:first-of-type');if(!form)return false;form.requestSubmit();return true;})()`, "create scoped stewardship template");
    await waitFor(`document.querySelector(${JSON.stringify(templateRoot)})?.innerText.includes('C04 Scoped Knowledge Steward')`, "created scoped role template");
  }
  const assignmentExists = await evaluate(`(()=>[...(document.querySelector(${JSON.stringify(templateRoot)})?.querySelectorAll('tbody tr')??[])].some(row=>row.innerText.includes(${JSON.stringify(manager.email)})&&row.innerText.includes('C04 Scoped Knowledge Steward')))()`);
  if (!assignmentExists) {
    const assignFormSelector = `${templateRoot} form:nth-of-type(2)`;
    const expires = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000);
    const localExpiry = new Date(expires.getTime() - expires.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
    await choose(assignFormSelector, 0, "C04 Scoped Knowledge Steward");
    await choose(assignFormSelector, 1, manager.email);
    await setValue(`${assignFormSelector} input[type="datetime-local"]`, localExpiry);
    await checkLabel(assignFormSelector, "C04 Transfer Operations");
    await click(`(()=>{const form=document.querySelector(${JSON.stringify(assignFormSelector)});if(!form)return false;form.requestSubmit();return true;})()`, "assign scoped stewardship template");
    await waitFor(`(()=>[...(document.querySelector(${JSON.stringify(templateRoot)})?.querySelectorAll('tbody tr')??[])].some(row=>row.innerText.includes(${JSON.stringify(manager.email)})&&row.innerText.includes('C04 Scoped Knowledge Steward')))()`, "scoped Manager template assignment");
  }
  console.log(`C04_ROLE_TEMPLATE_CREATION_AND_SCOPED_ASSIGNMENT=${templateExists && assignmentExists ? "REUSED_PREVIOUS_PASS" : "PASS"}`);

  stage = "deactivation transfer";
  let eventStart = await evaluate(`(window.__c04Events??[]).length`);
  const stewardSelector = `[aria-label="Knowledge steward after deactivation for ${activeDeparting.email}"]`;
  await choose(stewardSelector, 0, manager.email);
  await clickAccountButton(activeDeparting.email, "Deactivate");
  await confirmDialog("deactivate");
  await waitForApiEvent("/active", 200, eventStart);
  await waitFor(`(()=>{const row=[...document.querySelectorAll('tr')].find(item=>item.innerText.includes(${JSON.stringify(activeDeparting.email)}));return row?.innerText.includes('Inactive')})()`, "successful deactivation and knowledge transfer");
  console.log("C04_DEACTIVATION_AND_SCOPED_KNOWLEDGE_TRANSFER=PASS");

  stage = "catalog-driven direct grant and inactive-account recovery";
  const grantSectionIndex = await evaluate(`(()=>[...document.querySelectorAll('section.card.people-section')].findIndex(section=>section.querySelector('h2')?.innerText==='Resource permissions'))()`);
  if (grantSectionIndex < 0) throw new Error("Catalog-driven resource-permission form was unavailable.");
  const grantSectionSelector = `section.card.people-section:nth-of-type(${grantSectionIndex + 1})`;
  const grantFormSelectorActual = `${grantSectionSelector} form`;
  const directGrantAlreadyExists = await evaluate(`(()=>[...document.querySelectorAll('section.card.people-section')].find(section=>section.querySelector('h2')?.innerText==='Resource permissions')?.innerText.includes(${JSON.stringify(manager.email+' · data.steward · policy')})??false)()`);
  if (!directGrantAlreadyExists) {
    await setValue(`${grantFormSelectorActual} input[type="email"]`, manager.email);
    await choose(grantFormSelectorActual, 0, "data.steward");
    await choose(grantFormSelectorActual, 1, "policy");
    await choose(grantFormSelectorActual, 2, "C04 Transfer Operations");
    await click(`(()=>{const form=document.querySelector(${JSON.stringify(grantFormSelectorActual)});if(!form)return false;form.requestSubmit();return true;})()`, "grant catalogued scoped steward permission");
    await waitFor(`(()=>[...document.querySelectorAll('section.card.people-section')].find(section=>section.querySelector('h2')?.innerText==='Resource permissions')?.innerText.includes('data.steward'))()`, "direct catalogued steward grant");
  }

  eventStart = await evaluate(`(window.__c04Events??[]).length`);
  const recoverySelector = `[aria-label="Knowledge steward for transfer for ${inactiveDeparting.email}"]`;
  await choose(recoverySelector, 0, manager.email);
  await clickAccountButton(inactiveDeparting.email, "Transfer knowledge");
  await confirmDialog("transfer knowledge");
  await waitForApiEvent("/knowledge-transfer", 200, eventStart);
  await waitFor(`document.querySelector('.toast-success')?.innerText.includes('transferred')`, "inactive account knowledge recovery receipt");
  await waitFor(`(()=>{const row=[...document.querySelectorAll('tr')].find(item=>item.innerText.includes(${JSON.stringify(inactiveDeparting.email)}));return row?.innerText.includes('Inactive')})()`, "inactive account remains inactive after recovery");
  console.log("C04_PERMISSION_CATALOG_DIRECT_GRANT_AND_INACTIVE_RECOVERY=PASS");

  stage = "mobile review evidence";
  await cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  if (await evaluate("document.documentElement.scrollWidth>window.innerWidth||window.innerWidth!==390")) throw new Error("People and access page overflowed at 390px.");
  const screenshot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  writeFileSync(evidencePath, Buffer.from(screenshot.data, "base64"), { flag: "wx" });
  console.log(`C04_CURRENT_SOURCE_BROWSER_TO_POSTGRESQL_390PX=PASS ${evidencePath}`);
} catch (error) {
  console.error(`C04_BROWSER_ACCEPTANCE=FAIL stage=${stage} error=${error?.message ?? "unknown"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (chrome && chrome.exitCode === null) {
    chrome.kill();
    await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3000)]);
  }
  if (profile && path.basename(profile).startsWith("infraspine-c04-chrome-")) rmSync(profile, { recursive: true, force: true });
  for (const account of [admin, manager, activeDeparting, inactiveDeparting]) if (account) account.password = "";
}
