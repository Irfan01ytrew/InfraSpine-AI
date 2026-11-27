#!/usr/bin/env node
/** Upload one synthetic source through the current Admin UI session for C13 training QA. */
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = "http://127.0.0.1:5186/admin/";
const root = process.env.INFRASPINE_C13_ROOT;
const fixturePath = process.env.INFRASPINE_C13_FIXTURE;
const sourcePath = path.join(root ?? "", "tmp", "c13-recovery-policy.txt");
if (!root || !fixturePath || !existsSync(sourcePath) || !base.startsWith("http://127.0.0.1:")) {
  throw new Error("C13 upload requires its protected local fixture, source, and loopback review UI.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const admin = fixture.admin;
if (!admin?.email || !admin?.password) throw new Error("The protected C13 Admin fixture is incomplete.");
const source = readFileSync(sourcePath);
const sourceSha256 = createHash("sha256").update(source).digest("hex");
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome is unavailable for C13 source upload.");

const tmp = path.join(root, "tmp");
const evidenceDir = path.join(root, "evidence");
mkdirSync(evidenceDir, { recursive: true });
const profile = mkdtempSync(path.join(tmp, "browser-c13-upload-"));
let chrome; let socket; let sequence = 0;
const pending = new Map();
function command(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser timed out at ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 30_000) {
  const result = await command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("A C13 upload browser action failed.");
  return result.result?.value;
}
async function waitFor(expression, label, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) { if (await evaluate(expression)) return; await delay(100); }
  throw new Error(`Timed out waiting for ${label}.`);
}
try {
  chrome = spawn(browserPath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < deadline) await delay(100);
  if (!existsSync(portFile)) throw new Error("Isolated C13 Chromium did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!page?.webSocketDebuggerUrl) throw new Error("Isolated C13 Chromium did not expose its page.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data); const item = pending.get(message.id);
    if (!item) return; clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error("A Chrome DevTools command failed.")) : item.resolve(message.result);
  });
  await command("Page.enable"); await command("Runtime.enable"); await command("Page.navigate", { url: base });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "synthetic login form");
  const submitted = await evaluate(`(()=>{const e=document.querySelector('form.login-form input[name="email"]'),p=document.querySelector('form.login-form input[name="password"]'),f=document.querySelector('form.login-form');if(!e||!p||!f)return false;for(const [x,v] of [[e,${JSON.stringify(admin.email)}],[p,${JSON.stringify(admin.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(x,v);x.dispatchEvent(new Event('input',{bubbles:true}));x.dispatchEvent(new Event('change',{bubbles:true}))}f.requestSubmit();return true})()`);
  if (!submitted) throw new Error("The C13 synthetic Admin login form could not be submitted.");
  await waitFor("Boolean(document.querySelector('.shell'))", "authenticated Admin", 45_000);
  const b64 = source.toString("base64");
  const uploaded = await evaluate(`(async()=>{const raw=atob(${JSON.stringify(b64)});const bytes=Uint8Array.from(raw,c=>c.charCodeAt(0));const f=new FormData();f.append('file',new File([bytes],'c13-recovery-policy.txt',{type:'text/plain'}));f.append('visibility','shared');const r=await fetch('/api/rag/documents',{method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineAdmin'},body:f});let data=null;try{data=await r.json()}catch{}return {status:r.status,data}})()`, 45_000);
  if (uploaded.status !== 202 || !uploaded.data?.id || uploaded.data.status !== "pending") throw new Error("The current Admin API did not queue the synthetic C13 source.");
  const evidence = { result: "PASS", reviewUrl: base, apiHealthUrl: "http://127.0.0.1:63014/health/ready", documentId: uploaded.data.id, filename: uploaded.data.filename, initialStatus: uploaded.data.status, sourceSha256, sourceBytes: source.length };
  writeFileSync(path.join(evidenceDir, "c13-source-upload.json"), JSON.stringify(evidence, null, 2) + "\n", { mode: 0o600 });
  console.log("C13_CURRENT_SOURCE_SYNTHETIC_UPLOAD=PASS; API returned 202 for one shared QA source.");
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (chrome && chrome.exitCode === null) { chrome.kill(); await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3_000)]); }
  const safeTmp = path.resolve(tmp); const safeProfile = path.resolve(profile);
  if (safeProfile.startsWith(`${safeTmp}${path.sep}`) && path.basename(safeProfile).startsWith("browser-c13-upload-")) {
    await import("node:fs/promises").then(({ rm }) => rm(safeProfile, { recursive: true, force: true }));
  }
}
