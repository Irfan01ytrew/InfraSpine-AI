#!/usr/bin/env node
/** Authenticated current-source browser acceptance for tracked obligations and replacement-version checks. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const appUrl = process.env.INFRASPINE_OBLIGATION_REVIEW_URL;
const adminEmail = process.env.INFRASPINE_OBLIGATION_REVIEW_ADMIN_EMAIL;
const adminPassword = process.env.INFRASPINE_OBLIGATION_REVIEW_ADMIN_PASSWORD;
const employeeEmail = process.env.INFRASPINE_OBLIGATION_REVIEW_EMPLOYEE_EMAIL;
const employeePassword = process.env.INFRASPINE_OBLIGATION_REVIEW_EMPLOYEE_PASSWORD;
const v1Pdf = process.env.INFRASPINE_OBLIGATION_REVIEW_V1_PDF;
const v2Pdf = process.env.INFRASPINE_OBLIGATION_REVIEW_V2_PDF;
const reviewRoot = process.env.INFRASPINE_OBLIGATION_REVIEW_ROOT;
const browserPath = process.env.INFRASPINE_BROWSER_PATH || [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((candidate) => candidate && existsSync(candidate));
if (!appUrl || !adminEmail || !adminPassword || !employeeEmail || !employeePassword || !v1Pdf || !v2Pdf || !reviewRoot) {
  throw new Error("The protected synthetic obligation-review harness is incomplete.");
}
const origin = new URL(appUrl);
if (origin.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(origin.hostname)) {
  throw new Error("The obligation-review browser must target loopback HTTP only.");
}
if (!browserPath || !existsSync(browserPath) || !existsSync(v1Pdf) || !existsSync(v2Pdf)) {
  throw new Error("The browser or synthetic contract pair is unavailable.");
}

const stamp = new Date().toISOString().replace(/[-:.]/g, "").replace("T", "-").replace("Z", "");
const profile = path.join(reviewRoot, `obligation-review-profile-${stamp}`);
const evidenceRoot = path.join(reviewRoot, "obligation-review-evidence");
mkdirSync(profile, { recursive: true });
mkdirSync(evidenceRoot, { recursive: true });
let filename = path.basename(v1Pdf);
const changedQuote = "Supplier shall deliver replacement parts within 30 days after written request.";
const retainedQuote = "Supplier shall keep goods insured while they are in transit.";
let deliveryTitle = `Delivery window ${stamp}`;
let insuranceTitle = `Transit insurance ${stamp}`;
let browser;
let socket;
let sequence = 0;
const pending = new Map();

function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Timed out during obligation-review browser step ${method}.`));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) {
    const className = result.exceptionDetails.exception?.className ?? "BrowserException";
    throw new Error(`An obligation-review browser action raised ${className}.`);
  }
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
  if (!ok) throw new Error("An expected obligation-review input was missing.");
}

async function fillCreateField(label, value) {
  const ok = await evaluate(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const form=item?.querySelector('.obligation-create');const field=[...form?.querySelectorAll('label')??[]].find(x=>x.querySelector('.field-label')?.textContent.trim()===${JSON.stringify(label)});const el=field?.querySelector('input,textarea');if(!el)return false;const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!ok) throw new Error("An expected source-obligation field was missing.");
}

async function clickText(selector, text) {
  const ok = await evaluate(`(()=>{const el=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(text)}&&!x.disabled);if(!el)return false;el.click();return true})()`);
  if (!ok) throw new Error(`Expected enabled obligation-review action was missing: ${text}`);
}

async function clickInObligation(title, action) {
  const ready = await waitFor(`(()=>{const card=[...document.querySelectorAll('.document-lifecycle .obligation-card')].find(x=>x.querySelector('.obligation-heading strong')?.textContent.trim()===${JSON.stringify(title)});return !![...card?.querySelectorAll('button')??[]].find(x=>x.textContent.trim()===${JSON.stringify(action)}&&!x.disabled)})()`, 15_000);
  const ok = ready && await evaluate(`(()=>{const card=[...document.querySelectorAll('.document-lifecycle .obligation-card')].find(x=>x.querySelector('.obligation-heading strong')?.textContent.trim()===${JSON.stringify(title)});const button=[...card?.querySelectorAll('button')??[]].find(x=>x.textContent.trim()===${JSON.stringify(action)}&&!x.disabled);button?.click();return !!button})()`);
  if (!ok) {
    const state = await evaluate(`(()=>{const card=[...document.querySelectorAll('.document-lifecycle .obligation-card')].find(x=>x.querySelector('.obligation-heading strong')?.textContent.trim()===${JSON.stringify(title)});return {card:!!card,status:card?.querySelector('.obligation-heading .pill')?.textContent.trim()||'',buttons:[...card?.querySelectorAll('button')??[]].map(x=>({label:x.textContent.trim(),disabled:x.disabled})),reason:!![...card?.querySelectorAll('label')??[]].find(x=>x.querySelector('.field-label')?.textContent.trim()==='Reason for this change')}})()`);
    throw new Error(`Expected obligation action was unavailable: ${action} (card=${!!state?.card}, status=${state?.status || "none"}, buttons=${JSON.stringify(state?.buttons ?? [])}, reasonField=${!!state?.reason}).`);
  }
}

async function rawApi(method, route, body = undefined) {
  return evaluate(`(async()=>{const init={method:${JSON.stringify(method)},credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'InfraSpineAdmin'}};${body === undefined ? "" : `init.headers['Content-Type']='application/json';init.body=JSON.stringify(${JSON.stringify(body)});`}const r=await fetch(${JSON.stringify(route)},init);let data=null;try{data=await r.json()}catch{}return {status:r.status,data}})()`);
}

async function api(method, route, body = undefined) {
  const response = await rawApi(method, route, body);
  if (response.status < 200 || response.status >= 300) throw new Error(`Obligation-review API returned HTTP ${response.status}: ${method} ${route}`);
  return response.data;
}

async function signIn(email, password, role) {
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`, 45_000)) {
    throw new Error("The local obligation-review sign-in page did not load.");
  }
  await fill('form.login-form input[name="email"]', email);
  await fill('form.login-form input[name="password"]', password);
  await evaluate(`document.querySelector('form.login-form')?.requestSubmit()`);
  if (!await waitFor(`!!document.querySelector('.user-chip-role')`, 45_000)) throw new Error("Synthetic account sign-in failed.");
  if ((await api("GET", "/api/auth/me")).role !== role) throw new Error("The signed-in synthetic account has the wrong role.");
}

async function signOut() {
  const clicked = await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Sign out"]');button?.click();return !!button})()`);
  if (!clicked || !await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`, 20_000)) {
    throw new Error("The synthetic review account did not sign out cleanly.");
  }
}

async function openKnowledge() {
  if (!await waitFor(`!![...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.trim()==='Knowledge')`, 20_000)) {
    throw new Error("The Knowledge navigation was unavailable.");
  }
  await clickText("button.nav-item", "Knowledge");
  if (!await waitFor(`document.querySelector('main h1,h1')?.textContent.trim()==='Knowledge'`, 20_000)) {
    throw new Error("The Knowledge page did not open.");
  }
}

async function connect() {
  const sanitizedEnv = { ...process.env };
  for (const name of [
    "INFRASPINE_OBLIGATION_REVIEW_URL", "INFRASPINE_OBLIGATION_REVIEW_ADMIN_EMAIL", "INFRASPINE_OBLIGATION_REVIEW_ADMIN_PASSWORD",
    "INFRASPINE_OBLIGATION_REVIEW_EMPLOYEE_EMAIL", "INFRASPINE_OBLIGATION_REVIEW_EMPLOYEE_PASSWORD",
    "INFRASPINE_OBLIGATION_REVIEW_V1_PDF", "INFRASPINE_OBLIGATION_REVIEW_V2_PDF", "INFRASPINE_OBLIGATION_REVIEW_ROOT",
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
  if (!existsSync(activePort)) throw new Error("The isolated obligation-review browser did not start.");
  const port = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline && browser.exitCode === null) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      if (response.ok) page = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("The obligation-review browser page did not become ready.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("Obligation-review browser connection failed.")), { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const entry = pending.get(message.id);
    clearTimeout(entry.timer);
    pending.delete(message.id);
    if (message.error) {
      let detail = String(message.error.message ?? "").replace(/[\r\n]+/g, " ").slice(0, 160);
      for (const secret of [adminPassword, employeePassword, adminEmail, employeeEmail]) {
        if (secret) detail = detail.split(secret).join("[redacted]");
      }
      entry.reject(new Error(`Obligation-review browser command failed: ${entry.method} (CDP ${message.error.code ?? "unknown"}${detail ? `: ${detail}` : ""}).`));
    }
    else entry.resolve(message.result);
  });
  await call("Page.enable");
  await call("Runtime.enable");
  await call("DOM.enable");
  await call("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: path.join(profile, "downloads"), eventsEnabled: true });
  await call("Emulation.setDeviceMetricsOverride", { width: 1365, height: 900, deviceScaleFactor: 1, mobile: false });
  await call("Page.navigate", { url: appUrl });
}

async function uploadInitial() {
  const root = await call("DOM.getDocument", { depth: -1, pierce: true });
  const input = await call("DOM.querySelector", { nodeId: root.root.nodeId, selector: 'input[type="file"][multiple][accept*=".pdf"]' });
  if (!input.nodeId) throw new Error("The synthetic contract upload input was missing.");
  await call("DOM.setFileInputFiles", { files: [v1Pdf], nodeId: input.nodeId });
  const deadline = Date.now() + 25_000;
  let document;
  while (Date.now() < deadline) {
    document = (await api("GET", "/api/rag/documents")).documents.find((item) => item.filename === filename);
    if (document) break;
    await delay(250);
  }
  if (!document) throw new Error("The synthetic contract did not create a document row.");
  return waitUntilIndexed(document.id, document);
}

async function findPriorObligationSource() {
  const documents = (await api("GET", "/api/rag/documents")).documents
    .filter((item) => item.filename.startsWith("obligation-review-") && item.filename.endsWith(".pdf"))
    .sort((a, b) => b.version - a.version || new Date(b.updated_at) - new Date(a.updated_at));
  for (const item of documents) {
    const response = await rawApi("GET", `/api/rag/documents/${item.id}/obligations`);
    if (response.status !== 200) continue;
    const obligations = response.data.obligations ?? [];
    const delivery = obligations.find((row) => row.title.startsWith("Delivery window ") && row.source_quote === changedQuote);
    const insurance = obligations.find((row) => row.title.startsWith("Transit insurance ") && row.source_quote === retainedQuote);
    if (delivery && insurance) return { document: item, obligations, delivery, insurance };
  }
  return null;
}

async function waitUntilIndexed(documentId, document) {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    document = await api("GET", `/api/rag/documents/${documentId}`);
    if (document.status === "ready" || document.status === "failed") break;
    await delay(500);
  }
  if (document.status !== "ready" || document.chunk_count < 1) throw new Error("The current synthetic contract version was not indexed by the local worker.");
  return document;
}

async function openLifecycle(documentId, expectCreate = true) {
  const opened = await evaluate(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const details=item?.querySelector('.document-lifecycle');if(!details)return false;if(!details.open)details.querySelector('summary')?.click();return true})()`);
  const panelExpression = expectCreate
    ? `(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const panel=item?.querySelector('.document-lifecycle');return !!panel?.open&&!!panel.querySelector("form.obligation-create")})()`
    : `(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});return !!item?.querySelector('.document-lifecycle')?.open})()`;
  const panelReady = await waitFor(panelExpression, 15_000);
  if (!opened || !panelReady) {
    const state = await evaluate(`(()=>{const cards=[...document.querySelectorAll('.doc-item')];const item=cards.find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const panel=item?.querySelector('.document-lifecycle');return {card:!!item,panel:!!panel,open:!!panel?.open,obligations:!!panel?.querySelector('[aria-label="Document obligations"]'),create:!!panel?.querySelector('form.obligation-create'),cards:cards.length}})()`);
    throw new Error(`The document version/obligation panel did not open (card=${!!state?.card}, panel=${!!state?.panel}, open=${!!state?.open}, obligations=${!!state?.obligations}, create=${!!state?.create}, visibleCards=${state?.cards ?? 0}).`);
  }
}

async function createObligation(title, quote) {
  const formReady = await waitFor(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const button=item?.querySelector('.obligation-create button');return !!button&&!button.disabled})()`, 15_000);
  if (!formReady) throw new Error("The selected document's obligation form did not finish loading.");
  await fillCreateField("Obligation", title);
  await fillCreateField("Exact source quote", quote);
  await fillCreateField("Tracking rationale", "Human review: track this specific source duty and verify it against each replacement.");
  await fillCreateField("Assignee email (blank assigns to you)", employeeEmail);
  const clicked = await evaluate(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const button=item?.querySelector('.obligation-create button');if(!button||button.disabled||button.textContent.trim()!=='Create obligation')return false;button.click();return true})()`);
  if (!clicked) throw new Error("The selected document's Create obligation action was not enabled.");
  const deadline = Date.now() + 20_000;
  let obligations = [];
  while (Date.now() < deadline) {
    obligations = (await api("GET", `/api/rag/documents/${currentDocument.id}/obligations`)).obligations;
    if (obligations.some((item) => item.title === title && item.source_quote.split(/\s+/).join(" ") === quote)) break;
    await delay(250);
  }
  if (!obligations.some((item) => item.title === title && item.source_quote.split(/\s+/).join(" ") === quote)) {
    const formState = await evaluate(`(()=>{const form=document.querySelector('.obligation-create');const alert=document.querySelector('.document-lifecycle [role="alert"]');return {form:!!form,titleFilled:!!form?.querySelector('input')?.value,quoteLength:form?.querySelector('textarea')?.value.length||0,rationaleFilled:[...form?.querySelectorAll('input')??[]].some(x=>x.value.includes('Human review')),errorVisible:!!alert}})()`);
    const titleFound = obligations.some((item) => item.title === title);
    throw new Error(`The source-quoted obligation did not persist (readback=${obligations.length}, titleMatch=${titleFound}, form=${!!formState?.form}, titleFilled=${!!formState?.titleFilled}, quoteLength=${formState?.quoteLength ?? 0}, rationaleFilled=${!!formState?.rationaleFilled}, errorVisible=${!!formState?.errorVisible}).`);
  }
  console.log("LOCAL_OBLIGATION_CREATED=" + title);
}

async function screenshot(name) {
  const result = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const output = path.join(evidenceRoot, `${name}-${stamp}.png`);
  writeFileSync(output, Buffer.from(result.data, "base64"), { flag: "wx" });
  return output;
}

let currentDocument;
let currentStep = "startup";
async function run() {
  currentStep = "browser startup";
  await connect();
  currentStep = "Admin sign-in";
  await signIn(adminEmail, adminPassword, "admin");
  currentStep = "Admin Knowledge open";
  await openKnowledge();
  currentStep = "synthetic document resume/upload and obligation creation";
  const prior = await findPriorObligationSource();
  let saved;
  if (prior) {
    currentDocument = await waitUntilIndexed(prior.document.id, prior.document);
    filename = currentDocument.filename;
    deliveryTitle = prior.delivery.title;
    insuranceTitle = prior.insurance.title;
    saved = prior.obligations;
    if (currentDocument.visibility !== "shared") {
      await api("PUT", `/api/rag/documents/${currentDocument.id}/access`, { visibility: "shared", user_emails: [], group_ids: [] });
      currentDocument = await api("GET", `/api/rag/documents/${currentDocument.id}`);
      filename = currentDocument.filename;
    }
  } else {
    const existingDoc = (await api("GET", "/api/rag/documents")).documents.find((item) => item.filename === filename);
    currentDocument = existingDoc ? await waitUntilIndexed(existingDoc.id, existingDoc) : await uploadInitial();
    await openLifecycle(currentDocument.id);
    const beforeCreate = (await api("GET", `/api/rag/documents/${currentDocument.id}/obligations`)).obligations;
    const priorDelivery = beforeCreate.find((item) => item.title.startsWith("Delivery window ") && item.source_quote === changedQuote);
    const priorInsurance = beforeCreate.find((item) => item.title.startsWith("Transit insurance ") && item.source_quote === retainedQuote);
    if (priorDelivery) deliveryTitle = priorDelivery.title;
    if (priorInsurance) insuranceTitle = priorInsurance.title;
    if (!priorDelivery) await createObligation(deliveryTitle, changedQuote);
    if (!priorInsurance) await createObligation(insuranceTitle, retainedQuote);
    saved = (await api("GET", `/api/rag/documents/${currentDocument.id}/obligations`)).obligations;
  }
  if (saved.length !== 2 || saved.some((item) => item.source_version !== 1) ||
      (currentDocument.version === 1 && saved.some((item) => !item.source_current)) ||
      (currentDocument.version === 2 && saved.some((item) => item.source_current))) {
    throw new Error("The source-obligation records do not match the retained version state.");
  }
  console.log("LOCAL_OBLIGATION_ADMIN_SOURCE_QUOTE_CREATION=PASS");

  currentStep = "Employee sign-in and Knowledge open";
  await signOut();
  await signIn(employeeEmail, employeePassword, "employee");
  await openKnowledge();
  const employeeCanList = (await api("GET", "/api/rag/documents")).documents.some((item) => item.id === currentDocument.id);
  if (!employeeCanList) throw new Error("The assigned Employee could not list the shared synthetic document.");
  currentStep = "Employee obligation panel";
  await openLifecycle(currentDocument.id, false);
  if (!await waitFor(`(()=>{const cards=[...document.querySelectorAll('.document-lifecycle .obligation-card')];return cards.some(x=>x.querySelector('.obligation-heading strong')?.textContent.trim()===${JSON.stringify(deliveryTitle)})&&cards.some(x=>x.querySelector('.obligation-heading strong')?.textContent.trim()===${JSON.stringify(insuranceTitle)})})()`, 15_000)) {
    throw new Error("The assigned Employee could not see the source obligations in the shared document.");
  }
  const permissionCheck = (await api("GET", `/api/rag/documents/${currentDocument.id}/obligations`)).obligations.find((item) => item.title === deliveryTitle);
  if (!permissionCheck) throw new Error("The assigned Employee obligation API item was missing.");
  console.log("LOCAL_OBLIGATION_EMPLOYEE_STATE=" + JSON.stringify({ status: permissionCheck.status, sourceCurrent: permissionCheck.source_current, canUpdateStatus: permissionCheck.can_update_status, historyEntries: permissionCheck.history.length }));
  if (!permissionCheck.can_update_status) throw new Error("The assigned Employee status action was not authorized.");
  let acknowledged = permissionCheck.status === "acknowledged" && permissionCheck.history.length === 2;
  if (currentDocument.version === 1 && !permissionCheck.source_current) throw new Error("The source unexpectedly changed before employee acknowledgement.");
  if (currentDocument.version === 1 && permissionCheck.status === "open") {
    const reasonSet = await evaluate(`(()=>{const card=[...document.querySelectorAll('.document-lifecycle .obligation-card')].find(x=>x.querySelector('.obligation-heading strong')?.textContent.trim()===${JSON.stringify(deliveryTitle)});const label=[...card?.querySelectorAll('label')??[]].find(x=>x.querySelector('.field-label')?.textContent.trim()==='Reason for this change');const input=label?.querySelector('input');if(!input)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'Employee acknowledged the current source-backed delivery duty.');input.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
    if (!reasonSet) throw new Error("The Employee status rationale field was missing.");
    await clickInObligation(deliveryTitle, "Acknowledge");
    const deadline = Date.now() + 20_000;
    while (Date.now() < deadline) {
      const updated = (await api("GET", `/api/rag/documents/${currentDocument.id}/obligations`)).obligations.find((item) => item.title === deliveryTitle);
      if (updated?.status === "acknowledged" && updated.history.length === 2) { acknowledged = true; break; }
      await delay(250);
    }
  } else if (permissionCheck.status !== "acknowledged" || permissionCheck.history.length !== 2) {
    throw new Error("The Employee obligation was not open or already acknowledged once.");
  }
  if (!acknowledged) throw new Error("The Employee acknowledgement did not persist with history.");
  console.log("LOCAL_OBLIGATION_EMPLOYEE_ACKNOWLEDGEMENT=PASS");

  currentStep = "Employee signout after acknowledgement";
  await signOut();
  currentStep = "Admin re-signin after acknowledgement";
  await signIn(adminEmail, adminPassword, "admin");
  currentStep = "Admin Knowledge after acknowledgement";
  await openKnowledge();
  currentStep = currentDocument.version === 1 ? "Admin replacement control" : "Admin open version comparison panel";
  let replacement;
  if (currentDocument.version === 1) {
    const replaceOpened = await evaluate(`(()=>{const item=[...document.querySelectorAll('.doc-item')].find(x=>x.querySelector('.doc-title strong')?.textContent.trim()===${JSON.stringify(filename)});const button=item?.querySelector('button[aria-label="Replace"]');button?.click();return !!button})()`);
    if (!replaceOpened) throw new Error("The synthetic contract replacement control was missing.");
    const dom = await call("DOM.getDocument", { depth: -1, pierce: true });
    const replaceInput = await call("DOM.querySelector", { nodeId: dom.root.nodeId, selector: 'input[type="file"][accept*=".pdf"]:not([multiple])' });
    if (!replaceInput.nodeId) throw new Error("The document replacement input was missing.");
    await call("DOM.setFileInputFiles", { files: [v2Pdf], nodeId: replaceInput.nodeId });
    const replacementDeadline = Date.now() + 120_000;
    while (Date.now() < replacementDeadline) {
      replacement = await api("GET", `/api/rag/documents/${currentDocument.id}`);
      if (replacement.version === 2 && (replacement.status === "ready" || replacement.status === "failed")) break;
      await delay(500);
    }
  } else {
    replacement = await api("GET", `/api/rag/documents/${currentDocument.id}`);
  }
  if (replacement.version !== 2 || replacement.status !== "ready" || replacement.indexed_version !== 2 || replacement.chunk_count < 1) {
    throw new Error("The replacement contract did not finish current-version indexing.");
  }
  currentDocument = replacement;
  filename = currentDocument.filename;
  currentStep = "Admin open version comparison panel";
  await openLifecycle(currentDocument.id, false);
  currentStep = "Admin load comparison selectors";
  if (!await waitFor(`(()=>{const selects=[...document.querySelectorAll('.lifecycle-compare select')];return !!(selects.length===2&&selects[0].querySelector('option[value="1"]')&&selects[1].querySelector('option[value="2"]'))})()`, 20_000)) {
    throw new Error("The UI did not load both retained contract versions for comparison.");
  }
  await fill(".lifecycle-compare label:nth-of-type(1) select", "1");
  await fill(".lifecycle-compare label:nth-of-type(2) select", "2");
  await clickText(".lifecycle-compare button", "Compare text");
  const diffReady = await waitFor(`(()=>{const text=document.querySelector('.document-diff')?.innerText||'';return text.includes('10 days')&&text.includes('30 days')&&text.includes('Exact source quote remains in v2')&&text.includes('Exact source quote was not found in v2')&&text.includes('does not determine legal meaning')})()`, 25_000);
  if (!diffReady) throw new Error("The version diff or exact-quote continuity notices did not render as expected.");
  const comparison = await api("GET", `/api/rag/documents/${currentDocument.id}/compare?from_version=1&to_version=2`);
  const impacts = Object.fromEntries(comparison.obligation_impacts.map((item) => [item.title, item.quote_found_in_to_version]));
  if (impacts[deliveryTitle] !== false || impacts[insuranceTitle] !== true || comparison.added_lines < 1 || comparison.removed_lines < 1) {
    throw new Error("The persisted version comparison did not distinguish changed and retained source quotes.");
  }
  const comparisonShot = await screenshot("obligation-version-comparison");
  console.log("LOCAL_OBLIGATION_REPLACEMENT_DIFF_AND_QUOTE_CONTINUITY=PASS");

  currentStep = "Employee stale-source refusal";
  await signOut();
  await signIn(employeeEmail, employeePassword, "employee");
  await openKnowledge();
  await openLifecycle(currentDocument.id, false);
  const staleNotice = await waitFor(`(()=>{const cards=[...document.querySelectorAll('.document-lifecycle .obligation-card')];const item=cards.find(x=>x.querySelector('.obligation-heading strong')?.textContent.trim()===${JSON.stringify(deliveryTitle)});return !!item?.innerText.includes('The source has changed')&&!item.querySelector('.obligation-actions button')})()`, 20_000);
  if (!staleNotice) throw new Error("The Employee UI did not withhold status changes against the stale source version.");
  const staleAttempt = await rawApi("PATCH", `/api/rag/document-obligations/${saved.find((item) => item.title === deliveryTitle).id}`, {
    expected_version: 2, status: "completed", rationale: "Reject status change against superseded source version.",
  });
  if (staleAttempt.status !== 409) throw new Error("The stale-source Employee status update was not rejected with HTTP 409.");
  console.log("LOCAL_OBLIGATION_STALE_SOURCE_UPDATE_REFUSED=PASS");

  currentStep = "Admin access revocation and Employee refusal";
  await signOut();
  await signIn(adminEmail, adminPassword, "admin");
  const revoked = await api("PUT", `/api/rag/documents/${currentDocument.id}/access`, { visibility: "private", user_emails: [], group_ids: [] });
  if (revoked.visibility !== "private") throw new Error("The synthetic shared-document access revocation did not persist.");
  await signOut();
  await signIn(employeeEmail, employeePassword, "employee");
  const employeeDocs = await api("GET", "/api/rag/documents");
  if (employeeDocs.documents.some((item) => item.id === currentDocument.id)) {
    throw new Error("The Employee still sees the document after Admin revocation.");
  }
  const withheld = await rawApi("GET", `/api/rag/documents/${currentDocument.id}/obligations`);
  if (withheld.status !== 404) throw new Error("The revoked document's obligation endpoint did not return 404 for the Employee.");
  console.log("LOCAL_OBLIGATION_ACL_REVOCATION=PASS");
  console.log("LOCAL_OBLIGATION_CURRENT_SOURCE_BROWSER_TO_POSTGRESQL=PASS");
  console.log("LOCAL_OBLIGATION_EVIDENCE=" + JSON.stringify({ comparison: comparisonShot }));
}

try {
  await run();
} catch (error) {
  console.error(`LOCAL_OBLIGATION_BROWSER_ACCEPTANCE=FAIL step=${currentStep} ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (browser?.exitCode === null) {
    browser.kill();
    await Promise.race([new Promise((resolve) => browser.once("exit", resolve)), delay(5_000)]);
    console.log("LOCAL_OBLIGATION_AUTOMATION_BROWSER_CLOSED=PASS");
  }
}
