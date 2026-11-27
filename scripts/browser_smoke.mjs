#!/usr/bin/env node
/**
 * Dependency-free Chromium smoke check for the running InfraSpine frontend.
 * It starts only an isolated headless browser profile; it never starts or
 * changes the application API, worker, database, or vector store.
 */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import net from "node:net";

const url = process.argv[2] ?? "http://localhost:5173/";
const root = new URL(url);
if (!new Set(["http:", "https:"]).has(root.protocol)) {
  throw new Error("The browser smoke check accepts only HTTP(S) frontend URLs.");
}

function findBrowser() {
  const configured = process.env.INFRASPINE_BROWSER_PATH;
  const candidates = configured ? [configured] : process.platform === "win32"
    ? [
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
        path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
      ]
    : ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/microsoft-edge"];
  const browser = candidates.find((candidate) => candidate && existsSync(candidate));
  if (!browser) {
    throw new Error("Chromium, Chrome, or Edge was not found. Set INFRASPINE_BROWSER_PATH to its executable.");
  }
  return browser;
}

function choosePort() {
  return new Promise((resolve, reject) => {
    const listener = net.createServer();
    listener.once("error", reject);
    listener.listen(0, "127.0.0.1", () => {
      const address = listener.address();
      listener.close((error) => error ? reject(error) : resolve(address.port));
    });
  });
}

async function waitForTarget(port, browser) {
  const deadline = Date.now() + 30_000;
  let lastError;
  while (Date.now() < deadline && browser.exitCode === null) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      if (response.ok) {
        const targets = await response.json();
        const page = targets.find((target) => target.type === "page" && target.webSocketDebuggerUrl);
        if (page) return page;
      }
    } catch (error) {
      lastError = error;
    }
    await delay(150);
  }
  throw new Error(`Browser debugging endpoint did not become ready${lastError ? ` (${lastError.name})` : ""}.`);
}

function connectDebugger(endpoint) {
  const socket = new WebSocket(endpoint);
  const pending = new Map();
  let sequence = 0;
  const runtimeErrors = [];

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method === "Runtime.exceptionThrown") runtimeErrors.push("page exception");
    if (message.id && pending.has(message.id)) {
      const { resolve, reject, timer } = pending.get(message.id);
      clearTimeout(timer);
      pending.delete(message.id);
      if (message.error) reject(new Error(`Browser command failed: ${message.error.message}`));
      else resolve(message.result);
    }
  });

  const ready = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("Could not connect to the browser debugging endpoint.")), { once: true });
  });

  return {
    socket,
    runtimeErrors,
    async ready() { await ready; },
    command(method, params = {}) {
      const id = ++sequence;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(new Error(`Browser command timed out: ${method}`));
        }, 10_000);
        pending.set(id, { resolve, reject, timer });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
  };
}

const browserPath = findBrowser();
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-browser-smoke-"));
const port = await choosePort();
const browser = spawn(browserPath, [
  "--headless=new",
  "--disable-gpu",
  "--no-first-run",
  "--no-default-browser-check",
  "--disable-extensions",
  "--disable-background-networking",
  "--disable-sync",
  "--remote-debugging-port=0",
  `--user-data-dir=${profile}`,
  url,
], { stdio: "ignore", windowsHide: true });

let socket;
let exitCode = 1;
try {
  const activePortFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activePortFile) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePortFile)) throw new Error("The browser did not publish its local debugging port.");
  const debugPort = Number(readFileSync(activePortFile, "utf8").split(/\r?\n/)[0]);
  if (!Number.isInteger(debugPort) || debugPort < 1 || debugPort > 65_535) throw new Error("Invalid browser debug port.");

  const page = await waitForTarget(debugPort, browser);
  const cdp = connectDebugger(page.webSocketDebuggerUrl);
  socket = cdp.socket;
  await cdp.ready();
  await cdp.command("Page.enable");
  await cdp.command("Runtime.enable");
  await cdp.command("Page.navigate", { url });
  await delay(2_500);

  const evaluated = await cdp.command("Runtime.evaluate", {
    expression: `(() => {
      const root = document.querySelector('#root');
      const text = root?.innerText ?? '';
      return {
        title: document.title,
        rootRendered: Boolean(root && root.childElementCount),
        brandVisible: /InfraSpine/i.test(text),
        emailField: Boolean(root?.querySelector('input[type="email"]')),
        passwordField: Boolean(root?.querySelector('input[type="password"]')),
        signInAction: /sign in|log in/i.test(text),
        appContentVisible: text.length > 20,
      };
    })()`,
    returnByValue: true,
  });
  const result = evaluated.result?.value;
  if (!result) throw new Error("The page rendered no inspectable application state.");
  const passed = result.rootRendered && result.brandVisible && result.emailField && result.passwordField && result.signInAction;
  console.log(JSON.stringify({
    url,
    browser: path.basename(browserPath),
    ...result,
    pageExceptions: cdp.runtimeErrors.length,
    result: passed && cdp.runtimeErrors.length === 0 ? "PASS" : "FAIL",
  }));
  if (!passed || cdp.runtimeErrors.length) throw new Error("The login screen did not render cleanly in Chromium.");
  exitCode = 0;
} finally {
  socket?.close();
  if (browser.exitCode === null) {
    browser.kill();
    await Promise.race([new Promise((resolve) => browser.once("exit", resolve)), delay(3_000)]);
  }
  const safeProfile = path.resolve(profile);
  const safeTempRoot = `${path.resolve(tmpdir())}${path.sep}`;
  if (safeProfile.startsWith(safeTempRoot) && path.basename(safeProfile).startsWith("infraspine-browser-smoke-")) {
    rmSync(safeProfile, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
  }
}
process.exitCode = exitCode;
