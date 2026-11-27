import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.argv[2] ?? "http://127.0.0.1:5182/admin/");
const apiBase = new URL(process.argv[3] ?? "http://127.0.0.1:63011/");
const fixturePath = path.resolve(process.env.INFRASPINE_C11_REVIEW_FIXTURE ?? "");
const reviewRoot = path.resolve(process.env.INFRASPINE_C11_REVIEW_ROOT ?? "");
if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || apiBase.hostname !== "127.0.0.1" || !existsSync(fixturePath) || !reviewRoot.startsWith(path.resolve("."))) {
  throw new Error("Connector acceptance requires the loopback C11 stack and protected synthetic fixture.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
if (!fixture.admin?.email || !fixture.admin?.password || !fixture.managers?.[0]?.email || !fixture.managers?.[0]?.password) {
  throw new Error("The protected Admin and manager fixtures are incomplete.");
}
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for connector acceptance.");

const tempRoot = path.join(reviewRoot, "tmp");
const evidenceDir = path.join(reviewRoot, "evidence");
mkdirSync(tempRoot, { recursive: true });
mkdirSync(evidenceDir, { recursive: true });
const profile = mkdtempSync(path.join(tempRoot, "browser-c16-connectors-"));
let chrome;
let socket;
let sequence = 0;
let dialogAccept = false;
let dialogCount = 0;
const pending = new Map();
const responses = [];
let createdConnectorId = null;
let connectorDeleted = false;

function command(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser timed out at ${method}.`)); }, timeoutMs);
    pending.set(id, { method, resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 20_000) {
  const result = await command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("A connector browser action failed.");
  return result.result?.value;
}
async function waitFor(expression, label, timeoutMs = 30_000) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (await evaluate(expression)) return;
    await delay(100);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function clickText(text, selector = "button") {
  return evaluate(`(()=>{const node=[...document.querySelectorAll(${JSON.stringify(selector)})].find((el)=>el.textContent.trim()===${JSON.stringify(text)});if(!node)return false;node.click();return true})()`);
}
async function setInput(labelText, value) {
  return evaluate(`(()=>{const form=document.querySelector('.cloud-connector-form');const label=[...(form?.querySelectorAll('label')??[])].find((el)=>el.textContent.includes(${JSON.stringify(labelText)}));const input=label?.querySelector('input');if(!input)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(value)});input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
}
async function setSelect(labelText, value) {
  return evaluate(`(()=>{const form=document.querySelector('.cloud-connector-form');const label=[...(form?.querySelectorAll('label')??[])].find((el)=>el.textContent.includes(${JSON.stringify(labelText)}));const select=label?.querySelector('select');if(!select)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,${JSON.stringify(value)});select.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
}
async function responseBody(item) {
  await waitFor(`Boolean(window.__connectorResponses?.some((row)=>row.method===${JSON.stringify(item.method)}&&row.path===${JSON.stringify(item.path)}))`, `${item.method} ${item.path} response body`);
  return evaluate(`(()=>[...window.__connectorResponses].reverse().find((row)=>row.method===${JSON.stringify(item.method)}&&row.path===${JSON.stringify(item.path)})?.body??'')()`);
}
async function waitForResponse(method, pathname, timeoutMs = 15_000, afterIndex = 0) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    const item = responses.slice(afterIndex).reverse().find((row) => row.method === method && row.path === pathname && !row.read);
    if (item) { item.read = true; return item; }
    await delay(50);
  }
  throw new Error(`Timed out waiting for ${method} ${pathname}.`);
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
    if (message.method === "Network.responseReceived") {
      const response = message.params.response;
      const url = new URL(response.url);
      if (url.pathname.startsWith("/api/connectors")) {
        const request = responses._requests?.get(message.params.requestId);
        responses.push({ requestId: message.params.requestId, status: response.status, path: url.pathname, method: request?.method ?? "GET", read: false });
      }
    }
    if (message.method === "Network.requestWillBeSent") {
      responses._requests ??= new Map();
      const url = new URL(message.params.request.url);
      if (url.pathname.startsWith("/api/connectors")) responses._requests.set(message.params.requestId, { method: message.params.request.method });
    }
    if (message.method === "Page.javascriptDialogOpening") {
      dialogCount += 1;
      void command("Page.handleJavaScriptDialog", { accept: dialogAccept });
    }
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error(`Chrome DevTools command ${message.error.code ?? "failed"} at ${item.method ?? "unknown"}.`)) : item.resolve(message.result);
  });
  await command("Page.enable"); await command("Runtime.enable"); await command("Network.enable");
  await command("Page.navigate", { url: base.href });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "synthetic login form");
  await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const pass=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!pass||!form)return false;for(const [el,value] of [[email,${JSON.stringify(fixture.admin.email)}],[pass,${JSON.stringify(fixture.admin.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
  await waitFor("Boolean(document.querySelector('.shell')||document.querySelector('.first-run-setup h1')||document.querySelector('input[name=\"code\"]'))", "authenticated synthetic Admin", 45_000);
  const loginState = await evaluate("({shell:!!document.querySelector('.shell'),setup:!!document.querySelector('.first-run-setup h1'),mfa:!!document.querySelector('input[name=\"code\"]')})");
  if (loginState.mfa) throw new Error("Synthetic Admin login requires a second factor unavailable to this acceptance.");
  if (loginState.setup) {
    await waitFor("Boolean(document.querySelector('.deployment-form input[required]'))", "synthetic first-run setup");
    await evaluate(`(()=>{const labels=[...document.querySelectorAll('.deployment-form label')];const set=(n,v)=>{const i=labels.find((l)=>l.textContent.trim().startsWith(n))?.querySelector('input');if(!i)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(i,v);i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new Event('change',{bubbles:true}));return true};return set('Company name','C16 Synthetic QA')&&set('Industry','Software testing')&&set('Product brand','C16 QA')})()`);
    if (!(await clickText("Save company profile"))) throw new Error("Synthetic setup could not be completed.");
    await waitFor("Boolean(document.querySelector('.shell'))", "configured Admin shell");
  } else if (!loginState.shell) throw new Error("Synthetic Admin did not reach the application shell.");

  if (!(await clickText("Cloud connectors", "nav[aria-label='Main navigation'] button"))) throw new Error("Cloud connectors navigation was unavailable.");
  await waitFor("document.querySelector('main h1')?.textContent.trim()==='Cloud connectors'", "connector Admin screen");
  await waitFor("Boolean(document.querySelector('.cloud-connector-list'))", "connector list");
  await evaluate(`(()=>{const nativeFetch=window.fetch.bind(window);window.__connectorResponses=[];window.fetch=async(input,init)=>{const response=await nativeFetch(input,init);const url=new URL(typeof input==='string'?input:input.url,location.href);if(url.pathname.startsWith('/api/connectors')){const body=await response.clone().text();window.__connectorResponses.push({method:(init?.method??'GET').toUpperCase(),path:url.pathname,status:response.status,body})}return response}})()`);
  const invalid = await evaluate(`fetch('/api/connectors',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'},body:JSON.stringify({name:'invalid synthetic',connector_type:'google_drive',client_id:'synthetic-client',client_secret:'synthetic-secret',visibility:'restricted',sync_interval_minutes:60})}).then((r)=>r.status)`);
  if (invalid !== 422) throw new Error("Google Drive connector validation did not reject a missing refresh token.");

  const managerLogin = await fetch(new URL("api/auth/login", apiBase), {
    method: "POST",
    headers: { "content-type": "application/json", "x-requested-with": "InfraSpineAdmin" },
    body: JSON.stringify({ email: fixture.managers[0].email, password: fixture.managers[0].password }),
  });
  const cookieHeader = managerLogin.headers.get("set-cookie") ?? "";
  const sessionCookie = cookieHeader.split(";")[0].replace(/^infraspine_session=/, "");
  if (managerLogin.status !== 200 || !sessionCookie) throw new Error("Synthetic manager session could not be established for the permission check.");
  const managerResponse = await fetch(new URL("api/connectors", apiBase), { headers: { cookie: `infraspine_session=${sessionCookie}` } });
  const managerDenied = managerResponse.status === 403;
  if (!managerDenied) throw new Error(`A non-admin manager received unexpected connector status ${managerResponse.status} (login ${managerLogin.status}).`);

  const name = `C16 Synthetic connector ${Date.now()}`;
  const secretSentinel = `C16_ONLY_SYNTHETIC_SECRET_${Date.now()}`;
  const tokenSentinel = `C16_ONLY_SYNTHETIC_REFRESH_${Date.now()}`;
  const departmentId = await evaluate(`(()=>[...document.querySelectorAll('.cloud-connector-form label')].find((label)=>label.textContent.includes('Department access'))?.querySelector('select option:nth-child(2)')?.value??'')()`);
  if (departmentId && !(await setSelect("Department access", departmentId))) throw new Error("The restricted connector department selector was unavailable.");
  for (const [label, value] of [["Name", name], ["Client ID", "synthetic-client-id"], ["Client secret", secretSentinel], ["Refresh token", tokenSentinel]]) {
    if (!(await setInput(label, value))) throw new Error(`Required synthetic form field was unavailable: ${label}.`);
  }
  const createResponseStart = responses.length;
  if (!(await clickText("Save connector"))) throw new Error("Connector save action was unavailable.");
  const created = await waitForResponse("POST", "/api/connectors", 15_000, createResponseStart);
  const createBody = await responseBody(created);
  if (created.status !== 201 || createBody.includes(secretSentinel) || createBody.includes(tokenSentinel) || /credential|last_cursor/.test(createBody)) {
    throw new Error(`Connector create check failed: HTTP ${created.status}, secretEcho=${createBody.includes(secretSentinel) || createBody.includes(tokenSentinel)}, secretFieldName=${/credential|last_cursor/.test(createBody)}.`);
  }
  await waitFor(`Boolean([...document.querySelectorAll('.cloud-connector-card h3')].some((node)=>node.textContent.trim()===${JSON.stringify(name)}))`, "created connector card");
  const cleared = await evaluate("[...document.querySelectorAll('.cloud-connector-form input[type=password]')].every((input)=>input.value==='')");
  if (!cleared) throw new Error("The credential fields were not cleared after save.");

  await command("Page.reload", { ignoreCache: true });
  await waitFor("Boolean(document.querySelector('.shell'))", "Admin shell after reload", 45_000);
  await waitFor(`Boolean([...document.querySelectorAll('.cloud-connector-card h3')].some((node)=>node.textContent.trim()===${JSON.stringify(name)}))`, "database-persisted connector after page reload");
  const listCheck = await evaluate("fetch('/api/connectors').then(async(response)=>({status:response.status,body:await response.text()}))");
  const listData = JSON.parse(listCheck.body);
  const persisted = listData.find((item) => item.name === name);
  if (!persisted || persisted.last_cursor !== undefined || JSON.stringify(persisted).includes(secretSentinel) || JSON.stringify(persisted).includes(tokenSentinel)) {
    throw new Error("Connector persistence or secret/cursor response redaction failed.");
  }
  if (departmentId && persisted.department_group_id !== departmentId) throw new Error("The selected restricted department was not persisted.");
  const id = persisted.id;
  createdConnectorId = id;
  const encryptedCheck = spawnSync(path.resolve(".venv/Scripts/python.exe"), ["scripts/verify_c16_connector_ciphertext.py", id], {
    cwd: path.resolve("."), encoding: "utf8", windowsHide: true,
  });
  if (encryptedCheck.status !== 0) throw new Error("The database ciphertext check failed; see protected verification logs.");
  const encryptionEvidence = JSON.parse(encryptedCheck.stdout);

  const mobile = [];
  for (const width of [1365, 390]) {
    await command("Emulation.setDeviceMetricsOverride", { width, height: 844, deviceScaleFactor: 1, mobile: width < 600, screenWidth: width, screenHeight: 844 });
    await delay(150);
    mobile.push(await evaluate(`({width:document.documentElement.clientWidth,root:document.documentElement.scrollWidth,body:document.body.scrollWidth,sample:${width}})`));
  }
  if (mobile.some((item) => item.root > item.width || item.body > item.width)) throw new Error("Cloud connectors page overflowed a tested viewport.");

  dialogAccept = false;
  if (!(await clickText("Sync now"))) throw new Error("Manual sync control was unavailable.");
  await waitFor("document.querySelector('.cloud-connector-form')!==null", "sync confirmation handling");
  await delay(250);
  const syncRequestSent = responses.some((item) => item.path.endsWith("/sync"));
  if (syncRequestSent || dialogCount < 1) throw new Error("Canceling sync confirmation did not prevent provider network activity.");

  dialogAccept = true;
  if (!(await clickText("Remove"))) throw new Error("Connector removal control was unavailable.");
  const deleted = await waitForResponse("DELETE", `/api/connectors/${id}`);
  await waitFor(`![...document.querySelectorAll('.cloud-connector-card h3')].some((node)=>node.textContent.trim()===${JSON.stringify(name)})`, "connector removal");
  if (deleted.status !== 204) throw new Error("Connector removal did not return HTTP 204.");
  connectorDeleted = true;

  const evidence = {
    review_url: base.href,
    api_health_url: new URL("health/ready", apiBase).href,
    synthetic_admin_login: true,
    non_admin_manager_status: managerResponse.status,
    google_refresh_token_required_http: invalid,
    create_http: created.status,
    create_response_credential_redacted: !createBody.includes(secretSentinel) && !createBody.includes(tokenSentinel),
    list_http: listCheck.status,
    response_cursor_redacted: persisted.last_cursor === undefined,
    encrypted_at_rest: encryptionEvidence.aad_bound_synthetic_credentials_decrypt && encryptionEvidence.plaintext_sentinels_absent,
    ciphertext_nonce_valid: encryptionEvidence.ciphertext_present && encryptionEvidence.nonce_is_96_bit,
    credential_fields_cleared: cleared,
    persistence_after_page_reload: true,
    restricted_department_selection: departmentId ? "selected and persisted" : "no active synthetic department available",
    canceled_manual_sync_no_provider_call: !syncRequestSent,
    delete_http: deleted.status,
    responsive_measurements: mobile,
    checks: ["current-source authenticated Admin browser", "backend role denial", "provider-specific field validation", "encrypted credential save with redacted API response", "database-backed list after reload", "outbound sync confirmation cancellation", "credential removal", "desktop and mobile widths"],
  };
  writeFileSync(path.join(evidenceDir, "c16-cloud-connectors-browser.json"), `${JSON.stringify(evidence, null, 2)}\n`, "utf8");
  console.log("C16 current-source cloud connector browser/API acceptance passed.");
} finally {
  if (createdConnectorId && !connectorDeleted && socket?.readyState === WebSocket.OPEN) {
    try {
      await evaluate(`fetch('/api/connectors/${createdConnectorId}',{method:'DELETE',headers:{'X-Requested-With':'InfraSpineAdmin'}}).then((response)=>response.status)`, 5_000);
    } catch { /* Keep the original browser result; the exact synthetic record can be inspected locally if cleanup failed. */ }
  }
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (chrome && chrome.exitCode === null) {
    chrome.kill();
    await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3_000)]);
  }
  const safeTempRoot = path.resolve(tempRoot);
  const safeProfile = path.resolve(profile);
  if (safeProfile.startsWith(`${safeTempRoot}${path.sep}`) && path.basename(safeProfile).startsWith("browser-c16-connectors-")) {
    rmSync(safeProfile, { recursive: true, force: true });
  }
}
