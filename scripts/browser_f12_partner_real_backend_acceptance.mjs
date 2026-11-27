#!/usr/bin/env node
/** Authenticated browser journey through the real partner API and isolated PostgreSQL. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const url = new URL(process.env.INFRASPINE_F12_URL ?? "http://127.0.0.1:5178/admin/");
const adminEmail = process.env.INFRASPINE_F12_ADMIN_USER;
const adminPassword = process.env.INFRASPINE_F12_ADMIN_PASSWORD;
const partnerPassword = process.env.INFRASPINE_F12_PARTNER_PASSWORD;
const profileRoot = process.env.INFRASPINE_F12_PROFILE_ROOT;
const evidenceRoot = process.env.INFRASPINE_F12_EVIDENCE;
if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(url.hostname)) {
  throw new Error("F12 acceptance is restricted to loopback HTTP.");
}
if (!adminEmail || !adminPassword || !partnerPassword || !profileRoot || !evidenceRoot) {
  throw new Error("F12 acceptance configuration is incomplete.");
}

const chrome = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((candidate) => candidate && existsSync(candidate));
if (!chrome) throw new Error("Chrome or Edge is unavailable for local browser acceptance.");

mkdirSync(profileRoot, { recursive: true });
mkdirSync(evidenceRoot, { recursive: true });
const stamp = Date.now().toString(36);
const profile = path.join(profileRoot, `f12-partner-${stamp}`);
const resolvedProfileRoot = path.resolve(profileRoot) + path.sep;
if (!path.resolve(profile).startsWith(resolvedProfileRoot)) throw new Error("Unsafe browser profile path.");
const browser = spawn(chrome, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank",
], { stdio: "ignore", windowsHide: true });
let socket;
const pending = new Map();
let sequence = 0;

async function waitFor(predicate, label, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await delay(180);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}

function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out during ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression, timeoutMs = 20_000) {
  const response = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (response.exceptionDetails) throw new Error("Browser evaluation failed.");
  return response.result?.value;
}

async function fill(selector, value) {
  return evaluate(`(()=>{const element=document.querySelector(${JSON.stringify(selector)});if(!element)return false;const proto=element instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(element,${JSON.stringify(value)});element.dispatchEvent(new Event('input',{bubbles:true}));element.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
}

async function apiRequest(method, route, body) {
  return evaluate(`(async()=>{const init={method:${JSON.stringify(method)},credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'XMLHttpRequest'}};${body === undefined ? "" : `init.headers['Content-Type']='application/json';init.body=JSON.stringify(${JSON.stringify(body)});`}const response=await fetch(${JSON.stringify(route)},init);let json=null;try{json=await response.json()}catch{}return {status:response.status,body:json}})()`);
}

try {
  const activePortPath = path.join(profile, "DevToolsActivePort");
  await waitFor(() => existsSync(activePortPath), "Chrome DevTools endpoint");
  const [debugPort] = readFileSync(activePortPath, "utf8").trim().split(/\r?\n/);
  let target;
  const targetDeadline = Date.now() + 15_000;
  while (!target && Date.now() < targetDeadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
      if (response.ok) target = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!target) await delay(100);
  }
  if (!target) throw new Error("Chrome page target did not become ready.");
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("Browser debugger connection failed.")), { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer);
    pending.delete(message.id);
    if (message.error) item.reject(new Error("Browser protocol call failed."));
    else item.resolve(message.result);
  });

  await call("Page.enable");
  await call("Runtime.enable");
  await call("Page.navigate", { url: url.href });
  await waitFor(() => evaluate("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))"), "Admin login form");
  await fill('form.login-form input[name="email"]', adminEmail);
  await fill('form.login-form input[name="password"]', adminPassword);
  await evaluate("document.querySelector('form.login-form')?.requestSubmit()");
  await waitFor(() => evaluate("(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;return (await r.json()).role==='admin'})()"), "Admin authentication", 40_000);

  const suffix = Date.now().toString(36);
  const workspaceA = await apiRequest("POST", "/api/admin/partner-workspaces", { slug: `f12-acme-${suffix}`, name: `F12 Acme ${suffix}` });
  const workspaceB = await apiRequest("POST", "/api/admin/partner-workspaces", { slug: `f12-beta-${suffix}`, name: `F12 Beta ${suffix}` });
  if (workspaceA.status !== 201 || workspaceB.status !== 201) throw new Error("Real Admin API workspace creation failed.");
  const partnerEmail = `f12-partner-${suffix}@example.test`;
  const invite = await apiRequest("POST", `/api/admin/partner-workspaces/${workspaceA.body.id}/invitations`, { email: partnerEmail });
  if (invite.status !== 201 || typeof invite.body.token !== "string") throw new Error("Real Admin API invitation creation failed.");
  const sourceA = await apiRequest("POST", `/api/admin/partner-workspaces/${workspaceA.body.id}/sources`, {
    title: "Acme Partner Support Hours",
    content: "Acme partner support hours are 08:00 to 17:00 UTC. Contact the Acme customer success team for help.",
  });
  const sourceB = await apiRequest("POST", `/api/admin/partner-workspaces/${workspaceB.body.id}/sources`, {
    title: "Beta Private Reference",
    content: "The Beta partner reference code is BETA-9137. This reference belongs only to the Beta workspace.",
  });
  if (sourceA.status !== 201 || sourceB.status !== 201 || "content" in sourceA.body) throw new Error("Real Admin API source creation failed or returned source content.");
  const invitationList = await apiRequest("GET", `/api/admin/partner-workspaces/${workspaceA.body.id}/invitations`);
  if (invitationList.status !== 200 || invitationList.body[0]?.token !== null) throw new Error("Invitation token was retrievable after creation.");
  console.log("F12_REAL_BACKEND_ADMIN_WORKSPACE_INVITATION_SOURCE=PASS");

  await apiRequest("POST", "/api/auth/logout");
  const internalAuth = await apiRequest("GET", "/api/auth/me");
  const internalCorpus = await apiRequest("GET", "/api/knowledge");
  if (internalAuth.status !== 401 || internalCorpus.status !== 401) throw new Error("Internal session remained available after Admin sign-out.");
  await evaluate("window.__f12PartnerCalls=[];window.__f12OriginalFetch=window.fetch.bind(window);window.fetch=(input,init={})=>{const requestUrl=new URL(typeof input==='string'?input:input.url,location.href);if(requestUrl.pathname.startsWith('/api/'))window.__f12PartnerCalls.push(requestUrl.pathname);return window.__f12OriginalFetch(input,init)}");

  const partnerUrl = new URL(url.href);
  partnerUrl.hash = `/partner?token=${encodeURIComponent(invite.body.token)}`;
  await call("Page.navigate", { url: partnerUrl.href });
  await waitFor(() => evaluate("document.querySelector('h1')?.textContent.trim()==='Set up partner access'"), "partner invitation form");
  await fill('form.partner-auth-form input[name="password"]', partnerPassword);
  await fill('form.partner-auth-form input[name="confirm_password"]', partnerPassword);
  await evaluate("document.querySelector('form.partner-auth-form')?.requestSubmit()");
  await waitFor(() => evaluate("document.querySelector('h1')?.textContent.trim()==='Ask your workspace'"), "partner sign-in after invitation acceptance", 45_000);
  const identity = await apiRequest("GET", "/api/partner/auth/me");
  if (identity.status !== 200 || identity.body.workspace_slug !== workspaceA.body.slug || identity.body.email !== partnerEmail) {
    throw new Error("Partner identity did not resolve to the invited workspace.");
  }
  const internalAfterPartner = await evaluate("(async()=>{const r=await window.__f12OriginalFetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});return r.status})()");
  if (internalAfterPartner !== 401) throw new Error("Partner cookie crossed into the internal auth boundary.");

  const ask = async (question, expectedCount) => {
    await fill("#partner-question", question);
    await evaluate("document.querySelector('form.partner-composer')?.requestSubmit()");
    await waitFor(() => evaluate(`document.querySelectorAll('article.partner-exchange').length===${expectedCount}`), `partner response ${expectedCount}`, 45_000);
  };
  await ask("What are the Acme partner support hours?", 1);
  const grounded = await evaluate("document.querySelector('article.partner-exchange')?.innerText??''");
  if (!grounded.includes("08:00 to 17:00 UTC") || !grounded.includes("Acme Partner Support Hours")
    || !documentHasCitedPartnerAnswer(grounded)) throw new Error("Partner answer was not grounded and cited in the browser.");

  await ask("What is BETA-9137?", 2);
  const tenantRefusal = await evaluate("(()=>{const items=[...document.querySelectorAll('article.partner-exchange')];const latest=items.at(-1);const answer=latest?.querySelector('.partner-answer-text')?.innerText??'';return {answer,insufficient:Boolean(latest?.querySelector('.partner-evidence-state')),citations:latest?.querySelectorAll('.partner-citation').length??0}})()");
  if (!tenantRefusal.insufficient || tenantRefusal.citations !== 0 || tenantRefusal.answer.includes("BETA-9137")) {
    throw new Error(`Cross-workspace refusal failed (insufficient=${tenantRefusal.insufficient}; citations=${tenantRefusal.citations}; beta_code_disclosed=${tenantRefusal.answer.includes("BETA-9137")}).`);
  }
  const boundaryCalls = await evaluate("window.__f12PartnerCalls??[]");
  if (!boundaryCalls.length || boundaryCalls.some((route) => !route.startsWith("/api/partner/"))) {
    throw new Error("Partner UI called an internal API route during the partner session.");
  }
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  const mobile = await evaluate("({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})");
  if (mobile.width !== 390 || mobile.scrollWidth !== 390) throw new Error("Partner portal overflowed at 390px.");
  const screenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const screenshotPath = path.join(evidenceRoot, `f12-real-partner-${suffix}.png`);
  writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"));
  console.log("F12_REAL_BACKEND_PARTNER_INVITATION_LOGIN=PASS");
  console.log("F12_REAL_BACKEND_CITED_ANSWER=PASS");
  console.log("F12_REAL_BACKEND_TENANT_ISOLATION_REFUSAL=PASS");
  console.log("F12_REAL_BACKEND_PARTNER_API_BOUNDARY=PASS");
  console.log("F12_REAL_BACKEND_MOBILE_390PX=PASS");
  console.log(`F12_REAL_BACKEND_BROWSER_ACCEPTANCE=PASS (${path.basename(screenshotPath)})`);
} catch (error) {
  console.error(`F12_REAL_BACKEND_BROWSER_ACCEPTANCE=FAIL ${error?.message ?? "unknown error"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (browser && browser.exitCode === null) {
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, 5000);
      browser.once("exit", () => { clearTimeout(timer); resolve(); });
      browser.kill();
    });
  }
  const resolvedProfile = path.resolve(profile);
  if (resolvedProfile.startsWith(resolvedProfileRoot)) {
    try { rmSync(resolvedProfile, { recursive: true, force: true, maxRetries: 4, retryDelay: 500 }); }
    catch { console.log("F12_BROWSER_PROFILE_CLEANUP_PENDING=true"); }
  }
}

function documentHasCitedPartnerAnswer(text) {
  return text.includes("Sources") && text.includes("[S1]");
}
