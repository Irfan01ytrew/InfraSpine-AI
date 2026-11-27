#!/usr/bin/env node
/** Current-source role-based browser acceptance for guided knowledge interviews and SOP review. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_F03_REVIEW_URL ?? "http://127.0.0.1:5178/admin/");
const credentialPath = process.env.INFRASPINE_F03_CREDENTIAL_FIXTURE;
const reviewRoot = process.env.INFRASPINE_F03_REVIEW_ROOT;
if (base.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(base.hostname) || !credentialPath || !reviewRoot) {
  throw new Error("F03 browser acceptance requires the isolated loopback review and protected synthetic fixture.");
}
const fixture = JSON.parse(readFileSync(credentialPath, "utf8").replace(/^\uFEFF/, ""));
const admin = fixture.admin;
const manager = fixture.managers?.[0];
const employee = fixture.employees?.[0];
const outsider = fixture.employees?.[1];
if (![admin, manager, employee, outsider].every((account) => account?.email && account?.password)) {
  throw new Error("The protected F03 role fixture is incomplete.");
}
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for F03 browser acceptance.");

const departmentName = "F03 Knowledge Interview Operations";
const run = Date.now().toString(36);
const templateTitle = `F03 Handover ${run}`;
const sopTitle = `Quarterly credential handover ${run}`;
const purposeAnswer = "Ensure the next operator can safely rotate synthetic QA credentials.";
const firstStepsAnswer = "Check the approved inventory, notify the test owner, and record the rotation date.";
const revisedStepsAnswer = `${firstStepsAnswer} Verify the retired test value is rejected and update the protected local fixture.`;
const purposeKey = "question_01";
const stepsKey = "question_02";
const evidenceDir = path.join(reviewRoot, "evidence");
mkdirSync(evidenceDir, { recursive: true });
const screenshotPath = path.join(evidenceDir, "f03-knowledge-interview-approved-390.png");
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-f03-interviews-"));
let chrome;
let socket;
let sequence = 0;
const pending = new Map();

function command(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser timed out at ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 20_000) {
  const response = await command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (response.exceptionDetails) throw new Error("A current-source F03 browser action failed.");
  return response.result?.value;
}
async function waitFor(expression, label, timeoutMs = 30_000) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (await evaluate(expression)) return;
    await delay(120);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function signIn(account) {
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "login form");
  const submitted = await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const pass=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!pass||!form)return false;for(const [el,value] of [[email,${JSON.stringify(account.email)}],[pass,${JSON.stringify(account.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
  if (!submitted) throw new Error("The synthetic role login form could not be submitted.");
  await waitFor("Boolean(document.querySelector('.shell'))", "authenticated review shell", 40_000);
}
async function signOut() {
  const clicked = await evaluate(`(()=>{const b=document.querySelector('button[aria-label="Sign out"]');if(!b)return false;b.click();return true})()`);
  if (!clicked) throw new Error("The current synthetic role could not sign out through the visible control.");
  await waitFor("Boolean(document.querySelector('form.login-form'))", "login form after sign out");
}
async function openInterviews() {
  const clicked = await evaluate(`(()=>{const b=[...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.trim()==='Knowledge interviews');if(!b)return false;b.click();return true})()`);
  if (!clicked) throw new Error("Knowledge interviews navigation was unavailable for this role.");
  await waitFor("document.querySelector('h1')?.textContent.trim()==='Knowledge interviews'", "knowledge interviews page");
}
async function setField(selector, value, elementKind = "input") {
  const script = `(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const p=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:el instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`;
  if (!await evaluate(script)) throw new Error(`The synthetic interview field was unavailable (${elementKind}).`);
}
async function setLabeledField(label, value) {
  const script = `(()=>{const l=[...document.querySelectorAll('label.field')].find(x=>x.querySelector('.field-label')?.textContent.trim().startsWith(${JSON.stringify(label)}));const e=l?.querySelector('input,textarea');if(!e)return false;const p=e instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));return true})()`;
  if (!await evaluate(script)) throw new Error(`The interview answer field '${label}' was unavailable.`);
}
async function clickButton(label, within = "document") {
  const script = `(()=>{const root=${within === "document" ? "document" : `document.querySelector(${JSON.stringify(within)})`};const b=[...(root?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()===${JSON.stringify(label)});if(!b||b.disabled)return false;b.click();return true})()`;
  return Boolean(await evaluate(script));
}
async function findSessionByTitle(title) {
  return evaluate(`(async()=>{const r=await fetch('/api/knowledge-interviews/sessions');if(!r.ok)return null;return (await r.json()).sessions.find(x=>x.sop_title===${JSON.stringify(title)}||x.template_title===${JSON.stringify(title)})??null})()`);
}
async function openSession(title) {
  await waitFor(`Boolean([...document.querySelectorAll('.interview-session-card')].some(x=>x.textContent.includes(${JSON.stringify(title)})))`, "saved interview card");
  const opened = await evaluate(`(()=>{const b=[...document.querySelectorAll('.interview-session-card')].find(x=>x.textContent.includes(${JSON.stringify(title)}));if(!b)return false;b.click();return true})()`);
  if (!opened) throw new Error("The requested saved interview was not visible in this role's list.");
  await waitFor(`Boolean(document.querySelector('.interview-editor-heading h2')?.textContent.includes(${JSON.stringify(title)}))`, "selected interview editor");
}
async function readSession(sessionId) {
  return evaluate(`(async()=>{const r=await fetch(${JSON.stringify(`/api/knowledge-interviews/sessions/${sessionId}`)});return r.ok?await r.json():null})()`);
}
async function reviewApi(sessionId, decision, rationale) {
  return evaluate(`(async()=>{const r=await fetch(${JSON.stringify(`/api/knowledge-interviews/sessions/${sessionId}/review`)},{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'},body:JSON.stringify({decision:${JSON.stringify(decision)},rationale:${JSON.stringify(rationale)}})});return {status:r.status,body:await r.json().catch(()=>null)}})()`);
}

try {
  chrome = spawn(browserPath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profile, "DevToolsActivePort");
  const startBy = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < startBy) await delay(100);
  if (!existsSync(portFile)) throw new Error("Isolated Chrome did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!page?.webSocketDebuggerUrl) throw new Error("Isolated Chrome did not expose a page target.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error("Chrome DevTools command failed.")) : item.resolve(message.result);
  });
  await command("Page.enable"); await command("Runtime.enable");
  await command("Page.navigate", { url: base.href });
  await signIn(admin);
  await openInterviews();
  const createForm = await clickButton("Add interview template");
  if (!createForm) throw new Error("Admin could not open the template form.");
  await waitFor("Boolean(document.querySelector('form.interview-template-form input'))", "interview template form");
  await setLabeledField("Template title", templateTitle);
  await setLabeledField("Purpose", "Record a safe, repeatable synthetic QA credential handover.");
  await setLabeledField("Questions, one per line", "What is the goal of the process?\nWhat steps must the next person follow?");
  const savedTemplate = await clickButton("Save template", "form.interview-template-form");
  if (!savedTemplate) throw new Error("Admin could not save the reusable interview template in the UI.");
  await waitFor(`(async()=>{const r=await fetch('/api/knowledge-interviews/options');if(!r.ok)return false;const d=await r.json();return d.templates.some(x=>x.title===${JSON.stringify(templateTitle)})&&d.can_manage_templates})()`, "saved Admin-managed interview template");
  const options = await evaluate(`(async()=>{const r=await fetch('/api/knowledge-interviews/options');return r.ok?await r.json():null})()`);
  const templateId = options?.templates?.find((item) => item.title === templateTitle)?.id;
  const departmentId = options?.departments?.find((item) => item.name === departmentName)?.id;
  if (!templateId || !departmentId) throw new Error("Employee-facing interview options did not contain the scoped template and department.");
  await signOut();

  await signIn(employee);
  await openInterviews();
  await waitFor(`(()=>{const selects=[...document.querySelectorAll('.interview-start select')];return selects.length===2&&[...selects[0].options].some(x=>x.textContent.trim()===${JSON.stringify(templateTitle)})&&[...selects[1].options].some(x=>x.value===${JSON.stringify(departmentId)})})()`, "employee template and department options");
  const chooseTemplate = await evaluate(`(()=>{const l=[...document.querySelectorAll('label.field')].find(x=>x.querySelector('.field-label')?.textContent.trim()==='Template');const e=l?.querySelector('select');const o=[...(e?.options??[])].find(x=>x.textContent.trim()===${JSON.stringify(templateTitle)});if(!e||!o)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,o.value);e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  const chooseDepartment = await evaluate(`(()=>{const l=[...document.querySelectorAll('label.field')].find(x=>x.querySelector('.field-label')?.textContent.trim()==='Department');const e=l?.querySelector('select');const o=[...(e?.options??[])].find(x=>x.value===${JSON.stringify(departmentId)});if(!e||!o)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,o.value);e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!chooseTemplate || !chooseDepartment) throw new Error("Employee could not select the scoped interview template and department.");
  await waitFor("Boolean(document.querySelector('.interview-start button')&&!document.querySelector('.interview-start button').disabled)", "enabled employee interview start action");
  if (!await clickButton("Start", ".interview-start")) throw new Error("Employee could not start the department-scoped interview from the UI.");
  await waitFor("Boolean(document.querySelector('section[aria-label=\"Interview editor\"] input'))", "new editable interview");
  await setLabeledField("SOP title", sopTitle);
  await setLabeledField("What is the goal of the process?", purposeAnswer);
  if (!await clickButton("Save progress", "section[aria-label=\"Interview editor\"]")) throw new Error("Employee could not save interview progress through the UI.");
  await waitFor(`(async()=>{const r=await fetch('/api/knowledge-interviews/sessions');if(!r.ok)return false;return (await r.json()).sessions.some(x=>x.sop_title===${JSON.stringify(sopTitle)}&&x.answers?.[${JSON.stringify(purposeKey)}]===${JSON.stringify(purposeAnswer)})})()`, "persisted partial employee answers");
  const saved = await findSessionByTitle(sopTitle);
  if (!saved?.id || saved.status !== "in_progress" || saved.answers?.[purposeKey] !== purposeAnswer) throw new Error("Saved interview progress was not present in the employee's API session.");
  const sessionId = saved.id;
  await signOut();

  await signIn(employee);
  await openInterviews();
  await openSession(sopTitle);
  await waitFor(`document.querySelector('section[aria-label="Interview editor"]')?.innerText.includes(${JSON.stringify(purposeAnswer)})`, "resumed saved employee answer");
  await signOut();

  await signIn(outsider);
  await openInterviews();
  const outsideOptions = await evaluate(`(async()=>{const r=await fetch('/api/knowledge-interviews/options');return r.ok?await r.json():null})()`);
  const outsideSession = await evaluate(`(async()=>{const r=await fetch(${JSON.stringify(`/api/knowledge-interviews/sessions/${sessionId}`)});return r.status})()`);
  const outsideStart = await evaluate(`(async()=>{const r=await fetch('/api/knowledge-interviews/sessions',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'},body:JSON.stringify({template_id:${JSON.stringify(templateId)},department_group_id:${JSON.stringify(departmentId)}})});return r.status})()`);
  if (outsideOptions?.departments?.some((item) => item.id === departmentId) || outsideSession !== 404 || outsideStart !== 403) {
    throw new Error("An employee outside the department could see or start an interview in the restricted department.");
  }
  await signOut();

  await signIn(employee);
  await openInterviews();
  await openSession(sopTitle);
  await setLabeledField("What steps must the next person follow?", firstStepsAnswer);
  if (!await clickButton("Submit SOP for review", "section[aria-label=\"Interview editor\"]")) throw new Error("Employee could not submit the completed SOP through the UI.");
  await waitFor(`(async()=>{const r=await fetch(${JSON.stringify(`/api/knowledge-interviews/sessions/${sessionId}`)});if(!r.ok)return false;const d=await r.json();return d.status==='submitted'&&d.sop_revisions.length===1&&d.sop_body.includes(${JSON.stringify(firstStepsAnswer)})})()`, "submitted source-neutral human-authored SOP revision");
  const selfReview = await reviewApi(sessionId, "approved", "The author must not approve their own SOP.");
  if (selfReview.status !== 403) throw new Error("The employee author was able to review their own SOP.");
  await signOut();

  await signIn(manager);
  await openInterviews();
  await openSession(sopTitle);
  const queued = await readSession(sessionId);
  if (!queued?.can_review || queued.status !== "submitted") throw new Error("The assigned Manager did not receive the scoped SOP review.");
  await setLabeledField("Decision rationale", "Please record the final verification owner and retirement check.");
  if (!await clickButton("Request changes", "section[aria-label=\"Interview editor\"]")) throw new Error("Manager could not request changes through the review UI.");
  await waitFor("Boolean(document.querySelector('.toasts')?.innerText.includes('Changes requested'))", "Manager change-request confirmation");
  await signOut();

  await signIn(employee);
  await openInterviews();
  await openSession(sopTitle);
  const returned = await readSession(sessionId);
  if (returned?.status !== "changes_requested" || returned.reviews.length !== 1 || returned.reviews[0].decision !== "changes_requested") {
    throw new Error("The Employee did not receive the persisted Manager change request.");
  }
  await waitFor("Boolean(document.querySelector('section[aria-label=\"Interview editor\"] textarea'))", "editable interview after requested changes");
  await setLabeledField("What steps must the next person follow?", revisedStepsAnswer);
  if (!await clickButton("Submit SOP for review", "section[aria-label=\"Interview editor\"]")) throw new Error("Employee could not resubmit the revised SOP.");
  await waitFor(`(async()=>{const d=await fetch(${JSON.stringify(`/api/knowledge-interviews/sessions/${sessionId}`)}).then(r=>r.json());return d.status==='submitted'&&d.revision_number===2&&d.sop_revisions.length===2})()`, "immutable second submitted SOP revision");
  await signOut();

  await signIn(manager);
  await openInterviews();
  await openSession(sopTitle);
  await setLabeledField("Decision rationale", "Approved after the owner and retirement verification were documented.");
  if (!await clickButton("Approve SOP", "section[aria-label=\"Interview editor\"]")) throw new Error("Manager could not approve the current SOP revision in the UI.");
  await waitFor("Boolean(document.querySelector('.toasts')?.innerText.includes('SOP approved'))", "Manager approval confirmation");
  await signOut();

  await signIn(employee);
  await openInterviews();
  await openSession(sopTitle);
  const final = await readSession(sessionId);
  const revisionIntegrity = final?.sop_revisions?.length === 2 &&
    final.sop_revisions[0].body.includes(firstStepsAnswer) &&
    !final.sop_revisions[0].body.includes("protected local fixture") &&
    final.sop_revisions[1].body.includes(revisedStepsAnswer);
  if (!revisionIntegrity || final.status !== "approved" || final.reviews.length !== 2 || final.can_edit || final.can_review) {
    throw new Error("Approved SOP revision history or final authorization state was incorrect.");
  }
  await waitFor(`document.querySelector('section[aria-label="Interview editor"]')?.innerText.includes(${JSON.stringify("Approved SOP")})`, "employee read-only approved SOP");
  const mobileSet = await command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await delay(350);
  const dimensions = await evaluate("({viewport:document.documentElement.clientWidth,body:document.body.clientWidth,root:document.documentElement.scrollWidth})");
  if (!dimensions || dimensions.viewport !== 390 || dimensions.body > 390) throw new Error("The knowledge interview page body exceeded 390px.");
  const shot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(screenshotPath, Buffer.from(shot.data, "base64"));
  const evidence = {
    result: "PASS",
    scope: "current-source authenticated browser/API, synthetic roles and isolated PostgreSQL; human-authored answers, no AI-generated content",
    reviewUrl: base.href,
    database: "infraspine_f02meetai_20261028_test",
    migration: "0059_workspace_hierarchy",
    syntheticDepartment: departmentName,
    lifecycle: ["Admin template", "Employee save/resume", "outside-department 404/403", "author self-review 403", "Manager changes requested", "Employee revision 2", "Manager approval", "Employee read-only approved SOP"],
    revisionCount: final.sop_revisions.length,
    reviewCount: final.reviews.length,
    mobile390: dimensions,
    screenshot: path.relative(process.cwd(), screenshotPath).replaceAll("\\", "/"),
  };
  writeFileSync(path.join(evidenceDir, "f03-knowledge-interview-current-source.json"), JSON.stringify(evidence, null, 2) + "\n", { mode: 0o600 });
  console.log("F03_CURRENT_SOURCE_INTERVIEW_BROWSER=PASS; admin template; employee save/resume and revision 2; cross-department 404/403; self-review denied; manager change request/approval; read-only SOP; 390px");
} finally {
  socket?.close();
  if (chrome && chrome.exitCode === null) { chrome.kill(); await new Promise((resolve) => chrome.once("exit", resolve)); }
  try { await import("node:fs/promises").then(({ rm }) => rm(profile, { recursive: true, force: true })); } catch { /* only the harness's own temporary profile */ }
}
