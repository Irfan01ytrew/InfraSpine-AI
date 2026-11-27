import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.argv[2] ?? "http://127.0.0.1:5181/admin/");
const fixturePath = path.resolve(process.env.INFRASPINE_C16_REVIEW_FIXTURE ?? "");
const reviewRoot = path.resolve(process.env.INFRASPINE_C16_REVIEW_ROOT ?? "");
if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || !fixturePath || reviewRoot === path.parse(reviewRoot).root) {
  throw new Error("C16 route audit requires a loopback URL and protected synthetic fixture.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
if (!fixture.admin?.email || !fixture.admin?.password) throw new Error("The protected Admin fixture is incomplete.");
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for C16 route audit.");

const tempRoot = path.join(reviewRoot, "tmp");
mkdirSync(tempRoot, { recursive: true });
const profile = mkdtempSync(path.join(tempRoot, "browser-c16-routes-"));
const evidenceDir = path.join(reviewRoot, "evidence");
mkdirSync(evidenceDir, { recursive: true });
let chrome;
let socket;
let sequence = 0;
const pending = new Map();
const routes = [
  "Knowledge", "Account security", "Knowledge interviews", "My work", "Company data research",
  "Tender & RFP coverage", "What-if forecasts", "Market & competitor research", "Employee training",
  "Meeting minutes", "Expertise directory", "Org knowledge", "Claims", "Approvals", "Workflows & tasks",
  "Agent profiles", "Knowledge handoffs", "Help & requests", "Memory", "People & access", "Cloud connectors",
  "External partner access", "Client & project workspaces", "Company setup", "Usage reports", "Settings",
  "AI Providers", "Traces", "Audit logs",
];
const focusedRoutes = new Set(["Knowledge", "Workflows & tasks", "Agent profiles", "External partner access", "Cloud connectors", "People & access", "Settings", "Usage reports"]);

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
  if (response.exceptionDetails) throw new Error("A C16 browser inspection failed.");
  return response.result?.value;
}
async function waitFor(expression, label, timeoutMs = 25_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(120);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function clickNav(label) {
  const escaped = JSON.stringify(label);
  return evaluate(`(()=>{const nav=document.querySelector('nav[aria-label="Main navigation"]');const item=[...(nav?.querySelectorAll('button')??[])].find((node)=>node.textContent.trim()===${escaped});if(!item)return false;item.click();return true})()`);
}
async function inspect(label, width, height, mobile, direction) {
  await command("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile, screenWidth: width, screenHeight: height });
  await delay(180);
  const result = await evaluate(`(()=>{
    const main=document.querySelector('main');
    const visible=(el)=>!!(el.offsetWidth||el.offsetHeight||el.getClientRects().length);
    const controls=[...document.querySelectorAll('button,input:not([type="hidden"]),select,textarea,[role="button"]')].filter(visible);
    const unnamed=controls.filter((el)=>{
      const aria=el.getAttribute('aria-label')?.trim();
      const labelled=el.getAttribute('aria-labelledby')?.split(/\\s+/).some((id)=>document.getElementById(id)?.textContent.trim());
      const title=el.getAttribute('title')?.trim();
      const text=(el.innerText||el.value||'').trim();
      const labels=el.labels&&[...el.labels].some((label)=>label.textContent.trim());
      const placeholder=el.getAttribute('placeholder')?.trim();
      return !(aria||labelled||title||text||labels||placeholder);
    }).map((el)=>({tag:el.tagName.toLowerCase(),type:el.getAttribute('type')||'',role:el.getAttribute('role')||''})).slice(0,12);
    const viewport=document.documentElement.clientWidth;
    const offenders=[...document.querySelectorAll('body *')].filter(visible).map((el)=>({el,right:Math.round(el.getBoundingClientRect().right)})).filter(({el,right})=>right>viewport+1&&!el.closest('.nav')).sort((a,b)=>b.right-a.right).slice(0,4).map(({el,right})=>({tag:el.tagName.toLowerCase(),className:typeof el.className==='string'?el.className.slice(0,70):'',right}));
    return {route:${JSON.stringify(label)},direction:${JSON.stringify(direction)},width:viewport,windowInnerWidth:window.innerWidth,visualViewportWidth:window.visualViewport?.width??null,visualViewportScale:window.visualViewport?.scale??null,devicePixelRatio:window.devicePixelRatio,rootScrollWidth:document.documentElement.scrollWidth,bodyScrollWidth:document.body.scrollWidth,mainWidth:main?.clientWidth??null,h1:[...(main?.querySelectorAll('h1')??[])].map((x)=>x.textContent.trim()).filter(Boolean).slice(0,3),loading:!!main?.querySelector('.loading'),visibleControlCount:controls.length,unnamedControls:unnamed,overflowOffenders:offenders};
  })()`);
  return result;
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
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error("A Chrome DevTools command failed.")) : item.resolve(message.result);
  });
  await command("Page.enable");
  await command("Runtime.enable");
  await command("Page.navigate", { url: base.href });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "login form");
  await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const pass=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!pass||!form)return false;for(const [el,value] of [[email,${JSON.stringify(fixture.admin.email)}],[pass,${JSON.stringify(fixture.admin.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
  await waitFor("Boolean(document.querySelector('.shell'))", "authenticated Admin shell", 45_000);

  const results = [];
  for (const route of routes) {
    if (!(await clickNav(route))) throw new Error(`Admin navigation entry was unavailable: ${route}.`);
    await waitFor("Boolean(document.querySelector('main h1')||document.querySelector('main .loading'))", `${route} page`, 25_000);
    await delay(180);
    results.push(await inspect(route, 1365, 900, false, "ltr-desktop"));
    if (focusedRoutes.has(route)) {
      results.push(await inspect(route, 390, 844, true, "ltr-mobile"));
      const switched = await evaluate(`(()=>{const button=document.querySelector('button[aria-label*="Right-to-Left"]');if(!button)return false;button.click();return document.documentElement.dir==='rtl'})()`);
      if (!switched) throw new Error(`RTL toggle was not available for ${route}.`);
      results.push(await inspect(route, 390, 844, true, "rtl-mobile"));
      await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Switch to Left-to-Right layout"]');if(button)button.click();return true})()`);
      await delay(100);
    }
  }
  const summary = {
    review_url: base.href,
    synthetic_admin_login: true,
    routes_visited: routes.length,
    focused_routes: [...focusedRoutes],
    screen_matrix: [1365, 390, 390],
    read_only_navigation: true,
    results,
  };
  const outputPath = path.join(evidenceDir, "c16-admin-route-audit.json");
  writeFileSync(outputPath, `${JSON.stringify(summary, null, 2)}\n`, "utf8");
  const missingHeadings = results.filter((item) => !item.h1.length);
  const overflow = results.filter((item) => item.rootScrollWidth > item.width || item.bodyScrollWidth > item.width);
  const unlabeled = results.filter((item) => item.unnamedControls.length);
  console.log(JSON.stringify({ routesVisited: routes.length, viewportSamples: results.length, pagesWithoutHeading: missingHeadings.map((item) => item.route), overflowing: overflow.map((item) => `${item.route}/${item.direction}:${item.width}/${item.rootScrollWidth}/${item.bodyScrollWidth}`), samplesWithUnnamedControls: unlabeled.map((item) => `${item.route}/${item.direction}:${item.unnamedControls.length}`), evidence: outputPath }, null, 2));
  if (missingHeadings.length || overflow.length) process.exitCode = 1;
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (chrome && chrome.exitCode === null) {
    chrome.kill();
    await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3_000)]);
  }
  const safeTempRoot = path.resolve(tempRoot);
  const safeProfile = path.resolve(profile);
  if (safeProfile.startsWith(`${safeTempRoot}${path.sep}`) && path.basename(safeProfile).startsWith("browser-c16-routes-")) {
    rmSync(safeProfile, { recursive: true, force: true });
  }
}
