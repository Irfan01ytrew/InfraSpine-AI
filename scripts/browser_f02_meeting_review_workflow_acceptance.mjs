#!/usr/bin/env node
/** Current-source F02 employee submission, scoped review and approved-task browser acceptance. */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const appUrl = process.env.INFRASPINE_F02_REVIEW_URL ?? "http://127.0.0.1:5178/admin/";
const fixturePath = process.env.INFRASPINE_F03_CREDENTIAL_FIXTURE;
const manifestPath = process.env.INFRASPINE_F02_WORKFLOW_MANIFEST;
const evidenceRoot = process.env.INFRASPINE_F02_REVIEW_ROOT;
const resumeMeetingId = process.env.INFRASPINE_F02_RESUME_MEETING_ID;
const resumeMeetingTitle = process.env.INFRASPINE_F02_RESUME_MEETING_TITLE;
const appOrigin = new URL(appUrl);
if (appOrigin.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(appOrigin.hostname)
  || !fixturePath || !manifestPath || !evidenceRoot) {
  throw new Error("F02 review workflow requires its protected synthetic fixture and loopback review app.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
const manager = fixture.managers?.[0];
const author = fixture.employees?.[0];
if (!manager?.email || !manager?.password || !author?.email || !author?.password
  || !manifest.source_document_id || !manifest.transcript_chunk_id) {
  throw new Error("The protected F02 reviewer/employee or source manifest is incomplete.");
}
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((candidate) => candidate && existsSync(candidate));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for isolated F02 review acceptance.");

mkdirSync(evidenceRoot, { recursive: true });
const stamp = new Date().toISOString().replace(/[-:.]/g, "").replace("T", "-").replace("Z", "");
const profile = mkdtempSync(path.join(evidenceRoot, `f02-review-profile-${stamp}-`));
const evidencePath = path.join(evidenceRoot, `f02-review-workflow-${stamp}.json`);
const screenshotPath = path.join(evidenceRoot, `f02-review-workflow-390-${stamp}.png`);
const meetingTitle = resumeMeetingTitle ?? `F02 scoped meeting review ${Date.now()}`;
let browser;
let socket;
let sequence = 0;
const pending = new Map();

function command(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out during ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 20_000) {
  const result = await command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("A current-source F02 browser step raised an exception.");
  return result.result?.value;
}
async function waitFor(expression, label, timeoutMs = 30_000) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (await evaluate(expression)) return;
    await delay(120);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function setValue(element, value) {
  const ok = await evaluate(`(()=>{const el=${element};if(!el)return false;const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:el instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!ok) throw new Error("An expected F02 meeting editor control was unavailable.");
}
async function field(label, value, index = 0) {
  const locator = `(()=>[...document.querySelectorAll('label.field')].filter(x=>x.querySelector('.field-label')?.textContent.trim()===${JSON.stringify(label)})[${index}]?.querySelector('input,select,textarea'))()`;
  await setValue(locator, value);
}
async function clickButton(label) {
  const ok = await evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()===${JSON.stringify(label)}&&!x.disabled);if(!b)return false;b.click();return true})()`);
  if (!ok) throw new Error(`The expected enabled F02 action was unavailable: ${label}`);
}
async function clickAria(label) {
  const ok = await evaluate(`(()=>{const b=document.querySelector('button[aria-label="Sign out"]');if(!b)return false;b.click();return true})()`);
  if (!ok) throw new Error(`The expected account action was unavailable: ${label}`);
}
async function api(method, route, payload) {
  return evaluate(`(async()=>{const r=await fetch(${JSON.stringify(route)},{method:${JSON.stringify(method)},credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'},${payload === undefined ? "" : `body:JSON.stringify(${JSON.stringify(payload)})`}});let data=null;try{data=await r.json()}catch{}return {status:r.status,data}})()`);
}
async function signIn(account, role) {
  await command("Page.navigate", { url: appUrl });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", `${role} login form`);
  const submitted = await evaluate(`(()=>{const form=document.querySelector('form.login-form');const email=form?.querySelector('input[name="email"]');const password=form?.querySelector('input[name="password"]');if(!form||!email||!password)return false;for(const [el,value] of [[email,${JSON.stringify(account.email)}],[password,${JSON.stringify(account.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
  if (!submitted) throw new Error(`Synthetic ${role} login form could not be submitted.`);
  await waitFor("Boolean(document.querySelector('.shell'))", `${role} authenticated shell`, 35_000);
  const me = await api("GET", "/api/auth/me");
  if (me.status !== 200 || me.data?.role !== role) throw new Error(`The authenticated synthetic account is not ${role}.`);
}
async function openMeetings() {
  await clickButton("Meeting minutes");
  await waitFor("document.querySelector('h1')?.textContent.trim()==='Meeting minutes'", "Meeting minutes page");
}
async function openMeeting(title) {
  await waitFor(`Boolean([...document.querySelectorAll('.people-row')].find(x=>x.textContent.includes(${JSON.stringify(title)})))`, "meeting row in the current role's list");
  const clicked = await evaluate(`(()=>{const row=[...document.querySelectorAll('.people-row')].find(x=>x.textContent.includes(${JSON.stringify(title)}));const button=[...(row?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Open');button?.click();return Boolean(button)})()`);
  if (!clicked) throw new Error("The saved synthetic meeting could not be opened from the UI.");
  await waitFor(`document.body.innerText.includes(${JSON.stringify(title)})&&document.body.innerText.includes('Transcript evidence')`, "meeting detail and transcript");
}

try {
  browser = spawn(browserPath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < deadline) await delay(100);
  if (!existsSync(portFile)) throw new Error("The isolated F02 Chromium session did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!page?.webSocketDebuggerUrl) throw new Error("The isolated F02 browser target was unavailable.");
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
  await command("Page.enable");
  await command("Runtime.enable");

  await signIn(author, "employee");
  await openMeetings();
  let meeting;
  if (resumeMeetingId) {
    if (!resumeMeetingTitle) throw new Error("A resumable F02 meeting ID requires its matching title.");
    const resumed = await api("GET", `/api/meetings/${resumeMeetingId}`);
    const openTask = resumed.data?.action_tasks?.[0];
    if (resumed.status !== 200 || resumed.data?.title !== meetingTitle || resumed.data?.status !== "approved"
      || resumed.data?.reviews?.length !== 1 || resumed.data?.action_tasks?.length !== 1
      || openTask?.status !== "open" || openTask?.assignee_email !== author.email) {
      throw new Error("The requested F02 resume target is not exactly one approved, assigned open task.");
    }
    meeting = { id: resumeMeetingId };
  } else {
    const sources = await api("GET", "/api/meetings/sources");
    if (sources.status !== 200 || !sources.data?.sources?.some((row) => row.id === manifest.source_document_id)) {
      throw new Error("The department-authorized synthetic transcript is not listed to its employee.");
    }
    const selected = await evaluate(`(()=>{const f=[...document.querySelectorAll('section.card form')].find(x=>x.querySelector('input[placeholder="Quarterly operations review"]'));const s=f?.querySelector('select');if(!s)return false;const o=[...s.options].find(x=>x.value===${JSON.stringify(manifest.source_document_id)});if(!o)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(s,o.value);s.dispatchEvent(new Event('input',{bubbles:true}));s.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
    if (!selected) throw new Error("The scoped transcript source was not selectable in the employee UI.");
    await setValue("document.querySelector('input[placeholder=\"Quarterly operations review\"]')", meetingTitle);
    await clickButton("Create minutes draft");
    await waitFor(`document.body.innerText.includes(${JSON.stringify(meetingTitle)})&&document.body.innerText.includes('Transcript evidence')`, "new meeting transcript details");
    await field("Minutes", "The team approved the vendor access review by November 6. The owner will update the audit register and report unresolved findings.");
    await clickButton("Add decision");
    await field("Decision 1", "Complete the vendor access review by November 6.");
    await field("Transcript citation", String(manifest.transcript_chunk_id));
    await clickButton("Add task");
    await field("Task title", "Update the vendor access audit register");
    const assigneeId = await evaluate(`(()=>{const l=[...document.querySelectorAll('label.field')].find(x=>x.querySelector('.field-label')?.textContent.trim()==='Assignee');return [...(l?.querySelector('select')?.options??[])].find(o=>o.textContent.includes(${JSON.stringify(author.email)}))?.value??null})()`);
    if (!assigneeId) throw new Error("The synthetic meeting author was not an eligible task assignee.");
    await field("Assignee", assigneeId);
    await field("Task details", "Record the review outcome and list unresolved access findings.");
    await field("Transcript citation", String(manifest.transcript_chunk_id), 1);
    await field("Due date", "2026-11-06");
    await clickButton("Save and submit for review");
    await waitFor("document.body.innerText.toLowerCase().includes('submitted')", "submitted minutes status");
    const meetingList = await api("GET", "/api/meetings");
    meeting = meetingList.data?.meetings?.find((item) => item.title === meetingTitle);
    if (meetingList.status !== 200 || !meeting?.id) throw new Error("The submitted meeting was not persisted.");
    const selfReview = await api("POST", `/api/meetings/${meeting.id}/review`, { decision: "approved", rationale: "Author self-approval must be denied." });
    if (selfReview.status !== 403) throw new Error("The meeting author was not denied self-approval.");

    await clickAria("Sign out");
    await signIn(manager, "manager");
    await openMeetings();
    await openMeeting(meetingTitle);
    await field("Review rationale", "Verified the transcript citation and assigned follow-up.");
    await clickButton("Approve minutes and tasks");
    await waitFor("document.body.innerText.toLowerCase().includes('approved')", "Manager-approved meeting");
    const approved = await api("GET", `/api/meetings/${meeting.id}`);
    const task = approved.data?.action_tasks?.[0];
    if (approved.status !== 200 || approved.data?.status !== "approved" || approved.data?.reviews?.length !== 1
      || approved.data?.action_tasks?.length !== 1 || task?.status !== "open" || task?.assignee_email !== author.email) {
      throw new Error("Manager approval did not persist exactly one cited action task.");
    }

    await clickAria("Sign out");
    await signIn(author, "employee");
    await openMeetings();
  }
  await openMeeting(meetingTitle);
  await waitFor("[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='Mark complete')", "assigned task completion action");
  await clickButton("Mark complete");
  await waitFor("document.body.innerText.toLowerCase().includes('completed')", "completed action task");
  const completed = await api("GET", `/api/meetings/${meeting.id}`);
  if (completed.status !== 200 || completed.data?.action_tasks?.[0]?.status !== "completed"
    || completed.data?.action_tasks?.[0]?.evidence_chunk_ids?.[0] !== manifest.transcript_chunk_id) {
    throw new Error("Employee task completion or transcript citation did not persist.");
  }

  await command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await delay(350);
  const viewport = await evaluate("({width:document.documentElement.clientWidth,body:document.body.scrollWidth})");
  const screenshot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"));
  const hashes = {};
  for (const file of ["app/services/meetings.py", "app/api/routes/meetings.py", "frontend/src/components/MeetingsPage.tsx"]) {
    hashes[file] = createHash("sha256").update(readFileSync(file)).digest("hex");
  }
  const head = await new Promise((resolve, reject) => {
    const child = spawn("git", ["rev-parse", "HEAD"], { stdio: ["ignore", "pipe", "ignore"], windowsHide: true });
    let output = "";
    child.stdout.on("data", (chunk) => { output += chunk; });
    child.once("error", reject);
    child.once("close", (code) => code === 0 ? resolve(output.trim()) : reject(new Error("Unable to read current source revision.")));
  });
  const evidence = {
    result: "PASS",
    flow: ["Employee creates cited minutes and task", "employee self-approval receives 403", "scoped Manager approves exactly once", "assigned Employee completes cited task"],
    source: { database: manifest.database, migration: manifest.migration, source_id: manifest.source_document_id, filename: manifest.source_filename, transcript_chunk_id: manifest.transcript_chunk_id, fixture_note: manifest.source_note },
    meeting_id: meeting.id,
    review_count: completed.data.reviews.length,
    task_status: completed.data.action_tasks[0].status,
    task_citation_ids: completed.data.action_tasks[0].evidence_chunk_ids,
    app_origin: appOrigin.origin,
    source_revision: head,
    source_sha256: hashes,
    mobile_viewport: viewport,
    screenshot: path.basename(screenshotPath),
    synthetic_credentials_recorded: false,
  };
  writeFileSync(evidencePath, `${JSON.stringify(evidence, null, 2)}\n`, "utf8");
  console.log("F02_CURRENT_SOURCE_REVIEW_WORKFLOW=PASS; employee cited draft; self-review 403; scoped Manager approval; one task; assignee completion; 390px browser");
} finally {
  try { socket?.close(); } catch {}
  try { browser?.kill(); } catch {}
}
