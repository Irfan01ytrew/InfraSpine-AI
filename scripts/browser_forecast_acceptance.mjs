#!/usr/bin/env node
/** Real-browser forecast UI/API acceptance against one isolated loopback restore candidate. */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_F09_URL ?? "http://127.0.0.1:18106/");
const fixturePath = process.env.INFRASPINE_QA_USERS_FILE;
const restoreEnvPath = process.env.INFRASPINE_F09_RESTORE_ENV;
const dockerPath = process.env.INFRASPINE_DOCKER_PATH ?? "docker.exe";
const roles = ["admin", "manager", "employee"];
const evidence = [];
let credentials;
let cdp;
let browser;
let profile;

if (!['http:', 'https:'].includes(base.protocol) || !['localhost', '127.0.0.1', '::1'].includes(base.hostname)) {
  throw new Error("Forecast browser acceptance is restricted to a loopback QA candidate.");
}
if (!fixturePath || !restoreEnvPath || !existsSync(fixturePath) || !existsSync(restoreEnvPath)) {
  throw new Error("Protected synthetic QA credentials and restored-candidate metadata are required.");
}
const restoreRoot = path.resolve(path.dirname(restoreEnvPath));
const project = path.basename(restoreRoot);
if (!/^infraspine-combined-restore-[a-f0-9]{12}$/i.test(project) || path.basename(restoreEnvPath) !== "restore-test.env") {
  throw new Error("Restore metadata is outside the expected isolated synthetic project.");
}
const restoreValues = Object.fromEntries(readFileSync(restoreEnvPath, "utf8").split(/\r?\n/).flatMap((row) => {
  const separator = row.indexOf("=");
  return separator > 0 ? [[row.slice(0, separator), row.slice(separator + 1)]] : [];
}));
const dbName = restoreValues.POSTGRES_DB;
const dbUser = restoreValues.POSTGRES_USER;
if (!/^restore_[a-f0-9]{12}$/i.test(dbName ?? "") || !/^qa_[a-f0-9]{12}$/i.test(dbUser ?? "")) {
  throw new Error("Restored synthetic database identity is invalid.");
}
credentials = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
for (const role of roles) {
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
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,
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

async function request(method, route, body) {
  const result = await evaluate(`(async()=>{
    const init={method:${JSON.stringify(method)},credentials:'same-origin',headers:{'X-Requested-With':'XMLHttpRequest'}};
    const payload=${body === undefined ? "undefined" : JSON.stringify(body)};
    if(payload!==undefined){init.headers['Content-Type']='application/json';init.body=JSON.stringify(payload)}
    const response=await fetch(${JSON.stringify(route)},init);let json=null;
    try{json=await response.json()}catch{}
    return {status:response.status,json};
  })()`);
  if (!result || typeof result.status !== "number") throw new Error("Browser API request returned no status.");
  return result;
}

async function expect(method, route, status, body) {
  const result = await request(method, route, body);
  if (result.status !== status) throw new Error(`${method} ${route} returned HTTP ${result.status}; expected ${status}.`);
  return result.json;
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
  if (!submitted || !await waitFor(`(async()=>{
    const response=await fetch('/api/auth/me',{credentials:'same-origin'});
    if(!response.ok)return false;return (await response.json()).role===${JSON.stringify(role)};
  })()`)) throw new Error(`Synthetic ${role} login failed.`);
  if (!await waitFor(`Boolean(document.querySelector('button.nav-item'))`)) throw new Error(`${role} application navigation did not render.`);
}

async function signOut() {
  const clicked = await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Sign out"]');if(!button)return false;button.click();return true})()`);
  if (!clicked || !await waitFor(`Boolean(document.querySelector('form.login-form'))`)) throw new Error("Browser logout failed.");
}

async function navigateForecasts() {
  const clicked = await evaluate(`(()=>{
    const button=[...document.querySelectorAll('button.nav-item')].find((item)=>item.textContent.trim()==='What-if forecasts');
    if(!button)return false;button.click();return true;
  })()`);
  if (!clicked || !await waitFor(`Boolean([...document.querySelectorAll('h1')].some((item)=>item.textContent.trim()==='What-if forecasts'))`)) {
    throw new Error("The What-if forecasts page did not open in the browser.");
  }
}

async function waitForDocument(documentId) {
  const deadline = Date.now() + 180_000;
  while (Date.now() < deadline) {
    const document = await expect("GET", `/api/rag/documents/${documentId}`, 200);
    if (document.status === "ready" && document.searchable) return document;
    if (document.status === "failed") throw new Error("Synthetic forecast source processing failed.");
    await delay(1000);
  }
  throw new Error("Worker did not finish indexing the synthetic forecast source.");
}

function sourceChunk(documentId) {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(documentId)) {
    throw new Error("Synthetic source ID is invalid.");
  }
  const sql = `SELECT content FROM document_chunks WHERE document_id='${documentId}' ORDER BY chunk_index LIMIT 1;`;
  const result = spawnSync(dockerPath, [
    "--context", "desktop-linux", "exec", `${project}-postgres-1`, "psql", "-U", dbUser, "-d", dbName, "-Atc", sql,
  ], { encoding: "utf8", windowsHide: true });
  const content = result.stdout?.trim() ?? "";
  if (result.status !== 0 || !content.includes("100.00") || !content.includes("150.00") || content.length > 200_000) {
    throw new Error("Synthetic source did not produce the expected bounded indexed passage.");
  }
  return content;
}

function forecastPdf(content) {
  const escaped = content.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)");
  const stream = Buffer.from(`BT /F1 12 Tf 50 780 Td (${escaped}) Tj ET`, "latin1");
  const objects = [
    Buffer.from("<< /Type /Catalog /Pages 2 0 R >>", "latin1"),
    Buffer.from("<< /Type /Pages /Kids [3 0 R] /Count 1 >>", "latin1"),
    Buffer.from("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>", "latin1"),
    Buffer.from("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", "latin1"),
    Buffer.concat([Buffer.from(`<< /Length ${stream.length} >>\nstream\n`, "latin1"), stream, Buffer.from("\nendstream", "latin1")]),
  ];
  const parts = [Buffer.from("%PDF-1.4\n", "latin1")];
  const offsets = [0];
  let length = parts[0].length;
  objects.forEach((object, index) => {
    offsets.push(length);
    const prefix = Buffer.from(`${index + 1} 0 obj\n`, "latin1");
    const suffix = Buffer.from("\nendobj\n", "latin1");
    parts.push(prefix, object, suffix);
    length += prefix.length + object.length + suffix.length;
  });
  const xrefOffset = length;
  const xref = [`xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`, ...offsets.slice(1).map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`)].join("");
  const trailer = `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`;
  parts.push(Buffer.from(xref + trailer, "latin1"));
  return Buffer.concat(parts).toString("base64");
}

async function uploadSource() {
  const text = `Synthetic browser forecast source ${Date.now()}. 2023 revenue was 100.00 USD. 2024 revenue was 150.00 USD.`;
  const base64 = forecastPdf(text);
  const result = await evaluate(`(async()=>{
    const binary=atob(${JSON.stringify(base64)});const bytes=Uint8Array.from(binary,(character)=>character.charCodeAt(0));
    const file=new File([bytes],${JSON.stringify(`f09-browser-${Date.now()}.pdf`)},{type:'application/pdf'});
    const form=new FormData();form.set('file',file);form.set('visibility','shared');
    const response=await fetch('/api/rag/documents',{method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'XMLHttpRequest'},body:form});
    let json=null;try{json=await response.json()}catch{}return {status:response.status,json};
  })()`, 30_000);
  if (result?.status !== 202 || !result.json?.id) throw new Error(`Synthetic source upload returned HTTP ${result?.status ?? "unknown"}.`);
  return result.json;
}

async function setControl(selector, value, kind = "input") {
  const expression = `(()=>{
    const element=document.querySelector(${JSON.stringify(selector)});if(!element)return false;
    const proto=element instanceof HTMLSelectElement?HTMLSelectElement.prototype:element instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto,'value').set.call(element,${JSON.stringify(String(value))});
    element.dispatchEvent(new Event(${JSON.stringify(kind)},{bubbles:true}));
    return true;
  })()`;
  if (!await evaluate(expression)) throw new Error("A visible forecast form control was unavailable.");
}

async function createThroughUi(role, title, source, excerpt) {
  await navigateForecasts();
  if (!await waitFor(`Boolean(document.querySelector('.forecast-observation select option[value="${source.id}"]'))`)) {
    throw new Error(`${role} could not select the readable synthetic forecast source.`);
  }
  const fields = [
    [".forecast-fields label:nth-of-type(1) input", title, "input"],
    [".forecast-fields label:nth-of-type(2) input", "Revenue", "input"],
    [".forecast-fields label:nth-of-type(3) input", "USD", "input"],
    [".forecast-fields label:nth-of-type(4) select", "year", "change"],
    [".forecast-fields label:nth-of-type(5) input", "10", "input"],
    [".forecast-fields label:nth-of-type(6) input", "2", "input"],
    [".forecast-fields label:nth-of-type(7) input", "2", "input"],
    [".forecast-fields label:nth-of-type(8) textarea", "Synthetic QA assumption; not a statistical confidence interval.", "input"],
  ];
  for (const [selector, value, event] of fields) await setControl(selector, value, event);
  for (let index = 0; index < 2; index += 1) {
    const root = `.forecast-observation:nth-of-type(${index + 1})`;
    const observation = index === 0 ? ["2023", "100"] : ["2024", "150"];
    await setControl(`${root} label:nth-of-type(1) input`, observation[0]);
    await setControl(`${root} label:nth-of-type(2) input`, observation[1]);
    await setControl(`${root} label:nth-of-type(3) select`, source.id, "change");
    await setControl(`${root} label:nth-of-type(4) textarea`, excerpt);
  }
  const ready = await evaluate(`(()=>{const form=document.querySelector('.forecast-form');const button=form?.querySelector('button[type="submit"]');return {button:!!button,disabled:button?.disabled??true,count:form?.querySelectorAll('.forecast-observation').length??0}})()`);
  if (!ready?.button || ready.disabled || ready.count !== 2) throw new Error(`${role} forecast form did not become submittable.`);
  const clicked = await evaluate(`(()=>{const button=document.querySelector('.forecast-form button[type="submit"]');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!clicked || !await waitFor(`document.body.innerText.includes('Immutable scenario saved')&&document.body.innerText.includes(${JSON.stringify(title)})`, 30_000)) {
    throw new Error(`${role} could not save a forecast through the visible form.`);
  }
  const rows = await expect("GET", "/api/forecasts", 200);
  const created = rows.items.find((item) => item.title === title);
  if (!created || rows.items.length !== 1) throw new Error(`${role} scenario was not private to its authenticated owner.`);
  const detail = await expect("GET", `/api/forecasts/${created.id}`, 200);
  if (detail.model_version !== "linear-growth-v1" || detail.projections?.[0]?.value !== "167.000000" ||
      detail.observations?.length !== 2 || detail.observations.some((item) => !item.citation?.excerpt.includes("revenue"))) {
    throw new Error(`${role} saved scenario did not retain its reproducible calculation and source evidence.`);
  }
  if (!await waitFor(`Boolean(document.querySelector('.forecast-citations a[href*="/pages/"]'))`)) {
    throw new Error(`${role} forecast citation did not expose its source-preview link.`);
  }
  const previewHref = await evaluate(`document.querySelector('.forecast-citations a[href*="/pages/"]')?.getAttribute('href')??''`);
  const preview = await evaluate(`(async()=>{const response=await fetch(${JSON.stringify(previewHref)},{credentials:'same-origin'});return {status:response.status,type:response.headers.get('content-type')}})()`);
  if (preview?.status !== 200 || !preview.type?.startsWith("image/png")) throw new Error(`${role} cited source preview returned HTTP ${preview?.status ?? "unknown"}.`);
  evidence.push(`F09_${role.toUpperCase()}_UI_API_CITATION_PREVIEW=PASS`);
  return created.id;
}

async function run() {
  profile = mkdtempSync(path.join(tmpdir(), "infraspine-f09-browser-"));
  const activePortFile = path.join(profile, "DevToolsActivePort");
  browser = spawn(findBrowser(), [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-background-networking", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${profile}`, base.href,
  ], { stdio: "ignore", windowsHide: true });
  try {
    const deadline = Date.now() + 30_000;
    while (!existsSync(activePortFile) && Date.now() < deadline && browser.exitCode === null) await delay(100);
    if (!existsSync(activePortFile)) throw new Error("Browser debugger did not start.");
    let port = 0;
    while (!port && Date.now() < deadline) {
      try { port = Number(readFileSync(activePortFile, "utf8").split(/\r?\n/)[0]); } catch {}
      if (!Number.isInteger(port) || port < 1 || port > 65_535) { port = 0; await delay(100); }
    }
    if (!port) throw new Error("Browser debugger port was invalid.");
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
    if (!await waitFor(`Boolean(document.querySelector('form.login-form'))`)) throw new Error("Synthetic candidate login page did not render.");

    await signIn("admin");
    evidence.push("F09_ADMIN_LOGIN=PASS");
    const source = await uploadSource();
    const ready = await waitForDocument(source.id);
    if (ready.visibility !== "shared" || !ready.searchable) throw new Error("Synthetic PDF did not become a searchable shared source.");
    const content = sourceChunk(source.id);
    const start = content.indexOf("2023 revenue was 100.00 USD");
    const second = content.indexOf("2024 revenue was 150.00 USD", start + 1);
    const end = second < 0 ? -1 : second + "2024 revenue was 150.00 USD".length;
    const excerpt = start < 0 || end < 0 ? "" : content.slice(start, end).trim();
    if (!excerpt || excerpt.length > 800) throw new Error("Indexed synthetic source did not contain a bounded verbatim forecast passage.");
    evidence.push("F09_ADMIN_UPLOAD_AND_WORKER_INDEX=PASS");

    const adminScenarioId = await createThroughUi("Admin", `QA Admin forecast ${Date.now()}`, source, excerpt);
    await signOut();
    await signIn("manager");
    const managerRows = await expect("GET", "/api/forecasts", 200);
    if (managerRows.items.length !== 0 || (await request("GET", `/api/forecasts/${adminScenarioId}`)).status !== 404) {
      throw new Error("Manager could read the Admin-owned private forecast.");
    }
    evidence.push("F09_MANAGER_OWNER_BOUNDARY=PASS");
    await createThroughUi("Manager", `QA Manager forecast ${Date.now()}`, source, excerpt);
    await signOut();
    await signIn("employee");
    const employeeRows = await expect("GET", "/api/forecasts", 200);
    if (employeeRows.items.length !== 0) throw new Error("Employee could read another account's forecast scenarios.");
    await createThroughUi("Employee", `QA Employee forecast ${Date.now()}`, source, excerpt);
    evidence.push("F09_EMPLOYEE_OWNER_BOUNDARY=PASS");
    await signOut();
    for (const item of evidence) console.log(item);
    console.log("F09_BROWSER_ACCEPTANCE=PASS");
  } finally {
    cdp?.socket.close();
    if (browser?.exitCode === null) {
      browser.kill();
      await Promise.race([new Promise((resolve) => browser.once("exit", resolve)), delay(3_000)]);
    }
    if (profile && path.basename(profile).startsWith("infraspine-f09-browser-")) rmSync(profile, { recursive: true, force: true });
  }
}

try {
  await run();
} catch (error) {
  for (const item of evidence) console.log(item);
  console.error(`F09_BROWSER_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
}
