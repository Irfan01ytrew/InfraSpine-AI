#!/usr/bin/env node
/** Read-only 390px layout diagnosis for the isolated C04 review app. */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const fixturePath = process.env.INFRASPINE_C04_REVIEW_FIXTURE;
const evidencePath = process.env.INFRASPINE_C04_REVIEW_EVIDENCE;
const base = process.env.INFRASPINE_C04_REVIEW_URL ?? "http://127.0.0.1:5183/admin/";
if (!fixturePath || !evidencePath || !existsSync(fixturePath)) throw new Error("Protected C04 browser fixture is unavailable.");
const { admin } = JSON.parse(readFileSync(fixturePath, "utf8"));
const chromePath = process.env.INFRASPINE_BROWSER_PATH ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
if (!admin?.email || !admin?.password || !existsSync(chromePath)) throw new Error("C04 Admin fixture or local Chrome is unavailable.");

let chrome;
let socket;
let profile;
let sequence = 0;
const pending = new Map();

function cdp(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Chrome command timed out: ${method}`)); }, 20_000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await cdp("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error("Mobile diagnostic browser action failed.");
  return result.result?.value;
}

async function waitFor(expression, label) {
  const deadline = Date.now() + 25_000;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(150);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}

async function fill(selector, value) {
  const done = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value')?.set;if(!setter)return false;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
  if (!done) throw new Error("C04 Admin sign-in form is unavailable.");
}

try {
  profile = mkdtempSync(path.join(tmpdir(), "infraspine-c04-mobile-") );
  chrome = spawn(chromePath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const activeFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 25_000;
  while (!existsSync(activeFile) && Date.now() < deadline) await delay(100);
  if (!existsSync(activeFile)) throw new Error("Isolated Chrome did not start.");
  const port = readFileSync(activeFile, "utf8").split(/\r?\n/)[0];
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((target) => target.type === "page");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer);
    pending.delete(message.id);
    message.error ? item.reject(new Error("Chrome DevTools command failed.")) : item.resolve(message.result);
  });
  await cdp("Page.enable");
  await cdp("Runtime.enable");
  await cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await cdp("Page.navigate", { url: base });
  await waitFor(`Boolean(document.querySelector('form.login-form input[name="email"]'))`, "synthetic Admin sign-in");
  await fill('form.login-form input[name="email"]', admin.email);
  await fill('form.login-form input[name="password"]', admin.password);
  if (!await evaluate(`(()=>{const form=document.querySelector('form.login-form');if(!form)return false;form.requestSubmit();return true;})()`)) throw new Error("C04 sign-in submit was unavailable.");
  await waitFor(`Boolean(document.querySelector('.shell'))`, "authenticated C04 workspace");
  await evaluate(`window.location.hash='#/people'`);
  await waitFor(`document.querySelector('h1')?.innerText==='People & access'`, "People page");
  await waitFor(`(()=>{const section=document.querySelector('section[aria-labelledby="role-template-heading"]');return section?.innerText.includes('data.steward')&&section.innerText.includes('C04 Stewardship Minimum Scope Corrected')})()`, "loaded steward template and catalog");
  await delay(200);
  const metrics = await evaluate(`(()=>{const nodes=[...document.querySelectorAll('body *')];const offenders=nodes.map(el=>{const r=el.getBoundingClientRect();return{tag:el.tagName.toLowerCase(),id:el.id,cls:typeof el.className==='string'?el.className:'',text:(el.innerText||'').trim().replace(/\\s+/g,' ').slice(0,70),left:Math.round(r.left),right:Math.round(r.right),width:Math.round(r.width),scroll:el.scrollWidth}}).filter(x=>x.width>0&&(x.right>innerWidth+1||x.left < -1)).sort((a,b)=>b.right-a.right).slice(0,24);return{viewport:innerWidth,visualViewport:visualViewport?.width,clientWidth:document.documentElement.clientWidth,documentWidth:document.documentElement.scrollWidth,bodyWidth:document.body.scrollWidth,devicePixelRatio,zoom:visualViewport?.scale,metaViewport:document.querySelector('meta[name=viewport]')?.content,offenders}})()`);
  console.log(JSON.stringify(metrics));
  const image = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  writeFileSync(evidencePath, Buffer.from(image.data, "base64"), { flag: "wx" });
} catch (error) {
  console.error(`C04_MOBILE_DIAGNOSTIC=FAIL ${error?.message ?? "unknown"}`);
  process.exitCode = 1;
} finally {
  try { socket?.close(); } catch {}
  if (chrome && chrome.exitCode === null) { chrome.kill(); await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3000)]); }
  if (profile && path.basename(profile).startsWith("infraspine-c04-mobile-")) rmSync(profile, { recursive: true, force: true });
  admin.password = "";
}
