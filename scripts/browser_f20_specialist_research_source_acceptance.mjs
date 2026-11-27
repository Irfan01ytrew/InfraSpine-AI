#!/usr/bin/env node
/** Current-source F20 research UI acceptance using the existing indexed synthetic F18 PDF. */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const fixturePath = process.env.INFRASPINE_F20_RESEARCH_FIXTURE;
const evidencePath = process.env.INFRASPINE_F20_RESEARCH_EVIDENCE;
const base = process.env.INFRASPINE_F20_RESEARCH_URL ?? "http://127.0.0.1:5181/admin/";
const sourceFilename = "f18-refund-policy-source.pdf";
const question = "What does the refund policy say about the return period?";
const url = new URL(base);
if (!fixturePath || !evidencePath || !existsSync(fixturePath)) throw new Error("Protected synthetic F20 fixture is unavailable.");
if (url.hostname !== "127.0.0.1" || !/^https?:$/.test(url.protocol)) throw new Error("F20 browser acceptance is limited to loopback HTTP.");
const { admin } = JSON.parse(readFileSync(fixturePath, "utf8"));
const chromePath = process.env.INFRASPINE_BROWSER_PATH ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
if (!admin?.email || !admin?.password || !existsSync(chromePath)) throw new Error("Protected synthetic Admin fixture or local Chrome is unavailable.");

let chrome;
let socket;
let profile;
let sequence = 0;
let researchResponse = null;
let responseId = null;
const pending = new Map();
const cdp = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++sequence;
  const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Chrome command timed out: ${method}`)); }, 30_000);
  pending.set(id, { resolve, reject, timer });
  socket.send(JSON.stringify({ id, method, params }));
});
async function evaluate(expression) {
  const result = await cdp("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error("Specialist research browser action failed.");
  return result.result?.value;
}
async function waitFor(expression, label, timeout = 35_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(120);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function fill(selector, value) {
  const ok = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!ok) throw new Error("Research page form control is missing.");
}

try {
  profile = mkdtempSync(path.join(tmpdir(), "infraspine-f20-research-source-"));
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
    const message = JSON.parse(event.data);
    if (message.method === "Network.responseReceived" && message.params.response.url.includes("/api/rag/research")) {
      responseId = message.params.requestId;
      researchResponse = { status: message.params.response.status };
    }
    if (message.method === "Network.loadingFinished" && message.params.requestId === responseId) {
      void cdp("Network.getResponseBody", { requestId: responseId }).then((body) => {
        try { researchResponse.body = JSON.parse(body.body); } catch { researchResponse.body = null; }
      });
    }
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error("Chrome DevTools command failed.")) : item.resolve(message.result);
  });
  await cdp("Page.enable"); await cdp("Runtime.enable"); await cdp("Network.enable");
  await cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await cdp("Page.navigate", { url: base });
  await waitFor(`Boolean(document.querySelector('form.login-form input[name="email"]'))`, "Admin sign-in");
  await fill('form.login-form input[name="email"]', admin.email);
  await fill('form.login-form input[name="password"]', admin.password);
  await evaluate(`document.querySelector('form.login-form')?.requestSubmit()`);
  await waitFor(`Boolean(document.querySelector('.shell'))`, "authenticated F20 workspace");
  const sourceReady = await evaluate(`(async()=>{const response=await fetch('/api/rag/documents',{credentials:'same-origin',cache:'no-store'});if(!response.ok)return false;const body=await response.json();const doc=body.documents.find(item=>item.filename===${JSON.stringify(sourceFilename)});window.__f20SourceReady=doc??null;return Boolean(doc&&doc.status==='ready'&&doc.searchable&&doc.chunk_count>0)})()`);
  if (!sourceReady) throw new Error("Existing isolated F20 synthetic source is not ready/searchable; no upload or worker will be started.");
  const researchNav = await evaluate(`(()=>{const button=[...document.querySelectorAll('button')].find(item=>item.innerText.trim()==='Company data research');if(!button)return false;button.click();return true})()`);
  if (!researchNav) throw new Error("Company data research navigation is unavailable.");
  await waitFor(`document.querySelector('h1')?.innerText==='Company data research'`, "specialist research page");
  await fill(".research-form textarea", question);
  const submitted = await evaluate(`(()=>{const button=[...document.querySelectorAll('.research-form button')].find(item=>item.innerText.includes('Run research'));if(!button||button.disabled)return false;button.click();return true})()`);
  if (!submitted) throw new Error("Research request could not be submitted.");
  await waitFor(`Boolean(document.querySelector('.research-results'))`, "document-grounded specialist report", 60_000);
  const responseDeadline = Date.now() + 10_000;
  while (!researchResponse?.body?.findings?.length && Date.now() < responseDeadline) await delay(50);
  if (!researchResponse?.body?.findings?.length) throw new Error("Current-source research API response body was not captured.");
  const finding = researchResponse.body.findings[0];
  const citation = finding.citations.find((item) => item.filename === sourceFilename);
  const ui = await evaluate(`(()=>{const card=document.querySelector('.research-finding');return{specialist:[...(card?.querySelectorAll('.chip')??[])].map(item=>item.innerText.trim())[0]??null,evidenceState:[...(card?.querySelectorAll('.pill')??[])].map(item=>item.innerText.trim())[0]??null,answer:document.querySelector('.research-answer')?.innerText??'',sources:[...document.querySelectorAll('.research-source strong')].map(item=>item.innerText.trim()),locations:[...document.querySelectorAll('.research-source small')].map(item=>item.innerText.trim()),sourceLinks:[...document.querySelectorAll('.research-source a')].map(item=>item.innerText.trim()),viewport:visualViewport?.width,bodyWidth:document.body.scrollWidth,documentWidth:document.documentElement.scrollWidth}})()`);
  const visibleLocation = citation?.location ?? (citation ? `Page ${citation.page_number}` : "");
  if (researchResponse.status !== 200 || finding.specialist !== "evidence_finder" || finding.insufficient_evidence || !citation || !citation.available || citation.page_number < 1 || ui.specialist !== "Evidence finder" || ui.evidenceState !== "Supported" || !ui.sources.includes(sourceFilename) || !ui.locations.includes(visibleLocation) || !ui.answer.includes(citation.source_id)) {
    throw new Error(`F20 cited research assertion mismatch: ${JSON.stringify({status:researchResponse.status,specialist:finding.specialist,insufficient:finding.insufficient_evidence,citations:finding.citations.map((item)=>({filename:item.filename,available:item.available,page:item.page_number,location:item.location})),uiSpecialist:ui.specialist,uiEvidence:ui.evidenceState,uiSourceCount:ui.sources.length,uiLocationCount:ui.locations.length})}`);
  }
  if (ui.viewport !== 390 || ui.bodyWidth > 390) throw new Error("Specialist research page did not fit the 390px mobile visual viewport.");
  const screenshot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  writeFileSync(evidencePath, Buffer.from(screenshot.data, "base64"), { flag: "wx" });
  console.log(`F20_SPECIALIST_RESEARCH_CURRENT_SOURCE_API_BROWSER=PASS HTTP ${researchResponse.status}; ${finding.specialist}; ${citation.filename}; ${visibleLocation}; cited source tag and page evidence visible; 390px body ${ui.bodyWidth}px; 0 additional documents/workers`);
  console.log(`F20_RESEARCH_EVIDENCE=${evidencePath}`);
} catch (error) {
  console.error(`F20_SPECIALIST_RESEARCH_SOURCE_UI=FAIL ${error?.message ?? "unknown"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (chrome && chrome.exitCode === null) { chrome.kill(); await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3000)]); }
  if (profile && path.basename(profile).startsWith("infraspine-f20-research-source-")) rmSync(profile, { recursive: true, force: true });
  admin.password = "";
}
