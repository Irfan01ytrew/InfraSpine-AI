#!/usr/bin/env node
/** Focused Admin/Manager browser acceptance for the current-source proposal coverage UI. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const appUrl = process.env.INFRASPINE_PROPOSAL_REVIEW_URL;
const adminEmail = process.env.INFRASPINE_PROPOSAL_REVIEW_ADMIN_EMAIL;
const adminPassword = process.env.INFRASPINE_PROPOSAL_REVIEW_ADMIN_PASSWORD;
const managerEmail = process.env.INFRASPINE_PROPOSAL_REVIEW_MANAGER_EMAIL;
const managerPassword = process.env.INFRASPINE_PROPOSAL_REVIEW_MANAGER_PASSWORD;
const reviewRoot = process.env.INFRASPINE_PROPOSAL_REVIEW_ROOT;
const browserPath = process.env.INFRASPINE_BROWSER_PATH || [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((candidate) => candidate && existsSync(candidate));
if (!appUrl || !adminEmail || !adminPassword || !managerEmail || !managerPassword || !reviewRoot) {
  throw new Error("The protected proposal review harness is missing a required setting.");
}
const target = new URL(appUrl);
if (target.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(target.hostname)) {
  throw new Error("The proposal review browser must target loopback HTTP only.");
}
if (!browserPath || !existsSync(browserPath)) throw new Error("Chrome or Edge is unavailable for local acceptance.");

const stamp = new Date().toISOString().replace(/[-:.]/g, "").replace("T", "-").replace("Z", "");
const profile = path.join(reviewRoot, `proposal-review-profile-${stamp}`);
const downloadRoot = path.join(profile, "downloads");
const screenshotRoot = path.join(reviewRoot, "proposal-review-evidence");
mkdirSync(downloadRoot, { recursive: true });
mkdirSync(screenshotRoot, { recursive: true });
let browser;
let socket;
let sequence = 0;
const pending = new Map();

function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Timed out during browser step ${method}.`));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await call("Runtime.evaluate", {
    expression, awaitPromise: true, returnByValue: true, userGesture: true,
  });
  if (result.exceptionDetails) throw new Error("A proposal review browser action raised an exception.");
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
  if (!ok) throw new Error(`Expected proposal review form control is missing: ${selector}`);
}

async function clickText(selector, text) {
  const ok = await evaluate(`(()=>{const el=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(text)});if(!el)return false;el.click();return true})()`);
  if (!ok) throw new Error(`Expected proposal review control is missing: ${text}`);
}

async function api(method, route, body = undefined) {
  const response = await evaluate(`(async()=>{const init={method:${JSON.stringify(method)},credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineAdmin'}};${body === undefined ? "" : `init.headers['Content-Type']='application/json';init.body=JSON.stringify(${JSON.stringify(body)});`}const r=await fetch(${JSON.stringify(route)},init);let data=null;try{data=await r.json()}catch{}return {status:r.status,data}})()`);
  if (response.status < 200 || response.status >= 300) {
    throw new Error(`Proposal review API step returned HTTP ${response.status}: ${method} ${route}`);
  }
  return response.data;
}

async function signIn(email, password, role) {
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`)) {
    throw new Error("The proposal review sign-in page was not shown.");
  }
  await fill('form.login-form input[name="email"]', email);
  await fill('form.login-form input[name="password"]', password);
  await evaluate(`document.querySelector('form.login-form')?.requestSubmit()`);
  if (!await waitFor(`!!document.querySelector('.user-chip-role')`, 45_000)) {
    throw new Error("Synthetic proposal review sign-in failed.");
  }
  const identity = await api("GET", "/api/auth/me");
  if (identity.role !== role) throw new Error("The proposal review account returned the wrong role.");
}

async function signOut() {
  await evaluate(`document.querySelector('button[aria-label="Sign out"]')?.click()`);
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`)) {
    throw new Error("Proposal review sign-out did not return to sign-in.");
  }
}

async function navigate(label) {
  await clickText("button.nav-item", label);
  if (!await waitFor(`document.querySelector('main h1, h1')?.textContent.trim()==='Tender & RFP coverage'`)) {
    throw new Error("The Tender & RFP coverage page did not open.");
  }
}

async function screenshot(name) {
  const result = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const output = path.join(screenshotRoot, `${name}-${stamp}.png`);
  if (existsSync(output)) throw new Error("Refusing to overwrite saved proposal review evidence.");
  writeFileSync(output, Buffer.from(result.data, "base64"), { flag: "wx" });
  return output;
}

async function connectBrowser() {
  const sanitizedEnv = { ...process.env };
  for (const name of [
    "INFRASPINE_PROPOSAL_REVIEW_ADMIN_EMAIL", "INFRASPINE_PROPOSAL_REVIEW_ADMIN_PASSWORD",
    "INFRASPINE_PROPOSAL_REVIEW_MANAGER_EMAIL", "INFRASPINE_PROPOSAL_REVIEW_MANAGER_PASSWORD",
    "INFRASPINE_PROPOSAL_REVIEW_URL", "INFRASPINE_PROPOSAL_REVIEW_ROOT",
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
  if (!existsSync(activePort)) throw new Error("The isolated proposal review browser did not start.");
  const debugPort = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline && browser.exitCode === null) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
      if (response.ok) page = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("The proposal review browser page did not become ready.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("The proposal review browser connection failed.")), { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const entry = pending.get(message.id);
    clearTimeout(entry.timer);
    pending.delete(message.id);
    if (message.error) entry.reject(new Error(`Proposal review browser command failed: ${entry.method}`));
    else entry.resolve(message.result);
  });
  await call("Page.enable");
  await call("Runtime.enable");
  await call("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloadRoot, eventsEnabled: true });
  await call("Emulation.setDeviceMetricsOverride", { width: 1365, height: 900, deviceScaleFactor: 1, mobile: false });
  await call("Page.navigate", { url: appUrl });
  if (!await waitFor(`!!document.querySelector('form.login-form input[name="email"]')`, 45_000)) {
    throw new Error("The current-source local review sign-in page did not load.");
  }
}

async function waitForDownload(extension, caseId) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const file = readdirSync(downloadRoot).find((name) => name.includes(caseId.slice(0, 8)) && name.endsWith(`.${extension}`));
    if (file) {
      const body = readFileSync(path.join(downloadRoot, file));
      const signatureOk = extension === "pdf" ? body.subarray(0, 5).toString() === "%PDF-" : body.subarray(0, 2).toString() === "PK";
      if (!signatureOk) throw new Error(`Proposal report ${extension.toUpperCase()} did not have a valid file signature.`);
      return file;
    }
    await delay(200);
  }
  throw new Error(`The proposal review UI did not download its ${extension.toUpperCase()} report.`);
}

async function run() {
  await connectBrowser();
  await signIn(managerEmail, managerPassword, "manager");
  const managed = await api("GET", "/api/departments/managed");
  const department = managed.groups.at(-1);
  if (!department) throw new Error("The synthetic Manager has no assigned department for proposal review.");
  await signOut();

  await signIn(adminEmail, adminPassword, "admin");
  await navigate("Tender & RFP coverage");
  let title = `Synthetic proposal review ${stamp}`;
  const requirement = "The service target for ticket F12-4817 is 48 hours.";
  let row;
  let list = await api("GET", "/api/proposal-coverage");
  row = list.cases.find((item) => item.title.startsWith("Synthetic proposal review ") && item.status === "submitted" && item.requirement_count === 1)
    ?? list.cases.find((item) => item.title.startsWith("Synthetic proposal review ") && item.status === "draft" && item.requirement_count === 1);
  if (row) {
    title = row.title;
    await evaluate(`(()=>{const item=[...document.querySelectorAll('button.proposal-case-row')].find(x=>x.innerText.includes(${JSON.stringify(title)}));item?.click()})()`);
    if (!await waitFor(`!!document.querySelector('.proposal-case-heading')`, 15_000)) {
      throw new Error("The saved synthetic proposal draft could not be reopened from the UI.");
    }
  } else {
    await fill("form.proposal-form input", title);
    await fill('form.proposal-form select:not([multiple])', department.id);
    await fill('form.proposal-form textarea:not([aria-label])', requirement);
    await clickText("button", "Create case");
    const createDeadline = Date.now() + 20_000;
    while (Date.now() < createDeadline) {
      list = await api("GET", "/api/proposal-coverage");
      row = list.cases.find((item) => item.title === title);
      if (row) break;
      await delay(250);
    }
  }
  row ??= list.cases.find((item) => item.title === title);
  if (!row) throw new Error("The current-source proposal coverage case was not saved from the UI.");
  const caseId = row.id;
  let detail = await api("GET", `/api/proposal-coverage/${caseId}`);
  if (detail.requirements.length !== 1) throw new Error("The proposal case did not retain its single synthetic requirement.");
  if (detail.status === "draft") {
    if (!detail.can_edit) throw new Error("The Admin could not edit the proposal draft.");
    if (detail.requirements[0].requirement !== requirement) {
      await fill(".proposal-edit-form textarea", requirement);
      await clickText("button", "Save draft");
      const editDeadline = Date.now() + 20_000;
      while (Date.now() < editDeadline) {
        detail = await api("GET", `/api/proposal-coverage/${caseId}`);
        if (detail.requirements[0]?.requirement === requirement) break;
        await delay(250);
      }
      if (detail.requirements[0]?.requirement !== requirement) throw new Error("The Admin could not edit the synthetic proposal requirement.");
    }
    const evidenceAction = detail.requirements[0].analyzed_at ? "Refresh evidence" : "Find evidence";
    if (!await waitFor(`[...document.querySelectorAll('button')].some(x=>x.textContent.trim()===${JSON.stringify(evidenceAction)}&&!x.disabled)`, 20_000)) {
      throw new Error("The proposal evidence action did not become available after saving the draft.");
    }
    await clickText("button", evidenceAction);
    const analysisDeadline = Date.now() + 60_000;
    while (Date.now() < analysisDeadline) {
      detail = await api("GET", `/api/proposal-coverage/${caseId}`);
      if (detail.requirements[0]?.analyzed_at) break;
      await delay(400);
    }
    const analyzed = detail.requirements[0];
    if (!analyzed?.analyzed_at || analyzed.insufficient_evidence || !analyzed.answer.includes("48 hours") || analyzed.citations.length < 1) {
      const facts = {
        hasAnalysis: Boolean(analyzed?.analyzed_at),
        insufficientEvidence: Boolean(analyzed?.insufficient_evidence),
        answerMentionsTarget: /48 hours/i.test(analyzed?.answer ?? ""),
        citationCount: analyzed?.citations?.length ?? 0,
        retrievedCount: analyzed?.retrieved_chunks?.length ?? 0,
        sourceFiles: (analyzed?.citations ?? []).map((citation) => citation.filename),
      };
      throw new Error(`The proposal UI finding was not grounded as expected: ${JSON.stringify(facts)}`);
    }
    if (!await waitFor(`!!document.querySelector('.proposal-requirement .research-sources .research-source')`, 15_000)) {
      throw new Error("The proposal UI did not render its source citation.");
    }
    await screenshot("admin-evidence-map");
    await clickText("button", "Submit for review");
    const submitted = await waitFor(`document.body.innerText.includes('submitted')`, 20_000);
    if (!submitted) throw new Error("The Admin proposal case did not enter department review.");
    detail = await api("GET", `/api/proposal-coverage/${caseId}`);
    if (detail.status !== "submitted") throw new Error("The proposal case status was not saved as submitted.");
    console.log("LOCAL_PROPOSAL_ADMIN_EVIDENCE_AND_SUBMISSION=PASS");
  } else if (detail.status === "submitted") {
    console.log("LOCAL_PROPOSAL_ADMIN_EVIDENCE_AND_SUBMISSION=PASS (resumed persisted submission)");
  } else {
    throw new Error("The synthetic proposal case is no longer in a reviewable status.");
  }

  await signOut();
  await signIn(managerEmail, managerPassword, "manager");
  await navigate("Tender & RFP coverage");
  if (!await waitFor(`[...document.querySelectorAll('button.proposal-case-row')].some(x=>x.innerText.includes(${JSON.stringify(title)}))`, 20_000)) {
    throw new Error("The scoped Manager proposal case did not appear in the review queue.");
  }
  await evaluate(`(()=>{const row=[...document.querySelectorAll('button.proposal-case-row')].find(x=>x.innerText.includes(${JSON.stringify(title)}));row?.click()})()`);
  if (!await waitFor(`!!document.querySelector('select[aria-label="Assessment for requirement 1"]')`, 20_000)) {
    throw new Error("The Manager review assessment control did not render.");
  }
  detail = await api("GET", `/api/proposal-coverage/${caseId}`);
  if (detail.assessment_count === 0) {
    await fill('select[aria-label="Assessment for requirement 1"]', "meets");
    await fill('input[aria-label="Rationale for requirement 1"]', "Synthetic answer cites the current indexed source passage.");
    if (!await waitFor(`[...document.querySelectorAll('button')].some(x=>x.textContent.trim()==='Save assessment'&&!x.disabled)`, 15_000)) {
      throw new Error("The Manager assessment action did not become available.");
    }
    await clickText("button", "Save assessment");
    const assessmentDeadline = Date.now() + 20_000;
    while (Date.now() < assessmentDeadline) {
      detail = await api("GET", `/api/proposal-coverage/${caseId}`);
      if (detail.assessment_count === 1) break;
      await delay(250);
    }
  }
  if (detail.assessment_count !== 1 || !detail.can_review) throw new Error("The Manager assessment was not persisted in the scoped case.");
  await fill(".proposal-review-panel textarea", "All checked requirements were reviewed against the cited synthetic evidence.");
  if (!await waitFor(`[...document.querySelectorAll('button')].some(x=>x.textContent.trim()==='Approve coverage'&&!x.disabled)`, 15_000)) {
    throw new Error("The Manager approval action did not become available after the rationale was entered.");
  }
  await clickText("button", "Approve coverage");
  const approvalDeadline = Date.now() + 25_000;
  while (Date.now() < approvalDeadline) {
    detail = await api("GET", `/api/proposal-coverage/${caseId}`);
    if (detail.status === "approved") break;
    await delay(250);
  }
  if (detail.status !== "approved" || detail.reviews.length !== 1) throw new Error("Manager approval or review history was not persisted.");
  await screenshot("manager-approved");
  console.log("LOCAL_PROPOSAL_MANAGER_ASSESSMENT_AND_APPROVAL=PASS");

  const exported = [];
  for (const [format, label] of [["pdf", "Export PDF"], ["docx", "Export Word"], ["xlsx", "Export Excel"]]) {
    await clickText("button", label);
    exported.push(await waitForDownload(format, caseId));
  }
  console.log("LOCAL_PROPOSAL_UI_EXPORTS=PASS (PDF, DOCX, XLSX; signatures verified)");
  console.log("LOCAL_PROPOSAL_CURRENT_SOURCE_BROWSER_TO_POSTGRESQL=PASS");
  console.log("LOCAL_PROPOSAL_EVIDENCE=" + JSON.stringify({
    admin: await screenshot("manager-approved-final"), exports: exported.map((name) => path.join(downloadRoot, name)),
  }));
}

try {
  await run();
} catch (error) {
  console.error(`LOCAL_PROPOSAL_BROWSER_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (browser?.exitCode === null) {
    browser.kill();
    await Promise.race([new Promise((resolve) => browser.once("exit", resolve)), delay(5_000)]);
    console.log("LOCAL_PROPOSAL_AUTOMATION_BROWSER_CLOSED=PASS");
  }
}
