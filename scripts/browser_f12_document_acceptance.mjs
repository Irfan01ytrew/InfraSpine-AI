#!/usr/bin/env node
/** Real loopback browser + isolated PostgreSQL + separate local-model worker acceptance. */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const url = new URL(process.env.INFRASPINE_F12_DOC_URL ?? "http://127.0.0.1:5178/admin/#/partner-workspaces");
const adminEmail = process.env.INFRASPINE_F12_ADMIN_USER;
const adminPassword = process.env.INFRASPINE_F12_ADMIN_PASSWORD;
const partnerPassword = process.env.INFRASPINE_F12_PARTNER_PASSWORD;
const profileRoot = process.env.INFRASPINE_F12_DOC_PROFILE_ROOT;
const evidenceRoot = process.env.INFRASPINE_F12_DOC_EVIDENCE;
const fixtureRoot = process.env.INFRASPINE_F12_DOC_FIXTURES;
const python = process.env.INFRASPINE_F12_DOC_PYTHON;
const workerCwd = process.env.INFRASPINE_F12_DOC_WORKER_CWD;
const model = process.env.INFRASPINE_F12_DOC_MODEL;
if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(url.hostname)) throw new Error("Loopback HTTP is required.");
if (![adminEmail, adminPassword, partnerPassword, profileRoot, evidenceRoot, fixtureRoot, python, workerCwd, model].every(Boolean)) {
  throw new Error("F12 document acceptance configuration is incomplete.");
}
const chrome = ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe")].find((item) => item && existsSync(item));
if (!chrome) throw new Error("Chrome or Edge is unavailable.");
mkdirSync(profileRoot, { recursive: true }); mkdirSync(evidenceRoot, { recursive: true });
const stamp = Date.now().toString(36);
const profile = path.resolve(profileRoot, `f12-doc-${stamp}`);
if (!profile.startsWith(path.resolve(profileRoot) + path.sep)) throw new Error("Unsafe browser profile path.");
const browser = spawn(chrome, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"],
  { stdio: "ignore", windowsHide: true });
let socket, worker;
let keepWorker = false;
const downloads = path.join(evidenceRoot, `downloads-${stamp}`);
mkdirSync(downloads, { recursive: true });
const pending = new Map(); let sequence = 0;
async function waitFor(predicate, label, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { if (await predicate()) return; await delay(250); }
  throw new Error(`Timed out waiting for ${label}.`);
}
function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out during ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 20_000) {
  const response = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (response.exceptionDetails) throw new Error("Browser evaluation failed.");
  return response.result?.value;
}
async function fill(selector, value) {
  return evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)return false;const p=e instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
}
async function api(method, route, body) {
  return evaluate(`(async()=>{const init={method:${JSON.stringify(method)},credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'InfraSpineAdmin'}};${body === undefined ? "" : `init.headers['Content-Type']='application/json';init.body=JSON.stringify(${JSON.stringify(body)});`}const r=await fetch(${JSON.stringify(route)},init);let b=null;try{b=await r.json()}catch{}return {status:r.status,body:b}})()`);
}
function startWorker(label, modelPath, retrySeconds) {
  const stdout = openSync(path.join(evidenceRoot, `worker-${label}-${stamp}.stdout.log`), "w");
  const stderr = openSync(path.join(evidenceRoot, `worker-${label}-${stamp}.stderr.log`), "w");
  const workerEnv = { ...process.env, RAG_EMBEDDING_MODEL: modelPath, RAG_EMBEDDING_ALLOW_DOWNLOAD: "false",
    RAG_RETRY_BASE_SECONDS: String(retrySeconds), RAG_MAX_ATTEMPTS: "5", RAG_PARSE_ISOLATION: "true" };
  delete workerEnv.INFRASPINE_F12_ADMIN_PASSWORD;
  delete workerEnv.INFRASPINE_F12_PARTNER_PASSWORD;
  const child = spawn(python, ["-m", "app.worker", "--concurrency", "1"], {
    cwd: workerCwd, windowsHide: true, detached: true, stdio: ["ignore", stdout, stderr], env: workerEnv,
  });
  closeSync(stdout); closeSync(stderr);
  return child;
}
async function stopWorker(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  await new Promise((resolve) => { const timer = setTimeout(resolve, 10_000); child.once("exit", () => { clearTimeout(timer); resolve(); }); child.kill(); });
  if (child.exitCode === null && child.signalCode === null) throw new Error("Temporary partner worker did not stop.");
}
async function attachFile(filename) {
  const base64 = readFileSync(path.join(fixtureRoot, filename)).toString("base64");
  return evaluate(`(()=>{const input=document.querySelector('form.partner-admin-source-upload input[type=file]');if(!input)return false;const t=new DataTransfer();t.items.add(new File([Uint8Array.from(atob(${JSON.stringify(base64)}),c=>c.charCodeAt(0))],${JSON.stringify(filename)},{type:'application/octet-stream'}));input.files=t.files;input.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
}

try {
  const activePort = path.join(profile, "DevToolsActivePort");
  await waitFor(() => existsSync(activePort), "browser debugger", 20_000);
  const [port] = readFileSync(activePort, "utf8").trim().split(/\r?\n/);
  let target;
  await waitFor(async () => { try { const r = await fetch(`http://127.0.0.1:${port}/json/list`); target = (await r.json()).find((x) => x.type === "page" && x.webSocketDebuggerUrl); return Boolean(target); } catch { return false; } }, "browser target");
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => { const message = JSON.parse(event.data); const item = pending.get(message.id); if (!item) return; clearTimeout(item.timer); pending.delete(message.id); if (message.error) item.reject(new Error("Browser protocol failure.")); else item.resolve(message.result); });
  await call("Page.enable"); await call("Runtime.enable");
  await call("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloads, eventsEnabled: true });
  await call("Page.navigate", { url: url.href });
  await waitFor(() => evaluate("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))"), "Admin login");
  await fill('form.login-form input[name="email"]', adminEmail); await fill('form.login-form input[name="password"]', adminPassword);
  await evaluate("document.querySelector('form.login-form')?.requestSubmit()");
  await waitFor(async () => (await api("GET", "/api/auth/me")).body?.role === "admin", "Admin session", 45_000);
  const setup = await api("PUT", "/api/admin/deployment/company", { company_name: "F12 Document Acceptance", industry: "Services", brand_name: "F12 Documents", primary_color: "#146B61", setup_complete: true });
  if (setup.status !== 200) throw new Error("Synthetic company setup failed.");
  await evaluate("window.location.hash='#/partner-workspaces'"); await call("Page.reload");
  await waitFor(() => evaluate("Boolean(document.querySelector('form.partner-admin-workspace-form'))"), "partner administration");
  const alpha = await api("POST", "/api/admin/partner-workspaces", { slug: `f12-alpha-${stamp}`, name: `F12 Alpha ${stamp}` });
  const beta = await api("POST", "/api/admin/partner-workspaces", { slug: `f12-beta-${stamp}`, name: `F12 Beta ${stamp}` });
  if (alpha.status !== 201 || beta.status !== 201) throw new Error("Workspace creation failed.");
  await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Refresh')?.click()");
  await waitFor(() => evaluate(`Boolean(document.querySelector('select option[value="${alpha.body.id}"]'))`), "workspace selector");
  await evaluate(`(()=>{const e=document.querySelector('select');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,${JSON.stringify(alpha.body.id)});e.dispatchEvent(new Event('change',{bubbles:true}))})()`);
  await waitFor(() => evaluate("Boolean(document.querySelector('form.partner-admin-source-upload input[type=file]'))"), "source upload form");
  if (!await attachFile("quartz.pdf")) throw new Error("PDF picker unavailable.");
  await evaluate("document.querySelector('form.partner-admin-source-upload')?.requestSubmit()");
  let pdf;
  await waitFor(async () => { const r = await api("GET", `/api/admin/partner-workspaces/${alpha.body.id}/sources`); pdf = r.body?.find((x) => x.title === "quartz"); return Boolean(pdf); }, "PDF upload");
  worker = startWorker("failed-model", path.join(workerCwd, "unavailable-model"), 20);
  await waitFor(async () => { const r = await api("GET", `/api/admin/partner-workspaces/${alpha.body.id}/sources`); pdf = r.body?.find((x) => x.id === pdf.id); return pdf?.index_status === "pending" && pdf.index_attempts >= 1 && pdf.index_error_code === "processing_error"; }, "worker transient failure", 120_000);
  await stopWorker(worker); worker = startWorker("local-model", model, 1);
  console.log("F12_DOCUMENT_WORKER_TRANSIENT_RETRY=PASS");

  const officeSources = {};
  for (const [filename, title] of [["cedar.docx", "cedar"], ["dispatch.xlsx", "dispatch"], ["renewal.pptx", "renewal"]]) {
    if (!await attachFile(filename)) throw new Error("Office file picker unavailable.");
    await evaluate("document.querySelector('form.partner-admin-source-upload')?.requestSubmit()");
    await waitFor(async () => { const r = await api("GET", `/api/admin/partner-workspaces/${alpha.body.id}/sources`); officeSources[title] = r.body?.find((x) => x.title === title); return Boolean(officeSources[title]); }, `${title} upload`);
  }
  const betaData = readFileSync(path.join(fixtureRoot, "beta.pdf")).toString("base64");
  const betaSource = await evaluate(`(async()=>{const f=new FormData();f.append('file',new File([Uint8Array.from(atob(${JSON.stringify(betaData)}),c=>c.charCodeAt(0))],'beta.pdf',{type:'application/pdf'}));const r=await fetch(${JSON.stringify(`/api/admin/partner-workspaces/${beta.body.id}/sources/upload`)},{method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineAdmin'},body:f});return {status:r.status,body:await r.json()}})()`);
  if (betaSource.status !== 201) throw new Error("Beta source upload failed.");
  await waitFor(async () => { if (worker.exitCode !== null || worker.signalCode !== null) throw new Error("Local-model worker stopped unexpectedly."); const a = await api("GET", `/api/admin/partner-workspaces/${alpha.body.id}/sources`); const b = await api("GET", `/api/admin/partner-workspaces/${beta.body.id}/sources`); pdf = a.body?.find((x) => x.id === pdf.id); return pdf?.index_status === "ready" && pdf.index_attempts >= 2 && Object.values(officeSources).every((s) => a.body?.find((x) => x.id === s.id)?.index_status === "ready") && b.body?.find((x) => x.id === betaSource.body.id)?.index_status === "ready"; }, "local model processing and retry", 180_000);
  await waitFor(() => evaluate("[...document.querySelectorAll('.partner-source-list li')].filter(x=>x.innerText.includes('ready')).length>=4"), "ready source status in Admin UI", 30_000);
  console.log("F12_DOCUMENT_ADMIN_UPLOAD_AND_LOCAL_WORKER=PASS");

  const email = `f12-doc-${stamp}@example.test`;
  const invite = await api("POST", `/api/admin/partner-workspaces/${alpha.body.id}/invitations`, { email });
  if (invite.status !== 201 || !invite.body.token) throw new Error("Partner invitation failed.");
  await api("POST", "/api/auth/logout");
  const partnerUrl = new URL(url.href); partnerUrl.hash = `/partner?token=${encodeURIComponent(invite.body.token)}`;
  await call("Page.navigate", { url: partnerUrl.href });
  await waitFor(() => evaluate("document.querySelector('h1')?.textContent.trim()==='Set up partner access'"), "partner invitation");
  await fill('form.partner-auth-form input[name="password"]', partnerPassword); await fill('form.partner-auth-form input[name="confirm_password"]', partnerPassword);
  await evaluate("document.querySelector('form.partner-auth-form')?.requestSubmit()");
  await waitFor(() => evaluate("document.querySelector('h1')?.textContent.trim()==='Ask your workspace'"), "partner portal", 45_000);
  const ask = async (question, count) => { await fill("#partner-question", question); await evaluate("document.querySelector('form.partner-composer')?.requestSubmit()"); await waitFor(() => evaluate(`document.querySelectorAll('article.partner-exchange').length===${count}`), `answer ${count}`, 90_000); return evaluate("[...document.querySelectorAll('article.partner-exchange')].at(-1)?.innerText??''"); };
  const cases = [
    { question: "When does Quartz dispatch open?", source: pdf, name: "quartz.pdf", title: "quartz", expected: "09:40 UTC", location: "Page 2" },
    { question: "How quickly does Cedar support respond?", source: officeSources.cedar, name: "cedar.docx", title: "cedar", expected: "17 business hours", location: "Cedar support policy" },
    { question: "What is the Copperpine dispatch window?", source: officeSources.dispatch, name: "dispatch.xlsx", title: "dispatch", expected: "21:35 UTC", location: "Dispatch" },
    { question: "What is the Lagoon renewal quota?", source: officeSources.renewal, name: "renewal.pptx", title: "renewal", expected: "42 units", location: "Slide 1" },
  ];
  for (const [index, item] of cases.entries()) {
    const answer = await ask(item.question, index + 1);
    if (!answer.includes(item.expected) || !answer.includes(item.title) || !answer.includes(item.location) || !answer.includes("[S")) throw new Error(`Citation or answer missing for ${item.name}.`);
    const clicked = await evaluate(`(()=>{const item=[...document.querySelectorAll('article.partner-exchange')].at(-1);const card=[...item.querySelectorAll('.partner-citation')].find(c=>c.innerText.includes(${JSON.stringify(item.title)}));const link=card?.querySelector('a');if(!link)return false;link.click();return true})()`);
    if (!clicked) throw new Error(`Original source link missing for ${item.name}.`);
    const downloaded = path.join(downloads, `partner-source-${item.source.id}.${item.name.split(".").at(-1)}`);
    await waitFor(() => existsSync(downloaded), `original ${item.name} download`, 30_000);
    const expectedHash = createHash("sha256").update(readFileSync(path.join(fixtureRoot, item.name))).digest("hex");
    const actualHash = createHash("sha256").update(readFileSync(downloaded)).digest("hex");
    if (expectedHash !== actualHash) throw new Error(`Original ${item.name} hash differs.`);
  }
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await evaluate("document.querySelectorAll('article.partner-exchange').item(3)?.scrollIntoView({block:'start'})");
  const mobile = await evaluate("({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})");
  if (mobile.width !== 390 || mobile.scrollWidth !== 390) throw new Error("Partner page overflowed at 390px.");
  const screenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const screenshotPath = path.join(evidenceRoot, `f12-document-citation-${stamp}.png`); writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"));
  console.log("F12_DOCUMENT_PDF_WORD_EXCEL_POWERPOINT_CITATIONS=PASS");
  console.log("F12_DOCUMENT_ORIGINAL_FILES_SHA256=PASS"); console.log("F12_DOCUMENT_MOBILE_390PX=PASS");
  await ask("What is VEGA-7261?", 5);
  const cross = await evaluate("(()=>{const item=[...document.querySelectorAll('article.partner-exchange')].at(-1);return {answer:item?.querySelector('.partner-answer-text')?.innerText??'',citations:[...item.querySelectorAll('.partner-citation')].map(c=>c.innerText)}})()");
  if (cross.answer.includes("VEGA-7261") || cross.citations.some((value) => /\bbeta\b/i.test(value)) ||
      (await api("GET", `/api/partner/sources/${betaSource.body.id}/file`)).status !== 404) throw new Error("Cross-partner content was disclosed.");
  console.log("F12_DOCUMENT_CROSS_PARTNER_ISOLATION=PASS");
  const adminLogin = await api("POST", "/api/auth/login", { email: adminEmail, password: adminPassword });
  if (adminLogin.status !== 200) throw new Error("Admin reauthentication failed.");
  const archived = await api("DELETE", `/api/admin/partner-workspaces/${alpha.body.id}/sources/${pdf.id}`);
  if (archived.status !== 204) throw new Error("Source archive failed.");
  const afterArchive = await ask("When does Quartz dispatch open?", 6);
  if (afterArchive.includes("09:40 UTC") || afterArchive.includes("quartz") || (await api("GET", `/api/partner/sources/${pdf.id}/file`)).status !== 404) throw new Error("Archived source remained visible in a new answer.");
  const identities = await api("GET", `/api/admin/partner-workspaces/${alpha.body.id}/identities`);
  const deactivated = await api("PATCH", `/api/admin/partner-workspaces/${alpha.body.id}/identities/${identities.body[0].id}/active`, { active: false });
  if (deactivated.status !== 200 || (await api("GET", "/api/partner/auth/me")).status !== 401 ||
      (await api("GET", `/api/partner/sources/${officeSources.cedar.id}/file`)).status !== 401) throw new Error("Partner session revocation failed.");
  await fill("#partner-question", "How quickly does Cedar support respond?"); await evaluate("document.querySelector('form.partner-composer')?.requestSubmit()");
  await waitFor(() => evaluate("Boolean(document.querySelector('form.partner-auth-form'))"), "revoked partner sign-in");
  console.log("F12_DOCUMENT_SOURCE_AND_IDENTITY_REVOCATION=PASS");
  const restored = await api("PATCH", `/api/admin/partner-workspaces/${alpha.body.id}/identities/${identities.body[0].id}/active`, { active: true });
  if (restored.status !== 200) throw new Error("Could not reactivate the synthetic review partner.");
  const reviewPdfData = readFileSync(path.join(fixtureRoot, "quartz.pdf")).toString("base64");
  const reviewPdf = await evaluate(`(async()=>{const f=new FormData();f.append('file',new File([Uint8Array.from(atob(${JSON.stringify(reviewPdfData)}),c=>c.charCodeAt(0))],'quartz-review.pdf',{type:'application/pdf'}));const r=await fetch(${JSON.stringify(`/api/admin/partner-workspaces/${alpha.body.id}/sources/upload`)},{method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineAdmin'},body:f});return {status:r.status,body:await r.json()}})()`);
  if (reviewPdf.status !== 201) throw new Error("Could not restore a PDF for the synthetic review workspace.");
  await waitFor(async () => { const rows = await api("GET", `/api/admin/partner-workspaces/${alpha.body.id}/sources`); return rows.body?.find((x) => x.id === reviewPdf.body.id)?.index_status === "ready"; }, "review PDF processing", 120_000);
  writeFileSync(path.join(evidenceRoot, "review-account.json"), JSON.stringify({ email, workspace_slug: alpha.body.slug }, null, 2));
  writeFileSync(path.join(evidenceRoot, "worker-state.json"), JSON.stringify({ pid: worker.pid, state: "running", model: "local", startedBy: "f12-document-acceptance" }, null, 2));
  worker.unref(); keepWorker = true;
  console.log(`F12_DOCUMENT_BROWSER_ACCEPTANCE=PASS (${path.basename(screenshotPath)})`);
} catch (error) {
  console.error(`F12_DOCUMENT_BROWSER_ACCEPTANCE=FAIL ${error?.message ?? "unknown error"}`); process.exitCode = 1;
} finally {
  if (!keepWorker) { try { await stopWorker(worker); } catch { console.log("F12_TEMP_WORKER_CLEANUP_PENDING=true"); } }
  try { socket?.close(); } catch {}
  if (browser.exitCode === null) await new Promise((resolve) => { const timer = setTimeout(resolve, 5000); browser.once("exit", () => { clearTimeout(timer); resolve(); }); browser.kill(); });
  try { rmSync(profile, { recursive: true, force: true, maxRetries: 4, retryDelay: 500 }); }
  catch { console.log("F12_DOC_PROFILE_CLEANUP_PENDING=true"); }
}
