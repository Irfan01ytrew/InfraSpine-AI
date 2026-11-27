#!/usr/bin/env node
/** Authenticated download acceptance for aggregate reports on an isolated synthetic restore. */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_REPORT_URL ?? "http://127.0.0.1:18107/admin/");
const fixturePath = process.env.INFRASPINE_QA_USERS_FILE;
const restoreEnvPath = process.env.INFRASPINE_REPORT_RESTORE_ENV;
let credentials;
let browser;
let cdp;
let profile;

const FORMATS = {
  csv: { mediaType: "text/csv; charset=utf-8", extension: ".csv", signature: "csv" },
  pdf: { mediaType: "application/pdf", extension: ".pdf", signature: "pdf" },
  docx: { mediaType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", extension: ".docx", signature: "zip" },
  xlsx: { mediaType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", extension: ".xlsx", signature: "zip" },
  pptx: { mediaType: "application/vnd.openxmlformats-officedocument.presentationml.presentation", extension: ".pptx", signature: "zip" },
};

if (!["http:", "https:"].includes(base.protocol) || !["localhost", "127.0.0.1", "::1"].includes(base.hostname)) {
  throw new Error("Report browser acceptance is restricted to a loopback QA candidate.");
}
if (!fixturePath || !restoreEnvPath || !existsSync(fixturePath) || !existsSync(restoreEnvPath)) {
  throw new Error("Protected synthetic role fixture and isolated restore metadata are required.");
}
const project = path.basename(path.resolve(path.dirname(restoreEnvPath)));
if (!/^infraspine-combined-restore-[a-f0-9]{12}$/i.test(project) || path.basename(restoreEnvPath) !== "restore-test.env") {
  throw new Error("Restore metadata is outside the expected isolated synthetic project.");
}
credentials = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
for (const role of ["admin", "manager", "employee"]) {
  if (typeof credentials?.[role]?.email !== "string" || typeof credentials?.[role]?.password !== "string") {
    throw new Error("Protected fixture is missing a synthetic role account.");
  }
}

function findBrowser() {
  const candidates = process.platform === "win32"
    ? [
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
        path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
      ]
    : ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/microsoft-edge"];
  const browserPath = candidates.find((candidate) => candidate && existsSync(candidate));
  if (!browserPath) throw new Error("Chrome, Chromium or Edge was not found.");
  return browserPath;
}

function connectDebugger(endpoint) {
  const socket = new WebSocket(endpoint);
  const pending = new Map();
  let sequence = 0;
  const opened = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("Browser debugger connection failed.")), { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const entry = pending.get(message.id);
    clearTimeout(entry.timer);
    pending.delete(message.id);
    if (message.error) entry.reject(new Error("Browser command failed."));
    else entry.resolve(message.result);
  });
  return {
    socket,
    ready: () => opened,
    command(method, params = {}, timeoutMs = 20_000) {
      const id = ++sequence;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(new Error(`Browser command timed out: ${method}`));
        }, timeoutMs);
        pending.set(id, { resolve, reject, timer });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
  };
}

async function evaluate(expression, timeoutMs = 20_000) {
  const response = await cdp.command("Runtime.evaluate", {
    expression, awaitPromise: true, returnByValue: true, userGesture: true,
  }, timeoutMs);
  if (response.exceptionDetails) throw new Error("Browser action raised an exception.");
  return response.result?.value;
}

async function waitFor(expression, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return true;
    await delay(250);
  }
  return false;
}

async function signIn(role) {
  const account = credentials[role];
  const submitted = await evaluate(`(()=>{
    const email=document.querySelector('input[name="email"]');
    const password=document.querySelector('input[name="password"]');
    const form=document.querySelector('form.login-form');
    if(!email||!password||!form)return false;
    const set=(input,value)=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,value);input.dispatchEvent(new Event('input',{bubbles:true}))};
    set(email,${JSON.stringify(account.email)});set(password,${JSON.stringify(account.password)});
    form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return true;
  })()`);
  if (!submitted || !await waitFor(`(async()=>{const response=await fetch('/api/auth/me',{credentials:'same-origin'});return response.ok&&(await response.json()).role===${JSON.stringify(role)}})()`)) {
    throw new Error(`Synthetic ${role} login failed.`);
  }
}

async function signOut() {
  const clicked = await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Sign out"]');if(!button)return false;button.click();return true})()`);
  if (!clicked || !await waitFor(`Boolean(document.querySelector('form.login-form'))`)) throw new Error("Browser logout failed.");
}

async function ensureLoginPage() {
  if (await waitFor(`Boolean(document.querySelector('form.login-form'))`, 5_000)) return;
  const auth = await evaluate(`(async()=>{const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),5000);try{const response=await fetch('/api/auth/me',{credentials:'same-origin',signal:controller.signal});let role=null;if(response.ok){try{role=(await response.json()).role??null}catch{}}return {status:response.status,role}}catch{return {status:0,role:null}}finally{clearTimeout(timer)}})()`);
  if (auth?.status === 200 && ["admin", "manager", "employee"].includes(auth.role)) await signOut();
  if (!await waitFor(`Boolean(document.querySelector('form.login-form'))`, 10_000)) {
    const diagnostic = await evaluate(`(()=>({path:location.pathname,title:document.title,appRoot:Boolean(document.querySelector('#root')),loginForm:Boolean(document.querySelector('form.login-form'))}))()`);
    throw new Error(`Synthetic QA login page did not render; authStatus=${auth?.status ?? 0}; appRoot=${diagnostic?.appRoot}; path=${diagnostic?.path}; title=${diagnostic?.title}.`);
  }
}

async function checkExport(role, format, expectedStatus) {
  const spec = FORMATS[format];
  const result = await evaluate(`(async()=>{
    const response=await fetch('/api/admin/reports/usage/export?days=30&format=${format}',{credentials:'same-origin'});
    const bytes=new Uint8Array(await response.arrayBuffer());
    const disposition=response.headers.get('content-disposition')||'';
    const prefix=new TextDecoder().decode(bytes.slice(0,5));
    const csvText=new TextDecoder().decode(bytes);
    return {status:response.status,type:response.headers.get('content-type'),cache:response.headers.get('cache-control'),nosniff:response.headers.get('x-content-type-options'),filename:disposition,size:bytes.length,zip:bytes.length>=4&&bytes[0]===0x50&&bytes[1]===0x4b,pdf:prefix.startsWith('%PDF-'),csv:bytes.length>=3&&bytes[0]===0xef&&bytes[1]===0xbb&&bytes[2]===0xbf&&csvText.includes('Usage report')&&csvText.includes('Source: aggregate RAG trace')};
  })()`);
  if (!result || result.status !== expectedStatus) throw new Error(`${role} ${format.toUpperCase()} export returned HTTP ${result?.status}; expected ${expectedStatus}.`);
  const signatureValid = spec.signature === "zip" ? result.zip : spec.signature === "pdf" ? result.pdf : result.csv;
  const minimumBytes = format === "csv" ? 100 : 1000;
  const filenameValid = result.filename.toLowerCase().endsWith(spec.extension + '"');
  if (role === "admin" && (result.type !== spec.mediaType
    || result.cache !== "no-store" || result.nosniff !== "nosniff" || !filenameValid
    || result.size < minimumBytes || !signatureValid)) {
    throw new Error(`Admin ${format.toUpperCase()} export invalid: type=${result.type}; noStore=${result.cache === "no-store"}; nosniff=${result.nosniff === "nosniff"}; filename=${filenameValid}; bytes=${result.size}; content=${signatureValid}.`);
  }
  console.log(`REPORT_${format.toUpperCase()}_${role.toUpperCase()}_HTTP_${result.status}=PASS`);
}

async function openReportsPage() {
  const opened = await evaluate(`(()=>{const button=[...document.querySelectorAll('button')].find(item=>item.textContent.trim()==='Usage reports');if(!button)return false;button.click();return true})()`);
  if (!opened || !await waitFor(`(()=>{const title=[...document.querySelectorAll('h1')].some(item=>item.textContent.trim()==='Usage reports');const formats=['CSV','PDF','Word','Excel','PowerPoint'].every(label=>Boolean(document.querySelector('button[aria-label="Export report as '+label+'"]')));return title&&formats})()`)) {
    throw new Error("Admin Usage reports page or one of its export controls did not render.");
  }
  const ready = await waitFor(`(()=>{const buttons=[...document.querySelectorAll('button[aria-label^="Export report as "]')];return buttons.length===5&&buttons.every(button=>!button.disabled)&&Boolean(document.querySelector('.report-metrics'))&&!Boolean(document.querySelector('.loading'))})()`, 30_000);
  if (!ready) {
    const diagnostic = await evaluate(`(async()=>{const started=performance.now();try{const response=await fetch('/api/admin/reports/usage?days=30',{credentials:'same-origin'});const body=await response.arrayBuffer();return {status:response.status,type:response.headers.get('content-type'),bytes:body.byteLength,elapsedMs:Math.round(performance.now()-started)}}catch{return {status:0,type:'unavailable',bytes:0,elapsedMs:Math.round(performance.now()-started)}}})()`);
    throw new Error(`Admin usage report did not finish loading; API status=${diagnostic?.status}; type=${diagnostic?.type}; bytes=${diagnostic?.bytes}; elapsedMs=${diagnostic?.elapsedMs}.`);
  }
  console.log("REPORT_UI_ALL_EXPORT_CONTROLS=PASS");
}

async function clickUiExport(format) {
  const label = ({ csv: "CSV", pdf: "PDF", docx: "Word", xlsx: "Excel", pptx: "PowerPoint" })[format];
  const spec = FORMATS[format];
  const armed = await evaluate(`(()=>{
    const button=document.querySelector('button[aria-label="Export report as ${label}"]');
    if(!button||button.disabled)return false;
    window.__infraspineReportCapture=null;
    window.__infraspineOriginalCreateObjectURL=URL.createObjectURL;
    window.__infraspineOriginalAnchorClick=HTMLAnchorElement.prototype.click;
    URL.createObjectURL=function(blob){const url=window.__infraspineOriginalCreateObjectURL.call(URL,blob);window.__infraspineReportCapture={url,type:blob.type,size:blob.size,filename:null};return url};
    HTMLAnchorElement.prototype.click=function(){if(this.download){window.__infraspineReportCapture={...window.__infraspineReportCapture,filename:this.download};return}return window.__infraspineOriginalAnchorClick.call(this)};
    button.click();return true;
  })()`);
  if (!armed || !await waitFor(`Boolean(window.__infraspineReportCapture&&window.__infraspineReportCapture.filename)`, 20_000)) {
    await evaluate(`(()=>{if(window.__infraspineOriginalCreateObjectURL)URL.createObjectURL=window.__infraspineOriginalCreateObjectURL;if(window.__infraspineOriginalAnchorClick)HTMLAnchorElement.prototype.click=window.__infraspineOriginalAnchorClick;delete window.__infraspineOriginalCreateObjectURL;delete window.__infraspineOriginalAnchorClick})()`);
    throw new Error(`Admin ${format.toUpperCase()} button did not initiate a file response.`);
  }
  const result = await evaluate(`(()=>{const value=window.__infraspineReportCapture;return {type:value.type,size:value.size,filename:value.filename}})()`);
  await evaluate(`(()=>{if(window.__infraspineOriginalCreateObjectURL)URL.createObjectURL=window.__infraspineOriginalCreateObjectURL;if(window.__infraspineOriginalAnchorClick)HTMLAnchorElement.prototype.click=window.__infraspineOriginalAnchorClick;delete window.__infraspineOriginalCreateObjectURL;delete window.__infraspineOriginalAnchorClick;delete window.__infraspineReportCapture})()`);
  if (!result || result.type !== spec.mediaType || result.size < (format === "csv" ? 100 : 1000)
    || !result.filename.toLowerCase().endsWith(spec.extension)) {
    throw new Error(`Admin ${format.toUpperCase()} button returned invalid file metadata: type=${result?.type}; bytes=${result?.size}; filename=${result?.filename?.endsWith(spec.extension) === true}.`);
  }
  console.log(`REPORT_UI_${format.toUpperCase()}_BUTTON=PASS`);
}

function removeBrowserProfile(directory) {
  if (!directory) return;
  const root = path.resolve(tmpdir());
  const target = path.resolve(directory);
  if (path.dirname(target) !== root || !path.basename(target).startsWith("infraspine-report-browser-")) {
    throw new Error("Refusing to remove a browser profile outside its generated temporary directory.");
  }
  rmSync(target, { recursive: true, force: true });
}

async function run() {
  profile = mkdtempSync(path.join(tmpdir(), "infraspine-report-browser-"));
  browser = spawn(findBrowser(), [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-background-networking", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${profile}`, base.href,
  ], { stdio: "ignore", windowsHide: true });
  try {
    const activePortFile = path.join(profile, "DevToolsActivePort");
    const deadline = Date.now() + 30_000;
    while (!existsSync(activePortFile) && Date.now() < deadline && browser.exitCode === null) await delay(100);
    if (!existsSync(activePortFile)) throw new Error("Browser debugger did not start.");
    const port = Number(readFileSync(activePortFile, "utf8").split(/\r?\n/)[0]);
    if (!Number.isInteger(port) || port < 1 || port > 65_535) throw new Error("Browser debugger port was invalid.");
    let target;
    while (!target && Date.now() < deadline) {
      try {
        const response = await fetch(`http://127.0.0.1:${port}/json/list`);
        if (response.ok) target = (await response.json()).find((item) => item.type === "page" && item.webSocketDebuggerUrl);
      } catch {}
      if (!target) await delay(150);
    }
    if (!target) throw new Error("Browser page target did not become ready.");
    cdp = connectDebugger(target.webSocketDebuggerUrl);
    await cdp.ready();
    await cdp.command("Page.enable");
    await cdp.command("Runtime.enable");
    await ensureLoginPage();

    await signIn("admin");
    await openReportsPage();
    for (const format of Object.keys(FORMATS)) {
      await checkExport("admin", format, 200);
      await clickUiExport(format);
    }
    await signOut();
    await signIn("manager");
    for (const format of Object.keys(FORMATS)) await checkExport("manager", format, 403);
    await signOut();
    await signIn("employee");
    for (const format of Object.keys(FORMATS)) await checkExport("employee", format, 403);
    console.log("REPORT_FORMAT_AND_ROLE_ACCEPTANCE=PASS");
  } finally {
    if (cdp) cdp.socket.close();
    if (browser.exitCode === null) {
      const exited = new Promise((resolve) => browser.once("exit", resolve));
      browser.kill();
      await Promise.race([exited, delay(5_000)]);
    }
    removeBrowserProfile(profile);
  }
}

run().catch((error) => {
  console.error(`REPORT_FORMAT_AND_ROLE_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
});
