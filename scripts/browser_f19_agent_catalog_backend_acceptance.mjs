#!/usr/bin/env node
/** Current-source authenticated browser-to-PostgreSQL F19 acceptance; requires an isolated local API. */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { createServer } from "node:net";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const frontend = path.join(repo, "frontend");
const viteEntry = path.join(frontend, "node_modules", "vite", "bin", "vite.js");
const apiTarget = process.env.INFRASPINE_F19_API_PROXY_TARGET ?? "http://127.0.0.1:63001";
const publisherEmail = process.env.INFRASPINE_F19_PUBLISHER_EMAIL ?? "catalog-publisher@example.test";
const reviewerEmail = process.env.INFRASPINE_F19_REVIEWER_EMAIL ?? "catalog-reviewer@example.test";
const password = process.env.INFRASPINE_F19_TEST_PASSWORD;
const browserCandidates = process.platform === "win32"
  ? ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"), path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe")]
  : ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/microsoft-edge"];
const browserPath = process.env.INFRASPINE_BROWSER_PATH || browserCandidates.find((candidate) => candidate && existsSync(candidate));
const target = new URL(apiTarget);
if (!existsSync(viteEntry)) throw new Error("Frontend dependencies are not installed; Vite was not found.");
if (!browserPath || !existsSync(browserPath)) throw new Error("Chrome, Chromium or Edge was not found.");
if (!password) throw new Error("Set INFRASPINE_F19_TEST_PASSWORD for the disposable acceptance accounts.");
if (!(["127.0.0.1", "localhost", "::1"].includes(target.hostname))) throw new Error("The acceptance API proxy must target loopback only.");

let browser; let vite; let socket; let profile; let port; let sequence = 0;
const pending = new Map();

async function freePort() {
  const server = createServer();
  await new Promise((resolve, reject) => server.once("error", reject).listen(0, "127.0.0.1", resolve));
  const value = server.address().port;
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  return value;
}

function call(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser command timed out: ${method}`)); }, 20_000);
    pending.set(id, { resolve, reject, timer, method }); socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error("Browser action raised an exception.");
  return result.result?.value;
}

async function waitFor(expression, timeout = 20_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { if (await evaluate(expression)) return true; await delay(150); }
  return false;
}

async function fill(selector, value) {
  const result = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value').set;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
  if (!result) throw new Error(`Form control was missing: ${selector}`);
}

async function signIn(email) {
  if (!await waitFor(`!!document.querySelector('.login-form input[name="email"]')`)) throw new Error("Sign-in form was not shown.");
  await fill('.login-form input[name="email"]', email);
  await fill('.login-form input[name="password"]', password);
  await evaluate(`document.querySelector('.login-form')?.requestSubmit()`);
  if (!await waitFor(`document.querySelector('h1')?.textContent.includes('Agent profiles')`)) {
    const error = await evaluate(`document.querySelector('.inline-alert-error')?.textContent ?? document.querySelector('.first-run-waiting')?.textContent ?? 'signed-in page did not render'`);
    throw new Error(`Sign-in failed for the synthetic acceptance account: ${String(error).slice(0, 160)}`);
  }
  if (!await waitFor(`!!document.querySelector('[aria-label="Reviewed internal agent catalog"] h2')`)) throw new Error("Catalog UI did not render after sign-in.");
}

async function signOut() {
  await evaluate(`document.querySelector('button[aria-label="Sign out"]')?.click()`);
  if (!await waitFor(`!!document.querySelector('.login-form input[name="email"]')`)) throw new Error("Sign-out did not return to the sign-in screen.");
}

async function apiCalls() {
  return await evaluate(`window.__f19RealCalls ?? []`);
}

async function run() {
  port = await freePort();
  vite = spawn(process.execPath, [viteEntry, "--host", "127.0.0.1", "--port", String(port), "--strictPort"], {
    cwd: frontend,
    stdio: "inherit",
    windowsHide: true,
    env: { ...process.env, INFRASPINE_DEV_API_PROXY_TARGET: apiTarget },
  });
  const url = `http://127.0.0.1:${port}/admin/`;
  const ready = Date.now() + 30_000;
  while (Date.now() < ready && vite.exitCode === null) { try { if ((await fetch(url)).ok) break; } catch {} await delay(150); }
  if (vite.exitCode !== null) throw new Error("Vite exited before becoming ready.");

  profile = mkdtempSync(path.join(tmpdir(), "infraspine-f19-real-browser-"));
  const activePort = path.join(profile, "DevToolsActivePort");
  browser = spawn(browserPath, ["--headless=new","--disable-gpu","--no-first-run","--no-default-browser-check","--disable-extensions","--disable-background-networking","--disable-sync","--remote-debugging-port=0",`--user-data-dir=${profile}`,"about:blank"], { stdio:"ignore", windowsHide:true });
  const browserReady = Date.now() + 30_000;
  while (!existsSync(activePort) && Date.now() < browserReady && browser.exitCode === null) await delay(100);
  if (!existsSync(activePort)) throw new Error("Browser debugger did not start.");
  const debugPort=Number(readFileSync(activePort,"utf8").split(/\r?\n/)[0]);
  let page;
  while(!page&&Date.now()<browserReady){try{const response=await fetch(`http://127.0.0.1:${debugPort}/json/list`);if(response.ok)page=(await response.json()).find((item)=>item.type==="page"&&item.webSocketDebuggerUrl);}catch{}if(!page)await delay(150);}
  if(!page)throw new Error("Browser page target did not become ready.");
  socket=new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.addEventListener("open",resolve,{once:true});socket.addEventListener("error",()=>reject(new Error("Browser debugger connection failed.")),{once:true});});
  socket.addEventListener("message",(event)=>{const message=JSON.parse(event.data);if(!message.id||!pending.has(message.id))return;const entry=pending.get(message.id);clearTimeout(entry.timer);pending.delete(message.id);if(message.error)entry.reject(new Error(`Browser command failed for ${entry.method}: ${message.error.message??"unknown error"}`));else entry.resolve(message.result);});
  await call("Page.enable"); await call("Runtime.enable");
  await call("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await call("Page.addScriptToEvaluateOnNewDocument",{source:`(()=>{window.__f19RealCalls=[];const original=window.fetch.bind(window);window.fetch=async(input,init={})=>{const url=new URL(typeof input==="string"?input:input.url,location.href);const method=String(init.method??(typeof input==="string"?"GET":input.method)??"GET").toUpperCase();const body=url.pathname==="/api/auth/login"?undefined:(typeof init.body==="string"?(()=>{try{return JSON.parse(init.body)}catch{return undefined}})():undefined);const response=await original(input,init);if(url.pathname.startsWith("/api/"))window.__f19RealCalls.push({method,path:url.pathname,status:response.status,body});return response;};})();`});
  await call("Page.navigate",{url:`${url}#/agents`});
  await signIn(publisherEmail);
  if (!await waitFor(`document.querySelector('[aria-label="Publisher template draft"] h3')?.textContent.includes('Create a template draft')`)) throw new Error("Publisher form was not available to the publisher account.");

  await fill('[aria-label="Publisher template draft"] label.field:nth-of-type(1) input','f19-live-browser-catalog');
  await fill('[aria-label="Publisher template draft"] label.field:nth-of-type(2) input','F19 live browser template');
  await fill('[aria-label="Publisher template draft"] textarea','Use only requester-readable operations procedures and preserve source citations.');
  await evaluate(`document.querySelector('[aria-label="Publisher template draft"] form')?.requestSubmit()`);
  if(!await waitFor(`document.querySelector('[role="status"]')?.textContent.includes('Draft version 1 saved')`))throw new Error("Real backend did not save the publisher draft.");
  if(!await waitFor(`[...document.querySelectorAll('.approval-card button')].some(button=>button.textContent.includes('Submit for independent review'))`))throw new Error("Real backend draft did not refresh into the publisher list.");
  await evaluate(`[...document.querySelectorAll('.approval-card button')].find(button=>button.textContent.includes('Submit for independent review'))?.click()`);
  if(!await waitFor(`document.querySelector('.approval-card')?.textContent.includes('pending review')`))throw new Error("Real backend did not persist pending review status.");
  console.log("F19_REAL_PUBLISHER_DRAFT_SUBMIT=PASS");

  await signOut();
  await signIn(reviewerEmail);
  if(!await waitFor(`document.querySelector('.approval-card')?.textContent.includes('pending review')`))throw new Error("Independent reviewer did not see the persisted pending version.");
  await fill('.approval-card textarea','This version follows the approved requester ACL and citation boundary.');
  await evaluate(`[...document.querySelectorAll('.approval-card button')].find(button=>button.textContent.includes('Approve version'))?.click()`);
  if(!await waitFor(`[...document.querySelectorAll('[aria-label="Reviewed internal agent catalog"] button')].some(button=>button.textContent.includes('Use as department profile'))`))throw new Error("Real backend approval did not publish the template.");
  console.log("F19_REAL_INDEPENDENT_REVIEW_PUBLICATION=PASS");

  await signOut();
  await signIn(publisherEmail);
  if(!await waitFor(`[...document.querySelectorAll('[aria-label="Reviewed internal agent catalog"] button')].some(button=>button.textContent.includes('Use as department profile'))`))throw new Error("Published template did not survive reload for the department manager.");
  await evaluate(`[...document.querySelectorAll('[aria-label="Reviewed internal agent catalog"] button')].find(button=>button.textContent.includes('Use as department profile'))?.click()`);
  if(!await waitFor(`document.querySelector('#department-agent-profile-form input')?.value==='F19 live browser template'`))throw new Error("Real approved definition did not populate the department profile form.");
  const copied=await evaluate(`(()=>{const form=document.querySelector('#department-agent-profile-form');const fields=[...form.querySelectorAll('select')];return {name:form.querySelector('input')?.value,department:fields[0]?.selectedOptions[0]?.textContent,type:fields[1]?.value,scope:fields[2]?.value};})()`);
  if(copied?.name!=="F19 live browser template"||copied?.department!=="F19 Browser Operations"||copied?.type!=="knowledge_research"||copied?.scope!=="requester_acl")throw new Error(`Unexpected applied definition: ${JSON.stringify(copied)}`);
  await evaluate(`document.querySelector('#department-agent-profile-form')?.requestSubmit()`);
  if(!await waitFor(`document.querySelector('[role="status"]')?.textContent.includes('Agent profile saved')`))throw new Error("Manager could not save the catalog definition as a department profile.");
  if(!await waitFor(`[...document.querySelectorAll('.automation-card')].some(section=>section.querySelector('h2')?.textContent==='Department agent profiles'&&section.querySelector('.automation-item h3')?.textContent==='F19 live browser template')`))throw new Error("Saved department profile did not appear after refresh.");
  const layout=await evaluate(`({viewport:innerWidth,width:document.documentElement.scrollWidth})`);
  if(layout.viewport!==390||layout.width>390)throw new Error(`F19 authenticated catalog UI overflows at 390px: ${JSON.stringify(layout)}`);
  console.log("F19_REAL_TEMPLATE_APPLY_SAVE_AND_390PX=PASS");

  await signOut();
  await signIn(reviewerEmail);
  if(!await waitFor(`!!document.querySelector('.approval-card textarea')`))throw new Error("Reviewer revocation control did not load after sign-in.");
  await fill('.approval-card textarea','Revoke this test template after verifying the review journey.');
  await evaluate(`[...document.querySelectorAll('.approval-card button')].find(button=>button.textContent.includes('Revoke catalog template'))?.click()`);
  if(!await waitFor(`document.querySelector('[aria-label="Reviewed internal agent catalog"] .empty')?.textContent.includes('No reviewed templates are published')`))throw new Error("Real backend revocation did not remove the template from the public catalog.");
  if(!await waitFor(`document.querySelector('.approval-card')?.textContent.includes('Revoked')`))throw new Error("Revocation state/history did not remain visible to the reviewer.");

  const calls=await apiCalls();
  const catalogPosts=calls.filter((item)=>item.path.startsWith('/api/agent-catalog/')&&item.method==='POST');
  const statuses=catalogPosts.map(({method,path,status})=>`${method} ${path.split('/').slice(-1)[0]} ${status}`);
  if(catalogPosts.some((item)=>item.status<200||item.status>=300))throw new Error(`A real catalog mutation failed: ${JSON.stringify(statuses)}`);
  if(!catalogPosts.some((item)=>item.path==='/api/agent-catalog/templates'&&item.status===201)||
     !catalogPosts.some((item)=>item.path.endsWith('/submit')&&item.status===200)||
     !catalogPosts.some((item)=>item.path.endsWith('/review')&&item.status===200)||
     !catalogPosts.some((item)=>item.path.endsWith('/revoke')&&item.status===200))throw new Error(`Expected real catalog API calls were missing: ${JSON.stringify(statuses)}`);
  console.log("F19_REAL_REVOCATION_AND_HISTORY=PASS");
  console.log("F19_AUTHENTICATED_CURRENT_SOURCE_BROWSER_TO_POSTGRESQL_ACCEPTANCE=PASS (390px; disposable local PostgreSQL; synthetic users and content)");
}

run().catch((error)=>{console.error(`F19_REAL_BROWSER=FAIL ${error?.message??"unknown failure"}`);process.exitCode=1;}).finally(async()=>{
  try{socket?.close();}catch{}
  for(const child of [browser,vite]){if(child&&child.exitCode===null){child.kill();await new Promise((resolve)=>{const timer=setTimeout(resolve,2000);child.once("exit",()=>{clearTimeout(timer);resolve();});});}}
  if(profile&&existsSync(profile))rmSync(profile,{recursive:true,force:true});
});
