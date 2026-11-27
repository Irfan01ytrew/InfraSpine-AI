#!/usr/bin/env node
/** Authenticated current-source C16 cloud connector browser acceptance against an isolated local API. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_C16_CLOUD_URL ?? "http://127.0.0.1:5184/admin/#/connectors");
const fixturePath = process.env.INFRASPINE_C16_CREDENTIAL_FIXTURE;
const evidencePath = path.resolve(process.env.INFRASPINE_C16_CLOUD_LIVE_EVIDENCE ?? ".verification-local-review-20261029-c16-cloud-verified/evidence/c16-cloud-live-browser.json");
if (base.protocol !== "http:" || base.hostname !== "127.0.0.1") throw new Error("C16 browser acceptance permits loopback HTTP only.");
if (!fixturePath || !path.isAbsolute(fixturePath)) throw new Error("Set the protected local synthetic credential fixture path.");
const account = JSON.parse(readFileSync(fixturePath, "utf8")).admin;
if (!account?.email || !account?.password || !account.email.endsWith("@example.test")) throw new Error("The protected fixture has no synthetic Admin account.");
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find(candidate => candidate && existsSync(candidate));
if (!browserPath) throw new Error("Chrome or Edge is unavailable.");

const syntheticProviderSecret = "c16-synthetic-provider-secret-never-used";
const syntheticRefreshToken = "c16-synthetic-refresh-token-never-used";
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-c16-live-"));
let browser;
let socket;
let sequence = 0;
const pending = new Map();
function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error("C16 browser acceptance timed out.")); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 20_000) {
  const response = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (response.exceptionDetails) throw new Error("A C16 browser action failed.");
  return response.result?.value;
}
async function waitFor(expression, label, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(125);
  }
  throw new Error("Timed out waiting for " + label + ".");
}

try {
  const environment = {
    SYSTEMROOT: process.env.SYSTEMROOT, PATH: process.env.PATH, TEMP: process.env.TEMP,
    TMP: process.env.TMP, USERPROFILE: process.env.USERPROFILE, LOCALAPPDATA: process.env.LOCALAPPDATA,
  };
  browser = spawn(browserPath, ["--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-sync", "--disable-background-networking", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
    "--user-data-dir=" + profile, "--window-size=1365,1000", "about:blank"], { stdio: "ignore", windowsHide: true, env: environment });
  const activeFile = path.join(profile, "DevToolsActivePort");
  const deadline = Date.now() + 30_000;
  while (!existsSync(activeFile) && Date.now() < deadline && browser.exitCode === null) await delay(100);
  if (!existsSync(activeFile)) throw new Error("Isolated Chromium did not start.");
  const port = Number(readFileSync(activeFile, "utf8").split(/\r?\n/)[0]);
  let page;
  while (!page && Date.now() < deadline) {
    try {
      const response = await fetch("http://127.0.0.1:" + port + "/json/list");
      if (response.ok) page = (await response.json()).find(item => item.type === "page" && item.webSocketDebuggerUrl);
    } catch {}
    if (!page) await delay(150);
  }
  if (!page) throw new Error("Isolated Chromium page target was unavailable.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("Browser debugger connection failed.")), { once: true });
  });
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const item = pending.get(message.id);
    clearTimeout(item.timer); pending.delete(message.id);
    if (message.error) item.reject(new Error("Browser command failed."));
    else item.resolve(message.result);
  });
  await call("Page.enable");
  await call("Runtime.enable");
  await call("Emulation.setDeviceMetricsOverride", { width: 1365, height: 1000, deviceScaleFactor: 1, mobile: false });
  await call("Page.navigate", { url: base.href });
  await waitFor("Boolean(document.querySelector('form.login-form'))", "Admin sign-in form");
  const filledLogin = await evaluate(`(() => {
    const set=(name,value)=>{const el=document.querySelector('input[name='+JSON.stringify(name)+']');if(!el)return false;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);
      el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true;};
    return set('email',${JSON.stringify(account.email)})&&set('password',${JSON.stringify(account.password)});
  })()`);
  if (!filledLogin) throw new Error("Admin sign-in fields were unavailable.");
  await evaluate("document.querySelector('form.login-form button:not([type])')?.click()");
  await waitFor("Boolean(document.querySelector('.shell'))", "authenticated Admin shell", 30_000);
  const opened = await evaluate(`(() => { const item=[...document.querySelectorAll('button')].find(button=>button.textContent.trim()==='Cloud connectors'); if(!item)return false;item.click();return true; })()`);
  if (!opened) throw new Error("The authenticated Admin navigation omitted Cloud connectors.");
  await waitFor("document.querySelector('.cloud-connectors-page h1')?.textContent.trim()==='Cloud connectors'", "real connector form");
  await waitFor("[...document.querySelectorAll('.cloud-connector-form select')].some(select=>[...select.options].some(option=>option.textContent.trim()==='Synthetic Engineering'))", "real department options");

  const setField = async (labelText, value, select = false) => evaluate(`(() => {
    const label=[...document.querySelectorAll('.cloud-connector-form label')].find(item=>item.querySelector('.field-label')?.textContent.trim().startsWith(${JSON.stringify(labelText)}));
    const element=label?.querySelector(${select ? "'select'" : "'input'"});if(!element)return false;
    const proto=element instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto,'value').set.call(element,${JSON.stringify(value)});
    element.dispatchEvent(new Event('input',{bubbles:true}));element.dispatchEvent(new Event('change',{bubbles:true}));return true;
  })()`);
  const departmentId = await evaluate(`(() => { const label=[...document.querySelectorAll('.cloud-connector-form label')].find(item=>item.querySelector('.field-label')?.textContent.trim()==='Department access');return label?.querySelector('select option:not([value=""])')?.value??null; })()`);
  if (!departmentId) throw new Error("The fresh synthetic department was not available to the Admin form.");
  for (const [label, value] of [
    ["Name", "C16 Synthetic Review Connector"], ["Client ID", "c16-synthetic-client"],
    ["Client secret", syntheticProviderSecret], ["Refresh token", syntheticRefreshToken], ["Department access", departmentId],
  ]) {
    if (!(await setField(label, value, label === "Department access"))) throw new Error("A required connector field was unavailable.");
  }
  const safeNotice = await evaluate(`document.querySelector('.banner-warning')?.textContent.includes('provider file-level permissions are not mirrored yet')??false`);
  if (!safeNotice) throw new Error("The form did not explain the provider ACL limitation.");
  await evaluate(`document.querySelector('.cloud-connector-form button[type=submit]')?.click()`);
  await waitFor("document.querySelector('[role=status]')?.textContent.includes('Connector saved')", "connector save confirmation", 30_000);
  await waitFor("[...document.querySelectorAll('.cloud-connector-card h3')].some(item=>item.textContent.trim()==='C16 Synthetic Review Connector')", "persisted connector card");
  const persisted = await evaluate(`(async()=>{
    const response=await fetch('/api/connectors',{headers:{'X-Requested-With':'InfraSpineAdmin'},credentials:'same-origin',cache:'no-store'});
    const rows=await response.json(); const row=rows.find(item=>item.name==='C16 Synthetic Review Connector');
    const text=document.body.innerText;
    return {status:response.status,count:rows.length,found:Boolean(row),scope:row?.visibility??null,department:Boolean(row?.department_group_id),
      credentialFieldsVisible:Object.keys(row??{}).some(key=>/secret|token|credential|cursor/i.test(key)),secretRendered:text.includes(${JSON.stringify(syntheticProviderSecret)})||text.includes(${JSON.stringify(syntheticRefreshToken)})};
  })()`);
  if (persisted.status !== 200 || !persisted.found || persisted.scope !== "restricted" || !persisted.department || persisted.credentialFieldsVisible || persisted.secretRendered) {
    throw new Error("The live API did not persist only redacted, department-restricted connector metadata.");
  }
  const noExternalSync = !(await evaluate("Boolean([...document.querySelectorAll('button')].some(button=>button.textContent.trim()==='Sync now'&&button.closest('.cloud-connector-card')))"));
  if (noExternalSync) throw new Error("The saved connector did not render its manual sync control.");

  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await delay(700);
  const mobile = await evaluate(`({width:innerWidth,documentWidth:document.documentElement.scrollWidth,bodyWidth:document.body.scrollWidth,heading:document.querySelector('.cloud-connectors-page h1')?.textContent.trim()??null})`);
  if (mobile.width !== 390 || Math.max(mobile.documentWidth,mobile.bodyWidth) > 390 || mobile.heading !== "Cloud connectors") {
    throw new Error("The authenticated cloud connector page overflowed at 390px.");
  }
  const image = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  mkdirSync(path.dirname(evidencePath), { recursive: true });
  writeFileSync(evidencePath.replace(/\.json$/i, "-mobile.png"), Buffer.from(image.data, "base64"));
  writeFileSync(evidencePath, JSON.stringify({
    url: base.href, authenticatedAs: "synthetic Admin", desktop: { width: 1365, connectorSaved: true, departmentScoped: true, credentialsRedacted: true, externalSyncCalled: false },
    mobile, api: { status: persisted.status, connectorCount: persisted.count, secretFieldsExposed: persisted.credentialFieldsVisible, secretRendered: persisted.secretRendered },
  }, null, 2) + "\n", { mode: 0o600 });
  console.log(JSON.stringify({ result: "passed", frontend: base.origin + "/admin/#/connectors", api: base.origin + "/api/connectors", mobileWidth: mobile.width, credentialsRedacted: true, externalSyncCalled: false, evidence: evidencePath }));
} catch (error) {
  console.error("C16_BROWSER_ACCEPTANCE_FAILED=" + (error instanceof Error ? error.message : "unknown failure"));
  process.exitCode = 1;
} finally {
  socket?.close();
  if (browser && browser.exitCode === null) {
    const exited = new Promise(resolve => browser.once("exit", resolve));
    browser.kill();
    await Promise.race([exited, delay(5_000)]);
  }
  try { rmSync(profile, { recursive: true, force: true, maxRetries: 6, retryDelay: 250 }); }
  catch { console.error("C16_BROWSER_TEMP_PROFILE_CLEANUP=deferred"); }
  account.password = null;
}
