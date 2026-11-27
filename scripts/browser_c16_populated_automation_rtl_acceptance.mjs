#!/usr/bin/env node
/** Focused C16 current-source RTL/accessibility check for the changed populated workflow screen. */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const repo = process.cwd();
const stackPath = path.resolve(process.env.C15_STACK_STATE ?? ".verification-local-review-20261029-c15-mywork-r3/stack-state.json");
const readJson = file => JSON.parse(readFileSync(file, "utf8").replace(/^\uFEFF/, ""));
const stack = readJson(stackPath);
const fixture = readJson(path.resolve(stack.credential_fixture));
const url = new URL(stack.frontend_url);
const evidenceDir = path.join(path.dirname(stackPath), "evidence");
const profile = path.join(path.dirname(stackPath), `browser-profile-c16-rtl-${Date.now()}`);
if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || existsSync(profile)) throw new Error("C16 populated RTL acceptance requires the isolated current-source loopback stack.");
mkdirSync(profile, { recursive: false });
const chromePath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find(value => value && existsSync(value));
if (!chromePath) throw new Error("Chrome or Edge is unavailable.");

const pending = new Map();
let sequence = 0;
let browser;
let socket;
function cdp(method, params = {}, timeoutMs = 25_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Chromium timed out during ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 25_000) {
  const result = await cdp("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("C16 current-source browser inspection raised an exception.");
  return result.result?.value;
}
async function waitFor(expression, timeoutMs = 35_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await delay(120);
  }
  return false;
}
async function navigate(target) {
  await cdp("Page.navigate", { url: target });
  await delay(200);
}
async function login() {
  if (!await waitFor(`Boolean(document.querySelector('form.login-form input[name="email"]'))`, 20_000)) throw new Error("C16 synthetic login form did not appear.");
  const accepted = await evaluate(`(()=>{const form=document.querySelector('form.login-form');const email=form?.querySelector('input[name="email"]');const pass=form?.querySelector('input[name="password"]');if(!form||!email||!pass)return false;for(const [el,value] of [[email,${JSON.stringify(fixture.manager.email)}],[pass,${JSON.stringify(fixture.manager.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
  if (!accepted || !await waitFor(`(async()=>{try{const r=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});return r.ok&&(await r.json()).email===${JSON.stringify(fixture.manager.email)}}catch{return false}})()`, 35_000)) throw new Error("The synthetic Manager session did not authenticate in the current-source app.");
}
async function screenshot(name) {
  const data = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  const filepath = path.join(evidenceDir, name);
  writeFileSync(filepath, Buffer.from(data.data, "base64"), { flag: "wx" });
  return filepath;
}

try {
  const env = { SYSTEMROOT: process.env.SYSTEMROOT, PATH: process.env.PATH, TEMP: process.env.TEMP, TMP: process.env.TMP, USERPROFILE: process.env.USERPROFILE, LOCALAPPDATA: process.env.LOCALAPPDATA };
  browser = spawn(chromePath, ["--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync", "--disable-background-networking", "--disable-component-update", "--headless=new", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "--window-size=1365,1000", "about:blank"], { stdio: "ignore", windowsHide: true, env });
  const activePort = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activePort) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activePort)) throw new Error("Isolated C16 Chromium did not start.");
  const port = Number(readFileSync(activePort, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline) {
    try { page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(item => item.type === "page" && item.webSocketDebuggerUrl); } catch {}
    if (!page) await delay(100);
  }
  if (!page) throw new Error("Isolated C16 browser target did not appear.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", () => reject(new Error("C16 debugger connection failed.")), { once: true }); });
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const call = pending.get(message.id); clearTimeout(call.timer); pending.delete(message.id);
    if (message.error) call.reject(new Error(`Chromium command failed: ${message.error.message}`)); else call.resolve(message.result);
  });
  await cdp("Page.enable"); await cdp("Runtime.enable");
  await cdp("Emulation.setDeviceMetricsOverride", { width: 1365, height: 1000, deviceScaleFactor: 1, mobile: false });
  await navigate(url.href);
  await login();
  if (!await waitFor("Boolean([...document.querySelectorAll('h1')].find(h=>h.textContent.trim()==='Workflows & tasks'))")) throw new Error("C16 populated workflow screen did not open.");
  if (!await waitFor("document.body.innerText.includes('Approved My Work assignment QA')&&document.body.innerText.includes('Review approved member access')&&document.body.innerText.includes('completed')", 25_000)) throw new Error("The changed workflow screen did not finish loading its populated record/history.");

  const inspect = () => evaluate(`(()=>{
    const root=document.documentElement, main=document.querySelector('main');
    const visible=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
    const controls=[...document.querySelectorAll('button,input:not([type=hidden]),select,textarea,[role=button]')].filter(visible);
    const unnamed=controls.filter(e=>{const aria=e.getAttribute('aria-label')?.trim();const labelled=e.getAttribute('aria-labelledby')?.split(/\\s+/).some(id=>document.getElementById(id)?.textContent.trim());const title=e.getAttribute('title')?.trim();const text=(e.innerText||e.value||'').trim();const labels=e.labels&&[...e.labels].some(l=>l.textContent.trim());const placeholder=e.getAttribute('placeholder')?.trim();return !(aria||labelled||title||text||labels||placeholder)}).map(e=>({tag:e.tagName.toLowerCase(),type:e.getAttribute('type')||''})).slice(0,12);
    return {direction:root.dir,computedDirection:getComputedStyle(main).direction,width:root.clientWidth,rootScrollWidth:root.scrollWidth,bodyScrollWidth:document.body.scrollWidth,mainWidth:main?.clientWidth??0,heading:[...(main?.querySelectorAll('h1,h2,h3')??[])].map(h=>h.textContent.trim()).filter(Boolean).slice(0,16),visibleControlCount:controls.length,unnamedControls:unnamed,workflowCards:document.querySelectorAll('.automation-item').length,completedText:document.body.innerText.includes('completed'),actionText:document.body.innerText.includes('Assign a task to My Work')||document.body.innerText.includes('Review approved member access')};
  })()`);
  const ltrDesktop = await inspect();
  if (ltrDesktop.workflowCards < 2 || !ltrDesktop.completedText) throw new Error("The C16 review page was not populated with workflow and run history.");

  const toggle = await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Switch to Right-to-Left (Arabic/Urdu) layout"]');if(!button)return false;button.click();return document.documentElement.dir==='rtl'&&localStorage.getItem('infraspine_direction')==='rtl'})()`);
  if (!toggle) throw new Error("The accessible RTL toggle did not switch and persist the direction.");
  const rtlDesktop = await inspect();
  await cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true, screenWidth: 390, screenHeight: 844 });
  await delay(250);
  const rtlMobile = await inspect();
  if (rtlDesktop.direction !== "rtl" || rtlDesktop.computedDirection !== "rtl" || rtlDesktop.rootScrollWidth > rtlDesktop.width || rtlDesktop.bodyScrollWidth > rtlDesktop.width) throw new Error("The populated workflow page failed desktop RTL layout.");
  if (rtlMobile.direction !== "rtl" || rtlMobile.computedDirection !== "rtl" || rtlMobile.rootScrollWidth > 390 || rtlMobile.bodyScrollWidth > 390) throw new Error("The populated workflow page overflows at 390px RTL.");
  if (rtlDesktop.unnamedControls.length || rtlMobile.unnamedControls.length) throw new Error("A visible workflow control is unnamed in the current-source RTL screen.");

  await evaluate("window.scrollTo(0,0)");
  const mobileScreenshot = await screenshot(`c16-populated-workflow-rtl-${Date.now()}.png`);
  await cdp("Page.reload", { ignoreCache: true });
  if (!await waitFor("document.documentElement.dir==='rtl'&&document.body.innerText.includes('Workflows & tasks')&&document.body.innerText.includes('Approved My Work assignment QA')", 30_000)) throw new Error("The RTL preference did not persist after a real page reload.");
  const persisted = await inspect();
  const focus = await evaluate(`(()=>{document.activeElement?.blur();return true})()`);
  await cdp("Input.dispatchKeyEvent", { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
  await cdp("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
  const keyboard = await evaluate(`(()=>{const el=document.activeElement;return {tag:el?.tagName?.toLowerCase()??null,aria:el?.getAttribute('aria-label')??null,text:(el?.innerText||el?.textContent||'').trim().slice(0,100),focusVisible:!!el?.matches(':focus-visible'),outline:getComputedStyle(el).outlineStyle}})()`);
  if (!focus || persisted.direction !== "rtl" || keyboard.tag === "body" || keyboard.tag === null) throw new Error("Keyboard navigation or persisted RTL state did not survive.");

  const evidence = {
    accepted: true,
    source: "current dirty worktree Vite/API stack; authenticated synthetic Manager; populated workflow/run records from C15 acceptance",
    route: "#/automation",
    frontend_url: stack.frontend_url,
    api_health_url: stack.api_health_url,
    database: stack.database,
    migration: stack.migration,
    rtl_toggle: "accessible button switched html[dir] and localStorage; preference remained after page reload",
    ltr_desktop: ltrDesktop,
    rtl_desktop: rtlDesktop,
    rtl_mobile: rtlMobile,
    persisted_rtl_after_reload: persisted,
    keyboard_tab_after_reload: keyboard,
    screenshot: path.basename(mobileScreenshot),
    source_hashes: Object.fromEntries(["frontend/src/App.tsx", "frontend/src/components/AdminConsole.tsx", "frontend/src/components/AutomationPage.tsx", "frontend/src/styles.css"].map(file => [file, createHash("sha256").update(readFileSync(path.join(repo,file))).digest("hex")])),
    captured_at: new Date().toISOString(),
  };
  const evidencePath = path.join(evidenceDir, "c16-populated-automation-rtl-accessibility.json");
  writeFileSync(evidencePath, `${JSON.stringify(evidence,null,2)}\n`, { flag: "wx" });
  console.log("C16_POPULATED_RTL_ACCEPTANCE=PASS");
  console.log("C16_METRICS=" + JSON.stringify({ desktop: { ltr: ltrDesktop.rootScrollWidth, rtl: rtlDesktop.rootScrollWidth }, mobile: { width: rtlMobile.width, root: rtlMobile.rootScrollWidth, body: rtlMobile.bodyScrollWidth }, visible_controls: rtlMobile.visibleControlCount, unnamed: rtlMobile.unnamedControls.length, rtl_persisted: persisted.direction, keyboard_focus: keyboard }));
} finally {
  if (socket) socket.close();
  if (browser && browser.exitCode === null) browser.kill();
}
