/** Real HTTP + authenticated Chromium UI checks, called only by the isolated pytest harness. */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, writeFileSync, rmSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.argv[2]);
if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || !process.env.TEST_DATABASE_URL || process.env.APP_ENV !== "testing") throw new Error("Use the disposable PostgreSQL browser harness.");
const fixture = JSON.parse(readFileSync(process.env.KNOWLEDGE_BROWSER_FIXTURE, "utf8"));
const evidence = path.resolve(process.env.KNOWLEDGE_BROWSER_EVIDENCE);
mkdirSync(evidence, { recursive: true });
const browserPath = [process.env.INFRASPINE_BROWSER_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "/usr/bin/chromium", "/usr/bin/google-chrome"].find((candidate) => candidate && existsSync(candidate));
if (!browserPath) throw new Error("Install Chromium or set INFRASPINE_BROWSER_PATH.");
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-knowledge-browser-"));
const browser = spawn(browserPath, ["--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync", "--disable-background-networking", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
let socket;
let diagnostics;
let phase = "startup";
const report = { passed: false, provider: "deterministic UI fixture; not a model benchmark", roles: [], errors: [] };
const failures = [];
try {
  const active = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(active) && browser.exitCode === null && Date.now() < deadline) await delay(100);
  if (!existsSync(active)) throw new Error("Chromium did not start.");
  const port = Number(readFileSync(active, "utf8").split(/\r?\n/)[0]);
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  socket = new WebSocket(targets.find((target) => target.type === "page").webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let sequence = 0;
  const pending = new Map();
  socket.onmessage = ({ data }) => {
    const message = JSON.parse(data);
    if (message.method === "Runtime.exceptionThrown") failures.push("Uncaught page exception");
    if (message.id && pending.has(message.id)) {
      const task = pending.get(message.id); pending.delete(message.id); clearTimeout(task.timer);
      message.error ? task.reject(new Error(message.error.message)) : task.resolve(message.result);
    }
  };
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out: ${method}`)); }, 15_000);
    pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => {
    const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
    if (result.exceptionDetails) throw new Error(`Browser action failed during ${phase}`);
    return result.result?.value;
  };
  const wait = async (expression, timeout = 25_000) => {
    const end = Date.now() + timeout;
    while (Date.now() < end) { if (await evaluate(expression)) return; await delay(150); }
    throw new Error(`UI condition timed out during ${phase}`);
  };
  const click = async (label, scope = "document") => {
    const found = await evaluate(`(() => { const root = ${scope}; const b = [...root.querySelectorAll('button')].find(b => (b.getAttribute('aria-label') || b.textContent.trim()) === ${JSON.stringify(label)}); if (!b || b.disabled) return false; b.click(); return true; })()`);
    if (!found) throw new Error(`Control unavailable: ${label} (${phase})`);
  };
  const fill = async (selector, value) => evaluate(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); if (!e) throw Error('missing field'); Object.getOwnPropertyDescriptor(e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value').set.call(e, ${JSON.stringify(value)}); e.dispatchEvent(new Event('input', {bubbles:true})); })()`);
  const screenshot = async (name) => {
    const { data } = await call("Page.captureScreenshot", { format: "png" });
    writeFileSync(path.join(evidence, `${name}.png`), Buffer.from(data, "base64"));
  };
  diagnostics = async () => {
    await screenshot("failure");
    // Synthetic fixture only; do not collect form input values, cookies or session storage.
    const state = await evaluate("({hash:location.hash, headings:[...document.querySelectorAll('h1,h2,h3')].map(e=>e.textContent), alerts:[...document.querySelectorAll('[role=alert]')].map(e=>e.textContent)})");
    writeFileSync(path.join(evidence, "failure.json"), JSON.stringify(state, null, 2));
  };
  await call("Page.enable"); await call("Runtime.enable"); await call("Network.enable");
  for (const account of fixture.accounts) {
    phase = `${account.role} sign-in`;
    await call("Network.clearBrowserCookies");
    await call("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
    await call("Page.navigate", { url: new URL("#/knowledge", base).href });
    await wait("!!document.querySelector('input[type=email]')");
    await fill('input[type="email"]', account.email); await fill('input[type="password"]', account.password);
    await click("Sign in");
    await wait("!!document.querySelector('[aria-label=\"Knowledge workspace\"]')");
    const result = { role: account.role, login: true, checks: [] };
    const workspace = "document.querySelector('[aria-label=\"Knowledge workspace\"]')";
    phase = `${account.role} overview`;
    await click("Overview", workspace);
    await wait("!!document.querySelector('[aria-label=\"Knowledge dashboard\"]')");
    await wait("!document.querySelector('[aria-label=\"Knowledge dashboard\"]').textContent.includes('Loading formats')");
    const hasActivity = await evaluate("!!document.querySelector('[aria-label=\"Assistant activity\"]')");
    if (hasActivity !== (account.role === "admin")) throw new Error("Activity analytics role visibility is incorrect.");
    const responseStatus = await evaluate("fetch('/api/admin/traces', {headers:{'X-Requested-With':'InfraSpineAdmin'}}).then(r => r.status)");
    if (responseStatus !== (account.role === "admin" ? 200 : 403)) throw new Error("Activity API role authorization is incorrect.");
    result.checks.push("overview", "analytics RBAC");
    await screenshot(`${account.role}-overview-desktop`);
    phase = `${account.role} library`;
    await click("Library", workspace);
    await wait("document.querySelector('[aria-label=\"Document library\"]').getBoundingClientRect().width > 0");
    await wait("document.querySelector('[aria-label=\"Document library\"]').textContent.includes('knowledge-acceptance.pdf')");
    result.checks.push("authorized library");
    await click("Chat", workspace);
    phase = `${account.role} chat`;
    await fill('textarea[aria-label="Question"]', "Using general knowledge, show a Python addition function and a comparison table.");
    await click("Send");
    await wait("!!document.querySelector('.answer-code code') && !!document.querySelector('.markdown table')");
    await wait("!!document.querySelector('[aria-label=\"Copy answer\"]')");
    const savedUrl = await evaluate("location.href");
    if (!savedUrl.includes("#/c/")) throw new Error("Conversation was not persisted.");
    await click("Copy code");
    await wait("document.body.textContent.includes('Copied to clipboard')");
    await call("Page.reload");
    await wait("!!document.querySelector('.answer-code code') && !!document.querySelector('.markdown table')");
    result.checks.push("message API", "fenced code", "syntax highlighting", "table", "clipboard", "reload persistence");
    await screenshot(`${account.role}-chat-desktop`);
    phase = `${account.role} source citations`;
    await fill('textarea[aria-label="Question"]', "How many days are allowed for refund requests in knowledge-acceptance.pdf?");
    await click("Send");
    await wait("!!document.querySelector('button[title*=\"knowledge-acceptance.pdf\"]')");
    await evaluate("document.querySelector('button[title*=\"knowledge-acceptance.pdf\"]').click()");
    await wait("[...document.querySelectorAll('blockquote')].some(e => e.textContent.includes('30 days'))");
    result.checks.push("retrieval citation", "source preview");
    phase = `${account.role} mobile`;
    await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await delay(250);
    const overflow = await evaluate("document.documentElement.scrollWidth > innerWidth + 1");
    if (overflow) throw new Error("Mobile viewport has horizontal page overflow.");
    await screenshot(`${account.role}-chat-mobile`);
    await click("Toggle chat history");
    await wait("document.querySelector('[aria-label=\"Conversations\"]').getBoundingClientRect().width > 0");
    await click("New chat");
    await wait("!location.hash.startsWith('#/c/')");
    await click("Overview", workspace);
    await screenshot(`${account.role}-overview-mobile`);
    await evaluate("document.documentElement.dataset.theme = 'dark'");
    await screenshot(`${account.role}-overview-dark`);
    result.checks.push("mobile width", "history drawer", "new chat", "dark theme");
    report.roles.push(result);
  }
  if (failures.length) throw new Error(`${failures.length} uncaught browser exceptions`);
  report.passed = true;
} catch (error) {
  try { await diagnostics?.(); } catch {}
  report.errors.push({ phase, message: error.message });
  process.exitCode = 1;
} finally {
  writeFileSync(path.join(evidence, "report.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ passed: report.passed, roles: report.roles.map((role) => role.role), errors: report.errors }));
  socket?.close();
  if (browser.exitCode === null) { browser.kill(); await Promise.race([new Promise((resolve) => browser.once("exit", resolve)), delay(3_000)]); }
  const target = path.resolve(profile);
  if (path.dirname(target) === path.resolve(tmpdir()) && path.basename(target).startsWith("infraspine-knowledge-browser-")) rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 150 });
}
