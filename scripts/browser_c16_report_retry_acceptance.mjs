#!/usr/bin/env node
/** Current-source synthetic browser acceptance for Usage report timeout and retry. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_C16_REVIEW_URL ?? "http://127.0.0.1:5181/admin/");
const fixturePath = process.env.INFRASPINE_C16_REVIEW_FIXTURE;
const reviewRoot = path.resolve(process.env.INFRASPINE_C16_REVIEW_ROOT ?? "");
if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || !fixturePath || reviewRoot === path.parse(reviewRoot).root) {
  throw new Error("C16 acceptance requires the loopback review URL and protected synthetic fixture.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
if (!fixture.admin?.email || !fixture.admin?.password) throw new Error("The protected Admin fixture is incomplete.");
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for C16 acceptance.");

const tempRoot = path.join(reviewRoot, "tmp");
mkdirSync(tempRoot, { recursive: true });
const profile = mkdtempSync(path.join(tempRoot, "browser-c16-"));
const evidenceDir = path.join(reviewRoot, "evidence");
mkdirSync(evidenceDir, { recursive: true });
let chrome;
let socket;
let sequence = 0;
let pausedReportRequests = 0;
let reportRequestCount = 0;
let retryReportStatus = null;
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
  if (response.exceptionDetails) throw new Error("A C16 browser action failed.");
  return response.result?.value;
}
async function waitFor(expression, label, timeoutMs = 35_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(125);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function clickByText(text, selector = "button") {
  return evaluate(`(()=>{const item=[...document.querySelectorAll(${JSON.stringify(selector)})].find((node)=>node.textContent.trim()===${JSON.stringify(text)});if(!item)return false;item.click();return true})()`);
}

try {
  chrome = spawn(browserPath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < deadline) await delay(100);
  if (!existsSync(portFile)) throw new Error("Isolated Chromium did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!page?.webSocketDebuggerUrl) throw new Error("Isolated Chromium did not expose a page target.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method === "Fetch.requestPaused" && message.params.request.url.includes("/api/admin/reports/usage?days=")) {
      pausedReportRequests += 1;
      const requestId = message.params.requestId;
      // The page's shortened test clock aborts this pending request first. Fail it shortly after
      // so the browser can deliver the aborted response into the actual React timeout handler.
      setTimeout(() => {
        void command("Fetch.failRequest", { requestId, errorReason: "Aborted" }).catch(() => {});
      }, 350);
    }
    if (message.method === "Network.requestWillBeSent" && message.params.request.url.includes("/api/admin/reports/usage?days=")) {
      reportRequestCount += 1;
    }
    if (message.method === "Network.responseReceived" && message.params.response.url.includes("/api/admin/reports/usage?days=")) {
      retryReportStatus = message.params.response.status;
    }
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error("A Chrome DevTools command failed.")) : item.resolve(message.result);
  });

  await command("Page.enable");
  await command("Runtime.enable");
  await command("Network.enable");
  await command("Page.navigate", { url: base.href });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "login form");
  const loginSubmitted = await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const pass=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!pass||!form)return false;for(const [el,value] of [[email,${JSON.stringify(fixture.admin.email)}],[pass,${JSON.stringify(fixture.admin.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
  if (!loginSubmitted) throw new Error("Synthetic Admin login form was unavailable.");
  await waitFor("Boolean(document.querySelector('.shell')||document.querySelector('.first-run-setup h1'))", "authenticated Admin", 45_000);
  const setupRequired = await evaluate("document.querySelector('.first-run-setup h1')?.textContent.trim()==='Company setup'");
  if (setupRequired) throw new Error("C16 review fixture unexpectedly requires company setup; refusing to create extra setup state.");
  await waitFor("Boolean(document.querySelector('.shell'))", "review shell");

  await command("Emulation.setDeviceMetricsOverride", { width: 1365, height: 900, deviceScaleFactor: 1, mobile: false });
  await evaluate(`(()=>{const original=window.setTimeout.bind(window);window.setTimeout=(callback,delayMs,...args)=>original(callback,delayMs===30000?100:delayMs,...args);return true})()`);
  await command("Fetch.enable", { patterns: [{ urlPattern: "*api/admin/reports/usage?days=*", requestStage: "Request" }] });
  const navOpened = await clickByText("Usage reports");
  if (!navOpened) throw new Error("The Usage reports navigation control was unavailable.");
  await waitFor("document.querySelector('h1')?.textContent.trim()==='Usage reports'", "Usage reports page");

  try {
    await waitFor("document.querySelector('[role=alert]')?.textContent.includes('usage report timed out')", "timeout recovery message", 15_000);
  } catch {
    const state = await evaluate("({alert:document.querySelector('[role=alert]')?.textContent??null,busy:!!document.querySelector('.loading'),report:!!document.querySelector('.report-metrics')})");
    throw new Error(`Timeout branch did not render (paused=${pausedReportRequests}, requests=${reportRequestCount}, state=${JSON.stringify(state)}).`);
  }
  await waitFor("[...document.querySelectorAll('button')].some((button)=>button.textContent.trim()==='Retry report'&&!button.disabled)", "enabled report retry button", 5_000);
  const retryVisible = await evaluate("[...document.querySelectorAll('button')].some((button)=>button.textContent.trim()==='Retry report'&&!button.disabled)");
  if (!retryVisible || pausedReportRequests < 1 || pausedReportRequests > 2) throw new Error(`The timeout did not expose a retryable failed report request (paused=${pausedReportRequests}, retry=${retryVisible}).`);

  await delay(450);
  await command("Fetch.disable");
  const controls = await evaluate(`(()=>{const retry=[...document.querySelectorAll('button')].find((button)=>button.textContent.trim()==='Retry report');const names=[...document.querySelectorAll('.report-actions button')].map((button)=>button.getAttribute('aria-label')||button.textContent.trim());return {retryFocused:!!retry&&!retry.disabled&&(retry.focus(),document.activeElement===retry),periodLabel:document.querySelector('.report-actions select')?.getAttribute('aria-label')??null,actionNames:names}})()`);
  if (!controls.retryFocused || controls.periodLabel !== "Report period" || controls.actionNames.some((name) => !name)) {
    throw new Error("Usage report controls did not expose keyboard focus and accessible names.");
  }
  await command("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, text: "\r", unmodifiedText: "\r" });
  await command("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
  try {
    await waitFor("Boolean(document.querySelector('.report-metrics'))", "successful retry report", 20_000);
  } catch {
    const state = await evaluate("({alert:document.querySelector('[role=alert]')?.textContent??null,busy:!!document.querySelector('.loading'),report:!!document.querySelector('.report-metrics'),active:document.activeElement?.textContent?.trim()??null})");
    throw new Error(`Keyboard retry did not complete (requests=${reportRequestCount}, status=${retryReportStatus}, state=${JSON.stringify(state)}).`);
  }
  if (retryReportStatus !== 200 || reportRequestCount < 2) throw new Error("The retry did not return a successful Usage report response.");

  await command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true, screenWidth: 390, screenHeight: 844 });
  await delay(300);
  const layout = await evaluate("({viewport:innerWidth,root:document.documentElement.scrollWidth,body:document.body.scrollWidth,report:!!document.querySelector('.report-metrics')})");
  const screenshot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(path.join(evidenceDir, "c16-report-retry-mobile.png"), Buffer.from(screenshot.data, "base64"));
  const evidence = {
    review_url: base.href,
    synthetic_admin_login: true,
    timeout_message: "The usage report timed out. Check service health, then try again.",
    timeout_request_held: pausedReportRequests > 0,
    held_report_requests: pausedReportRequests,
    retry_button_enabled: retryVisible,
    retry_keyboard_activation: true,
    retry_focused_before_activation: controls.retryFocused,
    accessible_report_controls: { period: controls.periodLabel, actions: controls.actionNames },
    report_requests: reportRequestCount,
    retry_http_status: retryReportStatus,
    mobile_layout: layout,
    checks: ["synthetic Admin login", "controlled report request timeout", "keyboard Enter activation of Retry report", "successful current-source retry", "390px report viewport"],
  };
  if (!layout.report || layout.viewport !== 390 || layout.body > 390) throw new Error("The mobile report layout did not fit its 390px viewport.");
  writeFileSync(path.join(evidenceDir, "c16-report-retry-browser.json"), `${JSON.stringify(evidence, null, 2)}\n`, "utf8");
  console.log("C16 current-source Usage report timeout/retry acceptance passed.");
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (chrome && chrome.exitCode === null) {
    chrome.kill();
    await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3_000)]);
  }
  const safeTempRoot = path.resolve(tempRoot);
  const safeProfile = path.resolve(profile);
  if (safeProfile.startsWith(`${safeTempRoot}${path.sep}`) && path.basename(safeProfile).startsWith("browser-c16-")) {
    rmSync(safeProfile, { recursive: true, force: true });
  }
}
