#!/usr/bin/env node
/** Current-source browser acceptance for purchase-order validation and generic labeled-form review. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const appUrl = process.env.INFRASPINE_FINANCIAL_REVIEW_URL;
const email = process.env.INFRASPINE_FINANCIAL_REVIEW_ADMIN_EMAIL;
const password = process.env.INFRASPINE_FINANCIAL_REVIEW_ADMIN_PASSWORD;
const poPdf = process.env.INFRASPINE_FINANCIAL_PO_PDF;
const formPdf = process.env.INFRASPINE_FINANCIAL_GENERIC_FORM_PDF;
const reviewRoot = process.env.INFRASPINE_FINANCIAL_REVIEW_ROOT;
const browserPath = process.env.INFRASPINE_BROWSER_PATH || [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((candidate) => candidate && existsSync(candidate));
if (!appUrl || !email || !password || !poPdf || !formPdf || !reviewRoot) throw new Error("The protected synthetic financial-form harness is incomplete.");
const origin = new URL(appUrl);
if (origin.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(origin.hostname)) throw new Error("Financial-form browser acceptance requires loopback HTTP.");
if (!browserPath || !existsSync(browserPath) || !existsSync(poPdf) || !existsSync(formPdf)) throw new Error("The browser or synthetic financial PDFs are unavailable.");

const stamp = new Date().toISOString().replace(/[-:.]/g, "").replace("T", "-").replace("Z", "");
const profile = path.join(reviewRoot, `financial-extra-profile-${stamp}`);
const downloadRoot = path.join(profile, "downloads");
const evidenceRoot = path.join(reviewRoot, "financial-review-evidence");
mkdirSync(downloadRoot, { recursive: true });
mkdirSync(evidenceRoot, { recursive: true });
let browser;
let socket;
let sequence = 0;
const pending = new Map();

function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out during additional financial review step ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error("An additional financial review browser action raised an exception.");
  return result.result?.value;
}

async function waitFor(expression, timeoutMs = 25_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) { if (await evaluate(expression)) return true; await delay(150); }
  return false;
}

async function fill(selector, value) {
  const ok = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!ok) throw new Error("An expected financial review control was missing.");
}

async function api(method, route) {
  const response = await evaluate(`(async()=>{const r=await fetch(${JSON.stringify(route)},{method:${JSON.stringify(method)},credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'InfraSpineAdmin'}});let data=null;try{data=await r.json()}catch{}return {status:r.status,data}})()`);
  if (response.status < 200 || response.status >= 300) throw new Error(`Financial-form API returned HTTP ${response.status}: ${method} ${route}`);
  return response.data;
}

async function clickText(selector, text) {
  const ok = await evaluate(`(()=>{const button=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(text)}&&!x.disabled);button?.click();return !!button})()`);
  if (!ok) throw new Error(`Expected enabled financial review action was missing: ${text}`);
}

async function signIn() {
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`, 45_000)) throw new Error("The local financial review sign-in page did not load.");
  await fill('form.login-form input[name="email"]', email);
  await fill('form.login-form input[name="password"]', password);
  await evaluate(`document.querySelector('form.login-form')?.requestSubmit()`);
  if (!await waitFor(`!!document.querySelector('.user-chip-role')`, 45_000)) throw new Error("Synthetic Admin sign-in failed.");
  const me = await api("GET", "/api/auth/me");
  if (me.role !== "admin") throw new Error("The financial review account is not an Admin.");
}

async function openKnowledge() {
  if (!await waitFor(`!![...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.trim()==='Knowledge')`, 20_000)) throw new Error("Knowledge navigation was unavailable.");
  await clickText("button.nav-item", "Knowledge");
  if (!await waitFor(`document.querySelector('main h1,h1')?.textContent.trim()==='Knowledge'`, 20_000)) throw new Error("The Knowledge page did not open.");
}

async function uploadPdf(pdfPath) {
  const filename = path.basename(pdfPath);
  const existing = (await api("GET", "/api/rag/documents")).documents.find((item) => item.filename === filename);
  if (existing) {
    const deadline = Date.now() + 120_000;
    let current = existing;
    while (Date.now() < deadline) {
      current = await api("GET", `/api/rag/documents/${existing.id}`);
      if (current.status === "ready" || current.status === "failed") break;
      await delay(500);
    }
    if (current.status !== "ready" || current.chunk_count < 1) throw new Error("The resumed synthetic financial PDF was not indexed.");
    return current;
  }
  const root = await call("DOM.getDocument", { depth: -1, pierce: true });
  const input = await call("DOM.querySelector", { nodeId: root.root.nodeId, selector: 'input[type="file"][multiple][accept*=".pdf"]' });
  if (!input.nodeId) throw new Error("The shared-document PDF upload control was unavailable.");
  await call("DOM.setFileInputFiles", { files: [pdfPath], nodeId: input.nodeId });
  const deadline = Date.now() + 25_000;
  let document;
  while (Date.now() < deadline) {
    document = (await api("GET", "/api/rag/documents")).documents.find((item) => item.filename === filename);
    if (document) break;
    await delay(250);
  }
  if (!document) throw new Error("The synthetic financial PDF did not create a document row.");
  const readyDeadline = Date.now() + 120_000;
  while (Date.now() < readyDeadline) {
    document = await api("GET", `/api/rag/documents/${document.id}`);
    if (document.status === "ready" || document.status === "failed") break;
    await delay(500);
  }
  if (document.status !== "ready" || document.chunk_count < 1) throw new Error("The synthetic financial PDF was not indexed by the local worker.");
  return document;
}

async function screenshot(name) {
  const shot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const output = path.join(evidenceRoot, `${name}-${stamp}.png`);
  writeFileSync(output, Buffer.from(shot.data, "base64"), { flag: "wx" });
  return output;
}

async function selectProfileAndExtract(document, profileName) {
  const filename = document.filename;
  const route = `/api/rag/documents/${document.id}/financial-extraction?profile=${encodeURIComponent(profileName)}`;
  const ready = await waitFor(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const select=item?.querySelector('select.financial-profile');const button=[...item?.querySelectorAll('button')??[]].find(x=>x.textContent.trim()==='Extract fields');return !!select&&!!button&&!button.disabled})()`, 30_000);
  if (!ready) throw new Error("The indexed financial document profile controls did not render.");
  const selected = await evaluate(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const el=item?.querySelector('select.financial-profile');if(!el)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(el,${JSON.stringify(profileName)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!selected) throw new Error("The financial extraction profile could not be selected.");
  const clicked = await evaluate(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const button=[...item?.querySelectorAll('button')??[]].find(x=>x.textContent.trim()==='Extract fields'&&!x.disabled);button?.click();return !!button})()`);
  if (!clicked) throw new Error("The financial extraction action was not enabled.");
  const deadline = Date.now() + 30_000;
  let result;
  while (Date.now() < deadline) {
    result = await api("GET", route);
    if (result.review_status === "pending") break;
    await delay(250);
  }
  if (result.review_status !== "pending") throw new Error(`The ${profileName} draft did not become reviewable.`);
  return result;
}

async function saveReviewed(document, profileName, rationale) {
  const filename = document.filename;
  if (!await waitFor(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});return !!item?.querySelector('.financial-extraction textarea')})()`, 15_000)) {
    throw new Error(`The ${profileName} review editor did not render.`);
  }
  await fill(`.doc-item:has(.doc-title strong[title="${filename}"]) .financial-extraction textarea`, rationale);
  const enabled = await waitFor(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const button=[...item?.querySelectorAll('.financial-review-controls button')??[]].find(x=>x.textContent.includes('Save reviewed values'));return !!button&&!button.disabled})()`, 15_000);
  if (!enabled) {
    const state = await evaluate(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const section=item?.querySelector('.financial-extraction');const button=[...item?.querySelectorAll('.financial-review-controls button')??[]].find(x=>x.textContent.includes('Save reviewed values'));return {section:!!section,button:!!button,disabled:!!button?.disabled,rationaleLength:section?.querySelector('textarea')?.value.length||0,exceptionCount:section?.querySelectorAll('.financial-issues li').length||0,checkedExceptions:section?.querySelectorAll('.financial-issues input[type=checkbox]:checked').length||0}})()`);
    throw new Error(`The ${profileName} review save remained disabled (section=${!!state?.section}, button=${!!state?.button}, disabled=${!!state?.disabled}, rationaleLength=${state?.rationaleLength ?? 0}, exceptionCount=${state?.exceptionCount ?? 0}, checkedExceptions=${state?.checkedExceptions ?? 0}).`);
  }
  const clicked = await evaluate(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const button=[...item?.querySelectorAll('.financial-review-controls button')??[]].find(x=>x.textContent.includes('Save reviewed values')&&!x.disabled);button?.click();return !!button})()`);
  if (!clicked) throw new Error(`The ${profileName} review save action was unavailable.`);
  const route = `/api/rag/documents/${document.id}/financial-extraction?profile=${encodeURIComponent(profileName)}`;
  const deadline = Date.now() + 30_000;
  let result;
  while (Date.now() < deadline) {
    result = await api("GET", route);
    if (result.review_status === "reviewed") break;
    await delay(250);
  }
  if (result.review_status !== "reviewed" || result.review_history.length !== 1) throw new Error(`The ${profileName} review revision did not persist.`);
  return result;
}

async function connect() {
  const sanitizedEnv = { ...process.env };
  for (const name of ["INFRASPINE_FINANCIAL_REVIEW_URL", "INFRASPINE_FINANCIAL_REVIEW_ADMIN_EMAIL", "INFRASPINE_FINANCIAL_REVIEW_ADMIN_PASSWORD", "INFRASPINE_FINANCIAL_PO_PDF", "INFRASPINE_FINANCIAL_GENERIC_FORM_PDF", "INFRASPINE_FINANCIAL_REVIEW_ROOT"]) delete sanitizedEnv[name];
  browser = spawn(browserPath, [
    "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync", "--disable-background-networking",
    "--disable-component-update", "--disable-features=PasswordManagerOnboarding,PasswordManagerUI", "--disable-save-password-bubble",
    "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank",
  ], { stdio: "ignore", windowsHide: false, env: sanitizedEnv });
  const activePort = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activePort) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePort)) throw new Error("The isolated financial review browser did not start.");
  const port = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline && browser.exitCode === null) {
    try { const response = await fetch(`http://127.0.0.1:${port}/json/list`); if (response.ok) page = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl); } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("The financial review browser page did not become ready.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", () => reject(new Error("Financial review browser connection failed.")), { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const entry = pending.get(message.id);
    clearTimeout(entry.timer);
    pending.delete(message.id);
    if (message.error) entry.reject(new Error(`Financial review browser command failed: ${entry.method}`));
    else entry.resolve(message.result);
  });
  await call("Page.enable");
  await call("Runtime.enable");
  await call("DOM.enable");
  await call("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloadRoot, eventsEnabled: true });
  await call("Emulation.setDeviceMetricsOverride", { width: 1365, height: 900, deviceScaleFactor: 1, mobile: false });
  await call("Page.navigate", { url: appUrl });
}

async function run() {
  await connect();
  await signIn();
  await openKnowledge();
  const purchaseOrder = await uploadPdf(poPdf);
  const po = await selectProfileAndExtract(purchaseOrder, "purchase_order");
  if (po.fields.purchase_order_number?.value !== "PO-4421" || po.fields.vendor?.value !== "North Star Supplies" ||
      po.fields.subtotal?.value !== "100.00" || po.fields.tax?.value !== "5.00" || po.fields.total?.value !== "106.00" ||
      !po.exceptions.some((item) => item.code === "total_components_mismatch")) {
    throw new Error("The purchase-order field mapping or mismatch warning did not match the synthetic source.");
  }
  await fill(`.doc-item:has(.doc-title strong[title="${purchaseOrder.filename}"]) .financial-extraction input[aria-label="Reviewed total"]`, "105.00");
  const acknowledged = await evaluate(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(purchaseOrder.filename)});const checkboxes=[...(item?.querySelectorAll('.financial-issues input[type="checkbox"]')??[])];if(!checkboxes.length)return false;for(const checkbox of checkboxes){if(!checkbox.checked)checkbox.click()}return checkboxes.every(checkbox=>checkbox.checked)})()`);
  if (!acknowledged) throw new Error("The purchase-order exception acknowledgement control was unavailable.");
  const reviewedPo = await saveReviewed(purchaseOrder, "purchase_order", "Corrected the order total and acknowledged the extracted component mismatch against the synthetic source.");
  if (reviewedPo.fields.total?.value !== "105.00" || !reviewedPo.review?.reviewed_exception_indexes.includes(0)) {
    throw new Error("The reviewed purchase-order correction or exception acknowledgement did not persist.");
  }
  const poShot = await screenshot("purchase-order-reviewed");
  console.log("LOCAL_FINANCIAL_PURCHASE_ORDER_VALIDATION_AND_REVIEW=PASS");

  const genericForm = await uploadPdf(formPdf);
  const form = await selectProfileAndExtract(genericForm, "form");
  if (form.fields.request_id?.value !== "REQ-2238" || form.fields.requested_by?.value !== "QA Employee" ||
      form.fields.department?.value !== "Laboratory QA" || form.fields.equipment?.value !== "Safety gloves" ||
      form.fields.quantity?.value !== "12") {
    throw new Error("Generic labeled-form fields did not map to their expected source values.");
  }
  await fill(`.doc-item:has(.doc-title strong[title="${genericForm.filename}"]) .financial-extraction input[aria-label="Reviewed department"]`, "QA Operations");
  const reviewedForm = await saveReviewed(genericForm, "form", "Checked the labeled equipment request fields against the synthetic source.");
  if (reviewedForm.fields.department?.value !== "QA Operations") throw new Error("The reviewed generic-form correction did not persist.");
  const formShot = await screenshot("generic-form-reviewed");
  console.log("LOCAL_FINANCIAL_GENERIC_FORM_EXTRACTION_AND_REVIEW=PASS");
  console.log("LOCAL_FINANCIAL_ADDITIONAL_FORMS_CURRENT_SOURCE_BROWSER_TO_POSTGRESQL=PASS");
  console.log("LOCAL_FINANCIAL_ADDITIONAL_FORMS_EVIDENCE=" + JSON.stringify({ purchaseOrder: poShot, genericForm: formShot }));
}

try {
  await run();
} catch (error) {
  console.error(`LOCAL_FINANCIAL_ADDITIONAL_FORMS_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (browser?.exitCode === null) {
    browser.kill();
    await Promise.race([new Promise((resolve) => browser.once("exit", resolve)), delay(5_000)]);
    console.log("LOCAL_FINANCIAL_ADDITIONAL_FORMS_BROWSER_CLOSED=PASS");
  }
}
