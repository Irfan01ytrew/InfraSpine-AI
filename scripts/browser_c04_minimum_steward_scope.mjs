#!/usr/bin/env node
/** Review the C04 role-template defaults and retain only the explicitly intended steward scope. */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const fixturePath = process.env.INFRASPINE_C04_REVIEW_FIXTURE;
const evidencePath = process.env.INFRASPINE_C04_REVIEW_EVIDENCE;
const base = process.env.INFRASPINE_C04_REVIEW_URL ?? "http://127.0.0.1:5183/admin/";
if (!fixturePath || !evidencePath || !existsSync(fixturePath)) throw new Error("Protected C04 review fixture is unavailable.");
const { admin, managers = [] } = JSON.parse(readFileSync(fixturePath, "utf8"));
const steward = managers[0];
const oldTemplate = "C04 Scoped Knowledge Steward";
const incorrectTemplate = "C04 Stewardship Minimum Scope Corrected";
const newTemplate = "C04 Stewardship Least Privilege";
const groupName = "C04 Transfer Operations";
const chromePath = process.env.INFRASPINE_BROWSER_PATH ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
if (!admin?.email || !admin?.password || !steward?.email || !existsSync(chromePath)) throw new Error("C04 Admin or Manager fixture is unavailable.");

let chrome;
let socket;
let profile;
let sequence = 0;
const pending = new Map();
let stage = "launch";
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
  if (result.exceptionDetails) throw new Error("C04 synthetic UI action raised an exception.");
  return result.result?.value;
}
async function waitFor(expression, label) {
  const deadline = Date.now() + 25_000;
  while (Date.now() < deadline) { if (await evaluate(expression)) return; await delay(150); }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function fill(selector, value) {
  const done = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
  if (!done) throw new Error(`C04 form control missing: ${selector}`);
}
async function choose(rootSelector, index, label) {
  const done = await evaluate(`(()=>{const root=document.querySelector(${JSON.stringify(rootSelector)});const selects=root instanceof HTMLSelectElement?[root]:[...(root?.querySelectorAll('select')??[])];const el=selects[${index}];const option=[...(el?.options??[])].find(item=>item.textContent.trim()===${JSON.stringify(label)}||item.textContent.trim().startsWith(${JSON.stringify(label)}));if(!el||!option)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(el,option.value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
  if (!done) throw new Error(`C04 dropdown option missing: ${label}`);
}
async function clickRow(names, buttonName) {
  const required = Array.isArray(names) ? names : [names];
  const done = await evaluate(`(()=>{const needles=${JSON.stringify(required)};const row=[...document.querySelectorAll('tr')].find(item=>needles.every(text=>item.innerText.includes(text))&&[...(item.querySelectorAll('button')??[])].some(button=>button.innerText.trim()===${JSON.stringify(buttonName)}));const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.innerText.trim()===${JSON.stringify(buttonName)});if(!button||button.disabled)return false;button.click();return true;})()`);
  if (!done) throw new Error(`C04 row action unavailable: ${buttonName}`);
}
async function clickTemplateExact(name, buttonName) {
  const done = await evaluate(`(()=>{const row=[...document.querySelectorAll('tr')].find(item=>[...(item.querySelectorAll('td')??[])].some(cell=>cell.innerText.trim()===${JSON.stringify(name)})&&[...(item.querySelectorAll('button')??[])].some(button=>button.innerText.trim()===${JSON.stringify(buttonName)}));const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.innerText.trim()===${JSON.stringify(buttonName)});if(!button||button.disabled)return false;button.click();return true;})()`);
  if (!done) throw new Error(`C04 exact template action unavailable: ${buttonName}`);
}
async function confirm(label) {
  await waitFor(`Boolean(document.querySelector('[role="dialog"]'))`, "confirmation dialog");
  const done = await evaluate(`(()=>{const dialog=document.querySelector('[role="dialog"]');const button=[...(dialog?.querySelectorAll('button')??[])].find(item=>item.innerText.trim()===${JSON.stringify(label)});if(!button)return false;button.click();return true;})()`);
  if (!done) throw new Error("C04 confirmation action unavailable.");
}
async function setLegendChecks(formSelector, legendText, selectedLabels) {
  const changed = await evaluate(`(()=>{const form=document.querySelector(${JSON.stringify(formSelector)});const fieldset=[...(form?.querySelectorAll('fieldset')??[])].find(item=>item.querySelector('legend')?.innerText.trim()===${JSON.stringify(legendText)});if(!fieldset)return false;const labels=[...fieldset.querySelectorAll('label')].filter(label=>label.querySelector('input[type=checkbox]'));if(!labels.length)return false;const selected=${JSON.stringify(selectedLabels)};if(selected.some(item=>!labels.some(label=>label.innerText.trim()===item)))return false;for(const label of labels){const input=label.querySelector('input[type=checkbox]');const shouldCheck=selected.includes(label.innerText.trim());if(input.checked!==shouldCheck)input.click();}return labels.every(label=>label.querySelector('input[type=checkbox]').checked===selected.includes(label.innerText.trim()))})()`);
  if (!changed) throw new Error(`C04 could not set exact ${legendText} checkboxes.`);
}

try {
  profile = mkdtempSync(path.join(tmpdir(), "infraspine-c04-scope-") );
  chrome = spawn(chromePath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const activeFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activeFile) && Date.now() < deadline) await delay(100);
  if (!existsSync(activeFile)) throw new Error("Isolated Chrome did not start.");
  const port = readFileSync(activeFile, "utf8").split(/\r?\n/)[0];
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data); const item = pending.get(message.id); if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error("Chrome DevTools command failed.")) : item.resolve(message.result);
  });
  await cdp("Page.enable"); await cdp("Runtime.enable");
  await cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await cdp("Page.navigate", { url: base });
  stage = "Admin sign-in";
  await waitFor(`Boolean(document.querySelector('form.login-form input[name="email"]'))`, "Admin sign-in form");
  await fill('form.login-form input[name="email"]', admin.email);
  await fill('form.login-form input[name="password"]', admin.password);
  await evaluate(`document.querySelector('form.login-form')?.requestSubmit()`);
  await waitFor(`Boolean(document.querySelector('.shell'))`, "Admin workspace");
  await evaluate(`window.location.hash='#/people'`);
  await waitFor(`document.querySelector('h1')?.innerText==='People & access'`, "People page");
  const templateRoot = 'section[aria-labelledby="role-template-heading"]';
  await waitFor(`(()=>{const section=document.querySelector(${JSON.stringify(templateRoot)});return section?.innerText.includes('data.steward')&&section.innerText.includes(${JSON.stringify(oldTemplate)})&&section.innerText.includes(${JSON.stringify(steward.email)})})()`, "loaded catalog and existing assignment rows");
  const emptyDefaults = await evaluate(`(()=>{const form=document.querySelector(${JSON.stringify(templateRoot)}+' form:first-of-type');const fields=[...(form?.querySelectorAll('fieldset')??[])];return fields.length===2&&fields.every(field=>[...(field.querySelectorAll('input[type=checkbox]')??[])].every(input=>!input.checked))&&form.querySelector('button')?.disabled===true})()`);
  if (!emptyDefaults) throw new Error("C04 permission template creation must begin with no grants selected and submit disabled.");
  console.log("C04_TEMPLATE_LEAST_PRIVILEGE_DEFAULTS=PASS");

  stage = "revoke overbroad synthetic test assignment";
  const oldAssignment = await evaluate(`(()=>[...(document.querySelector(${JSON.stringify(templateRoot)})?.querySelectorAll('tbody tr')??[])].find(row=>row.innerText.includes(${JSON.stringify(steward.email)})&&row.innerText.includes(${JSON.stringify(oldTemplate)}))?.innerText??'')()`);
  if (oldAssignment) {
    await clickRow([steward.email, oldTemplate], "Revoke");
    await confirm("Revoke assignment");
    await waitFor(`document.querySelector('.toast-success')?.innerText.includes('Role assignment revoked')`, "old assignment revocation");
  }
  const oldTemplateActive = await evaluate(`(()=>[...(document.querySelector(${JSON.stringify(templateRoot)})?.querySelectorAll('tbody tr')??[])].some(row=>row.innerText.includes(${JSON.stringify(oldTemplate)})&&[...(row.querySelectorAll('button')??[])].some(button=>button.innerText.trim()==='Deactivate')))()`);
  if (oldTemplateActive) {
    await clickRow(oldTemplate, "Deactivate");
    await confirm("Deactivate");
    await waitFor(`document.querySelector('.toast-success')?.innerText.includes('Template deactivated')`, "old template deactivation");
  }

  stage = "revoke prior correction grant from isolated fixture";
  const priorCorrectionAssignment = await evaluate(`(()=>[...(document.querySelector(${JSON.stringify(templateRoot)})?.querySelectorAll('tbody tr')??[])].find(row=>row.innerText.includes(${JSON.stringify(steward.email)})&&row.innerText.includes(${JSON.stringify(incorrectTemplate)}))?.innerText??'')()`);
  if (priorCorrectionAssignment) {
    await clickRow([steward.email, incorrectTemplate], "Revoke");
    await confirm("Revoke assignment");
    await waitFor(`document.querySelector('.toast-success')?.innerText.includes('Role assignment revoked')`, "prior correction grant revocation");
  }

  stage = "minimum-scope replacement";
  const wrongTemplate = "C04 Stewardship Minimum Scope";
  const wrongTemplateActive = await evaluate(`(()=>[...(document.querySelector(${JSON.stringify(templateRoot)})?.querySelectorAll('tbody tr')??[])].some(row=>[...(row.querySelectorAll('td')??[])].some(cell=>cell.innerText.trim()===${JSON.stringify(wrongTemplate)})&&[...(row.querySelectorAll('button')??[])].some(button=>button.innerText.trim()==='Deactivate')))()`);
  if (wrongTemplateActive) {
    await clickTemplateExact(wrongTemplate, "Deactivate");
    await confirm("Deactivate");
    await waitFor(`document.querySelector('.toast-success')?.innerText.includes('Template deactivated')`, "incomplete template deactivation");
  }
  const existingMinimal = await evaluate(`(()=>{const row=[...(document.querySelector(${JSON.stringify(templateRoot)})?.querySelectorAll('tbody tr')??[])].find(item=>[...(item.querySelectorAll('td')??[])].some(cell=>cell.innerText.trim()===${JSON.stringify(newTemplate)}));return row?{active:row.innerText.includes('Active')&&!row.innerText.includes('Inactive')}:null})()`);
  if (existingMinimal && !existingMinimal.active) throw new Error("C04 final least-privilege template exists but is inactive; refusing to reuse it.");
  if (!existingMinimal) {
    const createForm = `${templateRoot} form:first-of-type`;
    await fill(`${createForm} input`, newTemplate);
    await fill(`${createForm} select`, "manager");
    await setLegendChecks(createForm, "Resource actions", ["data.steward"]);
    await setLegendChecks(createForm, "Category scope", ["financial"]);
    await evaluate(`document.querySelector(${JSON.stringify(createForm)})?.requestSubmit()`);
    await waitFor(`document.querySelector(${JSON.stringify(templateRoot)})?.innerText.includes(${JSON.stringify(newTemplate)})`, "minimum-scope template creation");
  }
  const newAssignmentExists = await evaluate(`(()=>[...(document.querySelector(${JSON.stringify(templateRoot)})?.querySelectorAll('tbody tr')??[])].some(row=>row.innerText.includes(${JSON.stringify(steward.email)})&&row.innerText.includes(${JSON.stringify(newTemplate)})&&[...(row.querySelectorAll('button')??[])].some(button=>button.innerText.trim()==='Revoke')) )()`);
  if (!newAssignmentExists) {
    const assignForm = `${templateRoot} form:nth-of-type(2)`;
    const expiry = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000);
    const localExpiry = new Date(expiry.getTime() - expiry.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
    await choose(assignForm, 0, newTemplate);
    await choose(assignForm, 1, steward.email);
    await fill(`${assignForm} input[type="datetime-local"]`, localExpiry);
    await setLegendChecks(assignForm, "Department scope (required)", [groupName]);
    await evaluate(`document.querySelector(${JSON.stringify(assignForm)})?.requestSubmit()`);
    await waitFor(`(()=>[...(document.querySelector(${JSON.stringify(templateRoot)})?.querySelectorAll('tbody tr')??[])].some(row=>row.innerText.includes(${JSON.stringify(steward.email)})&&row.innerText.includes(${JSON.stringify(newTemplate)})&&row.innerText.includes('1')))()`, "one-grant scoped assignment");
  }
  console.log("C04_TEMPLATE_EXACT_SCOPE=PASS (active template: data.steward only; financial category; Transfer Operations department; one active grant)");

  stage = "mobile review";
  await waitFor("window.visualViewport?.width<=390&&document.body.scrollWidth<=390", "stable 390px visual viewport without body overflow");
  await delay(200);
  const metrics = await evaluate(`({innerWidth,visualViewport:visualViewport?.width,clientWidth:document.documentElement.clientWidth,documentWidth:document.documentElement.scrollWidth,bodyWidth:document.body.scrollWidth,devicePixelRatio,zoom:visualViewport?.scale})`);
  const image = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  writeFileSync(evidencePath, Buffer.from(image.data, "base64"), { flag: "wx" });
  console.log(`C04_CURRENT_SOURCE_PEOPLE_UI_390PX=PASS ${JSON.stringify(metrics)} ${evidencePath}`);
} catch (error) {
  console.error(`C04_SCOPE_REVIEW=FAIL stage=${stage} error=${error?.message ?? "unknown"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (chrome && chrome.exitCode === null) { chrome.kill(); await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3000)]); }
  if (profile && path.basename(profile).startsWith("infraspine-c04-scope-")) rmSync(profile, { recursive: true, force: true });
  admin.password = "";
  steward.password = "";
}
