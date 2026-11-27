import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.argv[2] ?? "http://127.0.0.1:5182/admin/");
const fixturePath = path.resolve(process.env.INFRASPINE_C04_REVIEW_FIXTURE ?? "");
const reviewRoot = path.resolve(process.env.INFRASPINE_C04_REVIEW_ROOT ?? "");
if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || !existsSync(fixturePath) || !reviewRoot.startsWith(path.resolve("."))) {
  throw new Error("C04 mobile audit requires the loopback synthetic review URL and protected fixture.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const chromePath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!fixture.admin?.email || !fixture.admin?.password || !chromePath) throw new Error("C04 synthetic Admin or browser fixture is unavailable.");

const tempRoot = path.join(reviewRoot, "tmp");
const evidenceDir = path.join(reviewRoot, "evidence");
mkdirSync(tempRoot, { recursive: true }); mkdirSync(evidenceDir, { recursive: true });
const profile = mkdtempSync(path.join(tempRoot, "browser-c04-mobile-"));
let chrome;
let socket;
let nextId = 0;
const pending = new Map();

function command(method, params = {}) {
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`C04 browser timed out at ${method}.`)); }, 20_000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const result = await command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error("C04 mobile inspection failed.");
  return result.result?.value;
}
async function waitFor(expression, label, maxMs = 30_000) {
  const until = Date.now() + maxMs;
  while (Date.now() < until) { if (await evaluate(expression)) return; await delay(100); }
  throw new Error(`C04 mobile audit timed out waiting for ${label}.`);
}

try {
  chrome = spawn(chromePath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const portPath = path.join(profile, "DevToolsActivePort");
  const startUntil = Date.now() + 30_000;
  while (!existsSync(portPath) && Date.now() < startUntil) await delay(100);
  if (!existsSync(portPath)) throw new Error("Isolated C04 Chromium did not start.");
  const port = readFileSync(portPath, "utf8").split(/\r?\n/)[0];
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!page?.webSocketDebuggerUrl) throw new Error("Isolated C04 Chromium did not expose a page.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data); const item = pending.get(msg.id); if (!item) return;
    clearTimeout(item.timer); pending.delete(msg.id);
    msg.error ? item.reject(new Error("A C04 DevTools command failed.")) : item.resolve(msg.result);
  });
  await command("Page.enable"); await command("Runtime.enable"); await command("Page.navigate", { url: base.href });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "synthetic login form");
  await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const password=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');for(const [input,value] of [[email,${JSON.stringify(fixture.admin.email)}],[password,${JSON.stringify(fixture.admin.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,value);input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
  await waitFor("Boolean(document.querySelector('.shell'))", "authenticated synthetic Admin", 45_000);
  const opened = await evaluate(`(()=>{const button=[...document.querySelectorAll('nav[aria-label="Main navigation"] button')].find((item)=>item.textContent.trim()==='People & access');if(!button)return false;button.click();return true})()`);
  if (!opened) throw new Error("People & access navigation was unavailable.");
  await waitFor("document.querySelector('main h1')?.textContent.trim()==='People & access'", "People & access screen");

  const measurements = [];
  async function measure(width, direction, includeToastStack) {
    await command("Emulation.setDeviceMetricsOverride", { width, height: 844, deviceScaleFactor: 1, mobile: width < 600, screenWidth: width, screenHeight: 844 });
    await delay(200);
    const beforeToast = await evaluate(`(()=>{const root=document.documentElement;return {viewport:root.clientWidth,window_inner_width:window.innerWidth,visual_viewport_width:window.visualViewport?.width??null,visual_viewport_scale:window.visualViewport?.scale??null,root_scroll_width:root.scrollWidth,body_scroll_width:document.body.scrollWidth}})()`);
    if (includeToastStack) {
      await evaluate(`(()=>{const host=document.querySelector('.toasts');if(!host)return false;host.innerHTML=${JSON.stringify('<div class="toast toast-success"><span class="toast-icon">✓</span><div class="toast-text"><strong>Synthetic role assignment revoked</strong><p>Scoped permissions no longer apply to this account or its active department assignments.</p></div><button class="icon-btn" aria-label="Dismiss">×</button></div><div class="toast toast-success"><span class="toast-icon">✓</span><div class="toast-text"><strong>Synthetic role template created</strong><p>This immutable template requires explicit independent approval before its permissions can be changed.</p></div><button class="icon-btn" aria-label="Dismiss">×</button></div><div class="toast toast-success"><span class="toast-icon">✓</span><div class="toast-text"><strong>Synthetic role template assigned</strong><p>One permission grant applies only in the selected department until its expiry date.</p></div><button class="icon-btn" aria-label="Dismiss">×</button></div>')};return true})()`);
    }
    measurements.push({ before_toast: beforeToast, ...(await evaluate(`(()=>{const root=document.documentElement;const viewport=root.clientWidth;const rack=document.querySelector('.toasts');const toasts=[...(rack?.querySelectorAll('.toast')??[])].map((el)=>{const r=el.getBoundingClientRect();return {left:Math.round(r.left),right:Math.round(r.right),width:Math.round(r.width)}});const offenders=[...document.querySelectorAll('body *')].map((el)=>({el,r:el.getBoundingClientRect()})).filter(({el,r})=>r.width>0&&r.height>0&&r.right>viewport+1&&!el.closest('.nav')).sort((a,b)=>b.r.right-a.r.right).slice(0,8).map(({el,r})=>({tag:el.tagName.toLowerCase(),class:typeof el.className==='string'?el.className.slice(0,60):'',right:Math.round(r.right),width:Math.round(r.width)}));const tables=[...document.querySelectorAll('.people-section .table-wrap')].map((wrap)=>({wrap:Math.round(wrap.getBoundingClientRect().width),wrapRight:Math.round(wrap.getBoundingClientRect().right),scroll:wrap.scrollWidth,table:Math.round(wrap.querySelector('table')?.getBoundingClientRect().width??0),overflow:getComputedStyle(wrap).overflowX}));return {route:'People & access',sample:${JSON.stringify(direction)},synthetic_toasts:${includeToastStack},viewport,window_inner_width:window.innerWidth,visual_viewport_width:window.visualViewport?.width??null,visual_viewport_scale:window.visualViewport?.scale??null,device_pixel_ratio:window.devicePixelRatio,root_scroll_width:root.scrollWidth,body_scroll_width:document.body.scrollWidth,main:document.querySelector('main')?.getBoundingClientRect().toJSON(),tables,toasts,offenders}})()`)) });
    if (includeToastStack) await evaluate("(()=>{const rack=document.querySelector('.toasts');if(rack)rack.replaceChildren();return true})()");
  }
  await measure(1365, "desktop-ltr", false);
  await measure(390, "mobile-ltr-with-notices", true);
  const rtl = await evaluate(`(()=>{const button=document.querySelector('button[aria-label*="Right-to-Left"]');if(!button)return false;button.click();return document.documentElement.dir==='rtl'})()`);
  if (!rtl) throw new Error("The global RTL toggle was not available.");
  await measure(390, "mobile-rtl-with-notices", true);
  await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Switch to Left-to-Right layout"]');if(button)button.click();return true})()`);
  const violations = measurements.filter((item) => item.root_scroll_width > item.viewport || item.body_scroll_width > item.viewport || item.toasts.some((toast) => toast.left < 0 || toast.right > item.viewport));
  const output = path.join(evidenceDir, "c04-people-mobile-toast-audit.json");
  writeFileSync(output, `${JSON.stringify({ review_url: base.href, synthetic_admin_login: true, toast_dom_matches_feedback_component: true, measurements }, null, 2)}\n`, "utf8");
  console.log(JSON.stringify({ result: violations.length ? "failed" : "passed", samples: measurements.length, violation_count: violations.length, measurements, evidence: output }));
  if (violations.length) throw new Error("C04 People & access overflowed the tested viewport or synthetic notification stack; measurements were saved locally.");
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (chrome && chrome.exitCode === null) { chrome.kill(); await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3_000)]); }
  const resolvedRoot = path.resolve(tempRoot); const resolvedProfile = path.resolve(profile);
  if (resolvedProfile.startsWith(`${resolvedRoot}${path.sep}`) && path.basename(resolvedProfile).startsWith("browser-c04-mobile-")) rmSync(resolvedProfile, { recursive: true, force: true });
}
