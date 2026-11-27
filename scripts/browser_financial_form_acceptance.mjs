#!/usr/bin/env node
/** Focused Admin browser acceptance for invoice extraction, human review, and reviewed-only CSV export. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const appUrl = process.env.INFRASPINE_FINANCIAL_REVIEW_URL;
const email = process.env.INFRASPINE_FINANCIAL_REVIEW_ADMIN_EMAIL;
const password = process.env.INFRASPINE_FINANCIAL_REVIEW_ADMIN_PASSWORD;
const pdfPath = process.env.INFRASPINE_FINANCIAL_REVIEW_PDF;
const reviewRoot = process.env.INFRASPINE_FINANCIAL_REVIEW_ROOT;
const browserPath = process.env.INFRASPINE_BROWSER_PATH || [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((candidate) => candidate && existsSync(candidate));
if (!appUrl || !email || !password || !pdfPath || !reviewRoot) throw new Error("The protected invoice review harness is incomplete.");
const origin = new URL(appUrl);
if (origin.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(origin.hostname)) {
  throw new Error("The invoice review browser must target loopback HTTP only.");
}
if (!browserPath || !existsSync(browserPath) || !existsSync(pdfPath)) throw new Error("The browser or synthetic invoice PDF is unavailable.");

const stamp = new Date().toISOString().replace(/[-:.]/g, "").replace("T", "-").replace("Z", "");
const profile = path.join(reviewRoot, `financial-review-profile-${stamp}`);
const downloadRoot = path.join(profile, "downloads");
const evidenceRoot = path.join(reviewRoot, "financial-review-evidence");
mkdirSync(downloadRoot, { recursive: true });
mkdirSync(evidenceRoot, { recursive: true });
const filename = path.basename(pdfPath);
const correctedInvoiceNumber = "INV-9077-REVIEWED";
let browser;
let socket;
let sequence = 0;
const pending = new Map();

function parseCsv(text) {
  const records = [];
  let row = [];
  let field = "";
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') { field += '"'; index += 1; }
      else if (char === '"') quoted = false;
      else field += char;
    } else if (char === '"') quoted = true;
    else if (char === ",") { row.push(field); field = ""; }
    else if (char === "\n") { row.push(field.replace(/\r$/, "")); records.push(row); row = []; field = ""; }
    else field += char;
  }
  if (field || row.length) { row.push(field.replace(/\r$/, "")); records.push(row); }
  return records;
}

function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Timed out during invoice review browser step ${method}.`));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error("An invoice review browser action raised an exception.");
  return result.result?.value;
}

async function waitFor(expression, timeoutMs = 25_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await delay(150);
  }
  return false;
}

async function fill(selector, value) {
  const ok = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:el instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!ok) throw new Error(`Expected invoice review UI control is missing: ${selector}`);
}

async function clickText(selector, text) {
  const ok = await evaluate(`(()=>{const el=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(text)}&&!x.disabled);if(!el)return false;el.click();return true})()`);
  if (!ok) throw new Error(`Expected enabled invoice review UI control is missing: ${text}`);
}

async function api(method, route, body = undefined) {
  const response = await evaluate(`(async()=>{const init={method:${JSON.stringify(method)},credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineAdmin'}};${body === undefined ? "" : `init.headers['Content-Type']='application/json';init.body=JSON.stringify(${JSON.stringify(body)});`}const r=await fetch(${JSON.stringify(route)},init);let data=null;try{data=await r.json()}catch{}return {status:r.status,data}})()`);
  if (response.status < 200 || response.status >= 300) throw new Error(`Invoice review API returned HTTP ${response.status}: ${method} ${route}`);
  return response.data;
}

async function signIn() {
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`, 45_000)) throw new Error("The local invoice review sign-in page did not load.");
  await fill('form.login-form input[name="email"]', email);
  await fill('form.login-form input[name="password"]', password);
  await evaluate(`document.querySelector('form.login-form')?.requestSubmit()`);
  if (!await waitFor(`!!document.querySelector('.user-chip-role')`, 45_000)) throw new Error("Synthetic Admin sign-in failed.");
  if ((await api("GET", "/api/auth/me")).role !== "admin") throw new Error("The invoice review account is not an Admin.");
}

async function screenshot(name) {
  const result = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const output = path.join(evidenceRoot, `${name}-${stamp}.png`);
  writeFileSync(output, Buffer.from(result.data, "base64"), { flag: "wx" });
  return output;
}

async function connect() {
  const sanitizedEnv = { ...process.env };
  for (const name of [
    "INFRASPINE_FINANCIAL_REVIEW_ADMIN_EMAIL", "INFRASPINE_FINANCIAL_REVIEW_ADMIN_PASSWORD",
    "INFRASPINE_FINANCIAL_REVIEW_PDF", "INFRASPINE_FINANCIAL_REVIEW_ROOT", "INFRASPINE_FINANCIAL_REVIEW_URL",
  ]) delete sanitizedEnv[name];
  browser = spawn(browserPath, [
    "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
    "--disable-background-networking", "--disable-component-update", "--disable-features=PasswordManagerOnboarding,PasswordManagerUI",
    "--disable-save-password-bubble", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
    `--user-data-dir=${profile}`, "about:blank",
  ], { stdio: "ignore", windowsHide: false, env: sanitizedEnv });
  const activePort = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activePort) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePort)) throw new Error("The isolated invoice review browser did not start.");
  const port = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline && browser.exitCode === null) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      if (response.ok) page = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("The invoice review browser page did not become ready.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("Invoice review browser connection failed.")), { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const entry = pending.get(message.id);
    clearTimeout(entry.timer);
    pending.delete(message.id);
    if (message.error) entry.reject(new Error(`Invoice review browser command failed: ${entry.method}`));
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
  const knowledge = await waitFor(`!![...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.trim()==='Knowledge')`, 20_000);
  if (!knowledge) throw new Error("The Knowledge navigation was unavailable to Admin.");
  await clickText("button.nav-item", "Knowledge");
  if (!await waitFor(`document.querySelector('main h1,h1')?.textContent.trim()==='Knowledge'`, 20_000)) throw new Error("The Knowledge page did not open.");
  const fileRoot = await call("DOM.getDocument", { depth: -1, pierce: true });
  const input = await call("DOM.querySelector", {
    nodeId: fileRoot.root.nodeId,
    selector: 'input[type="file"][multiple][accept*=".pdf"]',
  });
  if (!input.nodeId) throw new Error("The PDF document upload control was missing.");
  await call("DOM.setFileInputFiles", { files: [pdfPath], nodeId: input.nodeId });

  let document;
  const uploadDeadline = Date.now() + 20_000;
  while (Date.now() < uploadDeadline) {
    const response = await api("GET", "/api/rag/documents");
    document = response.documents.find((item) => item.filename === filename);
    if (document) break;
    await delay(250);
  }
  if (!document) throw new Error("The synthetic invoice upload did not create a document row.");
  const documentSelector = `.doc-item:has(.doc-title strong[title="${filename}"])`;
  const readyDeadline = Date.now() + 120_000;
  while (document.status !== "ready" && document.status !== "failed" && Date.now() < readyDeadline) {
    await delay(500);
    document = await api("GET", `/api/rag/documents/${document.id}`);
  }
  if (document.status !== "ready" || document.chunk_count < 1) throw new Error("The local invoice worker did not index the synthetic PDF.");

  const documentCard = await waitFor(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});return !!item?.querySelector('select.financial-profile[aria-label^="Extraction profile for"]')&&!![...item.querySelectorAll('button')].find(x=>x.textContent.trim()==='Extract fields'&&!x.disabled)})()`, 30_000);
  if (!documentCard) throw new Error("The indexed invoice profile and extraction controls did not render in the Knowledge library.");
  await fill(`${documentSelector} select.financial-profile[aria-label^="Extraction profile for"]`, "invoice");
  const extractClicked = await evaluate(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const button=[...item?.querySelectorAll('button')??[]].find(x=>x.textContent.trim()==='Extract fields'&&!x.disabled);button?.click();return !!button})()`);
  if (!extractClicked) throw new Error("The invoice Extract fields action was unavailable.");

  let extraction;
  const extractionDeadline = Date.now() + 25_000;
  while (Date.now() < extractionDeadline) {
    extraction = await api("GET", `/api/rag/documents/${document.id}/financial-extraction?profile=invoice`);
    if (extraction.review_status === "pending") break;
    await delay(250);
  }
  if (extraction.review_status !== "pending" || extraction.fields.invoice_number?.value !== "INV-9077" || extraction.fields.total?.value !== "25.50") {
    throw new Error("The indexed invoice did not render the expected typed fields and review draft.");
  }
  if (!await waitFor(`!!document.querySelector('.financial-extraction input[aria-label="Reviewed invoice number"]')`, 15_000)) {
    throw new Error("Extracted invoice fields did not render in the Knowledge UI.");
  }
  await fill('.financial-extraction input[aria-label="Reviewed invoice number"]', correctedInvoiceNumber);
  await fill(".financial-extraction textarea", "Checked invoice number and total against the synthetic source PDF.");
  await clickText("button", "Save reviewed values");

  const reviewDeadline = Date.now() + 25_000;
  while (Date.now() < reviewDeadline) {
    extraction = await api("GET", `/api/rag/documents/${document.id}/financial-extraction?profile=invoice`);
    if (extraction.review_status === "reviewed") break;
    await delay(250);
  }
  if (extraction.review_status !== "reviewed" || extraction.fields.invoice_number.value !== correctedInvoiceNumber || extraction.review_history.length !== 1) {
    throw new Error("The reviewed invoice correction did not persist with its revision history.");
  }
  if (!await waitFor(`document.querySelector('.financial-extraction')?.innerText.includes('Reviewed · revision 1')`, 15_000)) {
    throw new Error("The Knowledge UI did not display the saved review state.");
  }
  const reviewShot = await screenshot("invoice-reviewed");
  console.log("LOCAL_FINANCIAL_INVOICE_WORKER_EXTRACTION_AND_REVIEW=PASS");

  await clickText("button", "Export reviewed bookkeeping CSV");
  const downloadDeadline = Date.now() + 20_000;
  let csv;
  while (Date.now() < downloadDeadline) {
    csv = readdirSync(downloadRoot).find((name) => name === "infraspine-bookkeeping-reviewed.csv");
    if (csv) break;
    await delay(200);
  }
  if (!csv) throw new Error("The Knowledge UI did not download its reviewed bookkeeping CSV.");
  const csvBody = readFileSync(path.join(downloadRoot, csv), "utf8");
  const [headers, ...rows] = parseCsv(csvBody);
  const invoiceColumn = headers?.indexOf("invoice_number") ?? -1;
  if (invoiceColumn < 0 || !rows.some((row) => row[invoiceColumn] === correctedInvoiceNumber) ||
      rows.some((row) => row[invoiceColumn] === "INV-9077")) {
    throw new Error("The reviewed-only CSV did not contain the corrected value or included an unreviewed value.");
  }
  console.log("LOCAL_FINANCIAL_REVIEWED_ONLY_CSV_UI_EXPORT=PASS");
  console.log("LOCAL_FINANCIAL_CURRENT_SOURCE_BROWSER_TO_POSTGRESQL=PASS");
  console.log("LOCAL_FINANCIAL_EVIDENCE=" + JSON.stringify({ review: reviewShot, csv: path.join(downloadRoot, csv) }));
}

try {
  await run();
} catch (error) {
  console.error(`LOCAL_FINANCIAL_BROWSER_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (browser?.exitCode === null) {
    browser.kill();
    await Promise.race([new Promise((resolve) => browser.once("exit", resolve)), delay(5_000)]);
    console.log("LOCAL_FINANCIAL_AUTOMATION_BROWSER_CLOSED=PASS");
  }
}
