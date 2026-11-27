#!/usr/bin/env node
/** Current-source partner portal UI journey with a synthetic, in-page partner API. */
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
const browserCandidates = process.platform === "win32"
  ? ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"), path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe")]
  : ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/microsoft-edge"];
const browserPath = browserCandidates.find((candidate) => candidate && existsSync(candidate));
if (!existsSync(viteEntry)) throw new Error("Frontend dependencies are not installed; Vite was not found.");
if (!browserPath) throw new Error("Chrome, Chromium or Edge was not found.");

let browser;
let vite;
let socket;
let profile;
let port;
const pending = new Map();
let sequence = 0;

async function freePort() {
  const server = createServer();
  await new Promise((resolve, reject) => server.once("error", reject).listen(0, "127.0.0.1", resolve));
  const address = server.address();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  return address.port;
}

function call(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser command timed out: ${method}`)); }, 20_000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error("Browser action raised an exception.");
  return result.result?.value;
}

async function waitFor(expression, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await delay(150);
  }
  return false;
}

function mockApiScript() {
  return `(() => {
    const identity = { id: "partner-identity-1", email: "partner@example.test", workspace_id: "workspace-1", workspace_slug: "acme-partner", workspace_name: "Acme Partner" };
    const state = window.__partnerPortal = { calls: [], identity, workspaces: [], invitations: [], identities: [], sources: [] };
    const json = (value, status=200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (input, init={}) => {
      const url = new URL(typeof input === "string" ? input : input.url, location.href);
      if (!url.pathname.startsWith("/api/")) return originalFetch(input, init);
      const method = String(init.method ?? (typeof input === "string" ? "GET" : input.method) ?? "GET").toUpperCase();
      const body = typeof init.body === "string" ? JSON.parse(init.body) : null;
      state.calls.push({ method, path: url.pathname, body: url.pathname.endsWith("/assistant/ask") ? body : null,
        csrf: new Headers(init.headers).get("X-Requested-With") });
      if (method === "GET" && url.pathname === "/api/auth/me") return json({ id: "admin-1", email: "admin@example.test", role: "admin" });
      if (method === "GET" && url.pathname === "/api/deployment/profile") return json({
        company_name: "Synthetic Company", industry: "Testing", brand_name: "Synthetic QA", primary_color: "#1765ad", setup_complete: true,
        template_id: null, template_version: null,
      });
      if (method === "GET" && url.pathname === "/api/admin/providers/catalog") return json({ providers: [] });
      if (method === "GET" && url.pathname === "/api/admin/providers") return json({ providers: [], mode: "mock" });
      if (method === "GET" && url.pathname.startsWith("/api/admin/audit-logs")) return json({ entries: [] });
      if (method === "GET" && url.pathname === "/api/admin/partner-workspaces") return json(state.workspaces);
      if (method === "POST" && url.pathname === "/api/admin/partner-workspaces") {
        const workspace = { id: "workspace-admin-1", slug: body.slug, name: body.name, status: "active", created_at: new Date().toISOString() };
        state.workspaces.push(workspace); return json(workspace, 201);
      }
      if (url.pathname === "/api/admin/partner-workspaces/workspace-admin-1/invitations" && method === "GET") return json(state.invitations);
      if (url.pathname === "/api/admin/partner-workspaces/workspace-admin-1/identities" && method === "GET") return json(state.identities);
      if (url.pathname === "/api/admin/partner-workspaces/workspace-admin-1/sources" && method === "GET") return json(state.sources);
      if (url.pathname === "/api/admin/partner-workspaces/workspace-admin-1/invitations" && method === "POST") {
        const invitation = { id: "invite-admin-1", workspace_id: "workspace-admin-1", email: body.email, created_at: new Date().toISOString(),
          expires_at: new Date(Date.now()+72*60*60*1000).toISOString(), accepted_at: null, revoked_at: null, token: "admin-invite-token-1234567890" };
        state.invitations.unshift(invitation); return json(invitation, 201);
      }
      if (url.pathname === "/api/admin/partner-workspaces/workspace-admin-1/sources" && method === "POST") {
        const source = { id: "partner-source-admin-1", workspace_id: "workspace-admin-1", title: body.title,
          content_sha256: "a".repeat(64), status: "active", created_at: new Date().toISOString(), archived_at: null };
        state.sources.unshift(source); return json(source, 201);
      }
      if (method === "GET" && url.pathname === "/api/partner/auth/me") return json({ error: { code: "authentication_required", message: "Sign in required." } }, 401);
      if (method === "POST" && url.pathname === "/api/partner/auth/accept") return json(identity);
      if (method === "POST" && url.pathname === "/api/partner/auth/login") return json(identity);
      if (method === "POST" && url.pathname === "/api/partner/assistant/ask") return json({
        answer: "The shared service window is 08:00 to 17:00 UTC.", insufficient_evidence: false, retrieved_sources: 1,
        citations: [{ source_id: "S1", source_record_id: "source-1", title: "Partner service guide", excerpt: "Support is available from 08:00 to 17:00 UTC." }],
      });
      if (method === "POST" && url.pathname === "/api/partner/auth/logout") return new Response(null, { status: 204 });
      return json({ error: { code: "unmocked_route", message: "The partner page called an unmocked API route." } }, 501);
    };
  })();`;
}

async function run() {
  port = await freePort();
  vite = spawn(process.execPath, [viteEntry, "--host", "127.0.0.1", "--port", String(port), "--strictPort"], {
    cwd: frontend, stdio: "inherit", windowsHide: true,
  });
  const url = `http://127.0.0.1:${port}/admin/`;
  const readyDeadline = Date.now() + 30_000;
  while (Date.now() < readyDeadline && vite.exitCode === null) {
    try { if ((await fetch(url)).ok) break; } catch {}
    await delay(150);
  }
  if (vite.exitCode !== null) throw new Error("Vite exited before becoming ready.");

  profile = mkdtempSync(path.join(tmpdir(), "infraspine-partner-ui-"));
  const activePortFile = path.join(profile, "DevToolsActivePort");
  browser = spawn(browserPath, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-background-networking", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"],
  { stdio: "ignore", windowsHide: true });
  const browserDeadline = Date.now() + 30_000;
  while (!existsSync(activePortFile) && Date.now() < browserDeadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePortFile)) throw new Error("Browser debugger did not start.");
  const debugPort = Number(readFileSync(activePortFile, "utf8").split(/\r?\n/)[0]);
  let target;
  while (!target && Date.now() < browserDeadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
      if (response.ok) target = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!target) await delay(150);
  }
  if (!target) throw new Error("Browser page target did not become ready.");
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("Browser debugger connection failed.")), { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const entry = pending.get(message.id); clearTimeout(entry.timer); pending.delete(message.id);
    if (message.error) entry.reject(new Error("Browser command failed.")); else entry.resolve(message.result);
  });
  await call("Page.enable");
  await call("Runtime.enable");
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await call("Page.addScriptToEvaluateOnNewDocument", { source: mockApiScript() });
  await call("Page.navigate", { url: `${url}#/partner?token=synthetic-invitation-token-1234567890` });
  if (!await waitFor(`document.querySelector('h1')?.textContent.includes('Set up partner access')`)) throw new Error("Invitation acceptance form did not render.");

  const fill = (selector, value) => `(() => { const el=document.querySelector(${JSON.stringify(selector)}); if(!el)return false; const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value').set; setter.call(el,${JSON.stringify(value)}); el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); return true; })()`;
  if (!await evaluate(fill('input[name="password"]', "Partner!password-789")) || !await evaluate(fill('input[name="confirm_password"]', "Partner!password-789"))) throw new Error("Could not fill invitation password fields.");
  await evaluate(`([...document.querySelectorAll('button')].find((button) => button.textContent.includes('Accept invitation'))?.click(), true)`);
  if (!await waitFor(`document.querySelector('h1')?.textContent.includes('Ask your workspace')`)) throw new Error("Invitation setup did not lead into the partner workspace.");
  if (await evaluate(`location.hash.includes('token=')`)) throw new Error("The invitation token remained in the browser URL after acceptance.");
  console.log("F12_PARTNER_INVITATION_ACCEPTANCE_UI=PASS");

  if (!await evaluate(fill("#partner-question", "What are the support hours?"))) throw new Error("Partner question field was missing.");
  await evaluate(`document.querySelector('.partner-composer button[type="submit"]')?.click()`);
  if (!await waitFor(`document.querySelector('.partner-answer-text')?.textContent.includes('08:00 to 17:00 UTC')`)) throw new Error("The cited assistant answer did not appear.");
  const evidence = await evaluate(`({ answer: document.querySelector('.partner-answer-text')?.textContent ?? '', citation: document.querySelector('.partner-citation')?.innerText ?? '', layout: { viewport: innerWidth, page: document.documentElement.scrollWidth } })`);
  if (!evidence.citation.includes("Partner service guide") || evidence.layout.viewport !== 390 || evidence.layout.page > 390) throw new Error(`Partner answer evidence or mobile layout failed: ${JSON.stringify(evidence)}`);
  console.log("F12_PARTNER_ASSISTANT_CITATION_AND_390PX=PASS");

  await evaluate(`document.querySelector('.partner-account button')?.click()`);
  if (!await waitFor(`document.querySelector('h1')?.textContent.includes('Partner sign in')`)) throw new Error("Partner sign out did not return to sign in.");
  if (!await evaluate(fill('input[name="workspace_slug"]', "acme-partner")) || !await evaluate(fill('input[name="email"]', "partner@example.test")) || !await evaluate(fill('input[name="password"]', "Partner!password-789"))) throw new Error("Partner sign-in fields were missing.");
  await evaluate(`document.querySelector('.partner-auth-form button[type="submit"]')?.click()`);
  if (!await waitFor(`document.querySelector('h1')?.textContent.includes('Ask your workspace')`)) throw new Error("Partner sign in did not open its workspace.");
  const calls = await evaluate(`window.__partnerPortal?.calls ?? []`);
  if (!calls.length || calls.some((entry) => !entry.path.startsWith("/api/partner/"))) throw new Error(`Partner UI called outside its partner API boundary: ${JSON.stringify(calls)}`);
  if (calls.some((entry) => entry.method !== "GET" && entry.csrf !== "InfraSpinePartner")) throw new Error("A state-changing partner API call omitted its partner CSRF marker.");
  if (!calls.some((entry) => entry.path === "/api/partner/auth/accept") || !calls.some((entry) => entry.path === "/api/partner/auth/login") || !calls.some((entry) => entry.path === "/api/partner/auth/logout")) throw new Error("Invitation, sign-in or sign-out was not sent to its dedicated partner endpoint.");
  console.log(`F12_PARTNER_API_CALLS=${calls.length}`);
  console.log("F12_NO_INTERNAL_AUTH_OR_CORPUS_CALLS=PASS");

  await call("Page.navigate", { url: `${url}#/partner-workspaces` });
  if (!await waitFor(`document.querySelector('h1')?.textContent.includes('External partner access')`)) throw new Error("Admin partner workspace page did not render.");
  const adminFill = async (selector, value) => evaluate(fill(selector, value));
  if (!await adminFill(".partner-admin-workspace-form label:nth-of-type(1) input", "Acme External Workspace")
    || !await adminFill(".partner-admin-workspace-form label:nth-of-type(2) input", "acme-external")) throw new Error("Workspace creation fields were missing.");
  await evaluate(`document.querySelector('.partner-admin-workspace-form button[type="submit"]')?.click()`);
  if (!await waitFor(`document.querySelector('.partner-admin-selector select')?.value === 'workspace-admin-1'`)) throw new Error("Admin workspace creation did not select the new workspace.");
  if (!await adminFill(".partner-admin-inline-form input", "buyer@example.test")) throw new Error("Partner invitation email field was missing.");
  await evaluate(`document.querySelector('.partner-admin-inline-form button[type="submit"]')?.click()`);
  if (!await waitFor(`document.querySelector('.partner-invite-link a')?.href.includes('admin-invite-token-1234567890')`)) throw new Error("Admin partner invitation link did not render.");
  if (!await adminFill(".partner-admin-source-form input", "Partner service guide")
    || !await adminFill(".partner-admin-source-form textarea", "Support is available from 08:00 to 17:00 UTC.")) throw new Error("Partner source fields were missing.");
  await evaluate(`document.querySelector('.partner-admin-source-form button[type="submit"]')?.click()`);
  if (!await waitFor(`document.querySelector('.partner-source-list')?.innerText.includes('Partner service guide')`)) throw new Error("Admin partner source did not appear in the workspace source list.");
  const adminState = await evaluate(`({ calls: window.__partnerPortal?.calls ?? [], viewport: innerWidth, width: document.documentElement.scrollWidth,
    invitations: document.querySelector('.partner-admin-inline-form')?.innerText ?? '', sources: document.querySelector('.partner-source-list')?.innerText ?? '' })`);
  const adminCalls = adminState.calls.filter((entry) => entry.path.startsWith("/api/admin/partner-workspaces"));
  if (!adminCalls.some((entry) => entry.method === "POST" && entry.path === "/api/admin/partner-workspaces")
    || !adminCalls.some((entry) => entry.method === "POST" && entry.path.endsWith("/invitations"))
    || !adminCalls.some((entry) => entry.method === "POST" && entry.path.endsWith("/sources"))) throw new Error("Admin workspace, invitation or source actions missed their dedicated endpoint.");
  if (adminState.viewport !== 390 || adminState.width > 390) throw new Error("Admin partner controls overflow at 390px.");
  console.log("F12_ADMIN_WORKSPACE_INVITATION_SOURCE_UI_390PX=PASS");
  console.log("F12_CURRENT_SOURCE_PARTNER_PORTAL_BROWSER_ACCEPTANCE=PASS (synthetic in-page API; server authorization covered separately)");
}

run().catch((error) => {
  console.error(`F12_PARTNER_PORTAL_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
}).finally(async () => {
  try { socket?.close(); } catch {}
  for (const child of [browser, vite]) {
    if (child && child.exitCode === null) {
      child.kill();
      await new Promise((resolve) => { const timer = setTimeout(resolve, 2000); child.once("exit", () => { clearTimeout(timer); resolve(); }); });
    }
  }
  if (profile && existsSync(profile)) rmSync(profile, { recursive: true, force: true });
});
