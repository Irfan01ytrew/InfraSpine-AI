#!/usr/bin/env node
/** Current-source F02 browser acceptance for transcript-grounded minutes generation. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_F02_REVIEW_URL ?? "http://127.0.0.1:5178/admin/");
const credentialPath = process.env.INFRASPINE_F02_CREDENTIAL_FIXTURE;
const reviewRoot = process.env.INFRASPINE_F02_REVIEW_ROOT;
if (base.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(base.hostname) || !credentialPath || !reviewRoot) {
  throw new Error("F02 browser acceptance requires its loopback review and protected synthetic fixture.");
}
const fixture = JSON.parse(readFileSync(credentialPath, "utf8").replace(/^\uFEFF/, ""));
const admin = fixture.admin;
if (!admin?.email || !admin?.password) throw new Error("The protected synthetic Admin fixture is incomplete.");
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for F02 browser acceptance.");

const transcript = "The team will review vendor access on November 6. Jordan Lee will own the review and document unresolved findings in the audit register.";
const title = `F02 AI minutes browser ${Date.now()}`;
const evidenceDir = path.join(reviewRoot, "evidence");
mkdirSync(evidenceDir, { recursive: true });
const screenshotPath = path.join(evidenceDir, "f02-ai-minutes-390.png");
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-f02-minutes-"));
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
  const result = await command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("A current-source F02 browser action failed.");
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
async function setFirstRunField(labelText, value) {
  const script = `(()=>{const f=document.querySelector('form.deployment-form');const l=[...(f?.querySelectorAll('label')??[])].find(x=>[...x.childNodes].some(n=>n.nodeType===Node.TEXT_NODE&&n.textContent.trim()===${JSON.stringify(labelText)}));const e=l?.querySelector('input,select');if(!e)return false;const p=e instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`;
  if (!await evaluate(script)) throw new Error(`First-run field ${labelText} was unavailable.`);
}
async function setMeetingField(labelText, value) {
  const script = `(()=>{const l=[...document.querySelectorAll('label.field')].find(x=>x.querySelector('.field-label')?.textContent.trim()===${JSON.stringify(labelText)});const e=l?.querySelector('input,select');if(!e)return false;const p=e instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`;
  if (!await evaluate(script)) throw new Error(`Meeting form field ${labelText} was unavailable.`);
}

try {
  chrome = spawn(browserPath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
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
  await command("Page.enable");
  await command("Runtime.enable");
  await command("Page.navigate", { url: base.href });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "synthetic Admin login form");
  const login = await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const pass=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!pass||!form)return false;for(const [el,value] of [[email,${JSON.stringify(admin.email)}],[pass,${JSON.stringify(admin.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
  if (!login) throw new Error("Synthetic Admin login could not be submitted.");
  await waitFor("Boolean(document.querySelector('.shell')||document.querySelector('.first-run-setup'))", "authenticated Admin", 40_000);
  if (await evaluate("Boolean(document.querySelector('.first-run-setup'))")) {
    await setFirstRunField("Company name", "F02 Synthetic Meeting QA");
    await setFirstRunField("Industry", "General");
    await setFirstRunField("Product brand", "InfraSpine F02 QA");
    const saved = await evaluate(`(()=>{const f=document.querySelector('.first-run-setup form.deployment-form');const b=[...(f?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Save company profile');if(!b||b.disabled)return false;b.click();return true})()`);
    if (!saved) throw new Error("Synthetic first-run company setup could not be saved.");
    await waitFor("Boolean(document.querySelector('.shell'))", "post-setup Admin shell", 45_000);
  }
  const nav = await evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Meeting minutes');if(!b)return false;b.click();return true})()`);
  if (!nav) throw new Error("The Meeting minutes navigation item was unavailable.");
  await waitFor("document.querySelector('h1')?.textContent.trim()==='Meeting minutes'", "Meeting minutes page");
  await waitFor("[...document.querySelector('section.card form')?.querySelector('select')?.options??[]].some(o=>o.textContent.includes('f02-synthetic-quarterly-review.mp3'))", "processed synthetic transcript source");
  await setMeetingField("Meeting title", title);
  const create = await evaluate(`(()=>{const f=[...document.querySelectorAll('section.card form')].find(x=>x.querySelector('input[placeholder="Quarterly operations review"]'));const b=[...(f?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()==='Create minutes draft');if(!f||!b||b.disabled)return false;b.click();return true})()`);
  if (!create) throw new Error("The meeting draft form could not be submitted.");
  await waitFor(`document.body.innerText.includes(${JSON.stringify(title)})&&document.body.innerText.includes(${JSON.stringify(transcript.slice(0, 52))})`, "meeting draft and source transcript");
  const meeting = await evaluate(`(async()=>{const r=await fetch('/api/meetings');if(!r.ok)return null;return (await r.json()).meetings.find(x=>x.title===${JSON.stringify(title)})??null})()`);
  if (!meeting?.id || !meeting.can_edit || !meeting.source_is_current) throw new Error("The source-backed meeting draft did not persist in the API.");
  const clicked = await evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Generate from transcript');if(!b||b.disabled)return false;b.click();return true})()`);
  if (!clicked) throw new Error("The current-source Generate from transcript control was unavailable.");
  await waitFor(`(async()=>{const r=await fetch(${JSON.stringify(`/api/meetings/${meeting.id}`)});if(!r.ok)return false;const d=await r.json();return d.current_revision===2&&d.revision.minutes_markdown.includes('Transcript #')})()`, "persisted generated minutes with a transcript citation", 40_000);
  const detail = await evaluate(`(async()=>{const r=await fetch(${JSON.stringify(`/api/meetings/${meeting.id}`)});return r.ok?await r.json():null})()`);
  const transcriptChunkId = await evaluate(`(async()=>{const r=await fetch(${JSON.stringify(`/api/meetings/${meeting.id}/transcript`)});if(!r.ok)return null;return (await r.json()).chunks[0]?.id??null})()`);
  const expectedFirst = transcript.split(". ")[0] + ".";
  const expectedSecond = transcript.split(". ")[1].trim();
  const validEvidence = detail?.revision?.decisions?.length === 1 &&
    detail.revision.decisions[0].text === expectedSecond &&
    JSON.stringify(detail.revision.decisions[0].evidence_chunk_ids) === JSON.stringify([transcriptChunkId]) &&
    detail.revision.minutes_markdown.includes(expectedFirst) &&
    detail.revision.minutes_markdown.includes(expectedSecond) &&
    detail.revision.minutes_markdown.includes(`Transcript #${transcriptChunkId}`);
  if (!validEvidence || detail.current_revision !== 2 || detail.status !== "draft" ||
      detail.revision.proposed_tasks.length !== 0 || detail.action_tasks.length !== 0 || detail.reviews.length !== 0) {
    throw new Error("Generated minutes were not an editable, source-cited draft without review/task side effects.");
  }
  const mobile = await command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await delay(350);
  const dimensions = await evaluate("({viewport:document.documentElement.clientWidth,body:document.body.clientWidth,root:document.documentElement.scrollWidth})");
  if (!dimensions || dimensions.viewport !== 390 || dimensions.body > 390) throw new Error("The F02 390px mobile view exceeded the viewport body width.");
  const shot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(screenshotPath, Buffer.from(shot.data, "base64"));
  const evidence = {
    result: "PASS",
    scope: "current-source browser/API against isolated synthetic PostgreSQL; deterministic transcript-copying LLM test double; no live provider call",
    reviewUrl: base.href,
    database: "infraspine_f02meetai_20261028_test",
    migration: "0059_workspace_hierarchy",
    meetingTitle: title,
    revision: detail.current_revision,
    transcriptCitation: transcriptChunkId,
    sourceSentencesCopiedExactly: true,
    reviewOrTaskSideEffectsBeforeSubmission: 0,
    mobile390: dimensions,
    screenshot: path.relative(process.cwd(), screenshotPath).replaceAll("\\", "/"),
  };
  writeFileSync(path.join(evidenceDir, "f02-ai-minutes-current-source.json"), JSON.stringify(evidence, null, 2) + "\n", { mode: 0o600 });
  console.log("F02_CURRENT_SOURCE_AI_DRAFT_BROWSER=PASS; two exact transcript sentences, citation persisted, revision 2 remains editable, zero task/review side effects, viewport 390px");
} finally {
  socket?.close();
  if (chrome && chrome.exitCode === null) {
    chrome.kill();
    await new Promise((resolve) => chrome.once("exit", resolve));
  }
  try { await import("node:fs/promises").then(({ rm }) => rm(profile, { recursive: true, force: true })); } catch { /* only the harness's own temporary browser profile */ }
}
