#!/usr/bin/env node
/**
 * Browser role acceptance on the local review stack: each synthetic account (from role_qa_setup.py)
 * signs in through the real login form in an isolated headless browser, and the script records the
 * landing page, the navigation it is offered, and what happens when it opens Administrator pages.
 * Credentials are read from the protected fixture and never printed. Screenshots go to evidence/.
 */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const root = path.resolve(".verification-live-review-20261029");
const base = "http://127.0.0.1:5182/admin/";
const evidenceDir = path.join(root, "evidence", "browser-role-qa-" + new Date().toISOString().replace(/[:.]/g, "-"));
mkdirSync(evidenceDir, { recursive: true });
const fixture = JSON.parse(readFileSync(path.join(root, "role-qa-fixture.json"), "utf8"));
const adminPassword = JSON.parse(readFileSync(path.join(root, "credentials-fixture.json"), "utf8").replace(/^\uFEFF/, ""))["admin@livereview.test"].password;
const accounts = [
  { key: "admin", email: "admin@livereview.test", password: adminPassword, role: "admin" },
  ...["manager_a", "employee_a", "employee_b"].map(key => ({ key, email: fixture[key].email, password: fixture[key].password, role: fixture[key].role })),
];
const ADMIN_PAGES = ["people", "providers", "settings", "reports", "logs"];

const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find(candidate => candidate && existsSync(candidate));
if (!browserPath) throw new Error("Chrome or Edge is unavailable.");

async function openBrowser() {
  const profile = mkdtempSync(path.join(tmpdir(), "infraspine-role-qa-"));
  const proc = spawn(browserPath, [
    "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync", "--headless=new",
    "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", "--user-data-dir=" + profile,
    "--window-size=1440,900", "about:blank",
  ], { stdio: "ignore", windowsHide: true });
  const activeFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activeFile) && Date.now() < deadline) await delay(100);
  const port = Number(readFileSync(activeFile, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline) {
    try { page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(item => item.type === "page"); } catch {}
    if (!page) await delay(150);
  }
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = () => reject(new Error("debugger connection failed")); });
  let sequence = 0;
  const pending = new Map();
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    const item = pending.get(message.id);
    if (!item) return;
    pending.delete(message.id);
    message.error ? item.reject(new Error(message.error.message)) : item.resolve(message.result);
  };
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async expression => (await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true })).result?.value;
  const waitFor = async (expression, ms = 30_000) => {
    const end = Date.now() + ms;
    while (Date.now() < end) { if (await evaluate(expression)) return true; await delay(200); }
    return false;
  };
  const shot = async name => {
    const { data } = await call("Page.captureScreenshot", { format: "png" });
    writeFileSync(path.join(evidenceDir, name + ".png"), Buffer.from(data, "base64"));
  };
  const close = () => { socket.close(); proc.kill(); try { rmSync(profile, { recursive: true, force: true }); } catch {} };
  await call("Page.enable");
  await call("Runtime.enable");
  return { call, evaluate, waitFor, shot, close };
}

const report = [];
for (const account of accounts) {
  const b = await openBrowser();
  const entry = { account: account.key, role: account.role };
  try {
    await b.call("Page.navigate", { url: base });
    await b.waitFor("!!document.querySelector('input[type=email], input[name=email]')");
    // Fill the real login form the way a person would (React-controlled inputs).
    await b.evaluate(`(() => {
      const set = (el, value) => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, value); el.dispatchEvent(new Event("input", { bubbles: true })); };
      set(document.querySelector("input[type=email], input[name=email]"), ${JSON.stringify(account.email)});
      set(document.querySelector("input[type=password]"), ${JSON.stringify(account.password)});
      return true;
    })()`);
    await delay(400); // let React commit the typed values before submitting
    await b.evaluate(`(() => {
      const button = [...document.querySelectorAll("button")].find(el => el.textContent.trim() === "Sign in");
      button.click();
      return true;
    })()`);
    entry.signed_in = await b.waitFor("!!document.querySelector('nav') && !document.querySelector('input[type=password]')", 30_000);
    await delay(2500);
    entry.landing = await b.evaluate("location.hash || '#/'");
    entry.landing_title = await b.evaluate("document.querySelector('main h1, main h2')?.textContent?.trim() ?? null");
    entry.navigation = await b.evaluate("[...document.querySelectorAll('nav a, nav button')].map(e => e.getAttribute('aria-label') || e.textContent.trim()).filter(Boolean)");
    await b.shot(`${account.key}-landing`);
    entry.admin_pages = {};
    for (const view of ADMIN_PAGES) {
      await b.evaluate(`location.hash = "/${view}"`);
      await delay(1800);
      const landed = await b.evaluate("location.hash");
      const heading = await b.evaluate("document.querySelector('main h1')?.textContent?.trim() ?? null");
      entry.admin_pages[view] = { landed, heading };
    }
    await b.shot(`${account.key}-after-admin-attempts`);
  } catch (error) {
    entry.error = String(error.message || error);
  } finally {
    b.close();
  }
  report.push(entry);
}
writeFileSync(path.join(evidenceDir, "report.json"), JSON.stringify(report, null, 2));
console.log(JSON.stringify({ evidence: path.relative(process.cwd(), evidenceDir), report }, null, 1));
