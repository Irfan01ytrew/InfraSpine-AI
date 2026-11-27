#!/usr/bin/env node
/** Current-source Employee/outsider browser acceptance for department-scoped database analysis. */
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_C11_DEPT_URL ?? "http://127.0.0.1:5186/admin/");
const fixturePath = process.env.INFRASPINE_C11_DEPT_FIXTURE;
const reviewRoot = process.env.INFRASPINE_C11_DEPT_ROOT;
const groupId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || !fixturePath || !reviewRoot) {
  throw new Error("C11 department acceptance requires its isolated loopback review and protected fixture.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const admin = fixture.admin;
const employee = fixture.finance_employee;
const outsider = fixture.unassigned_employee;
if (![admin, employee, outsider].every((item) => item?.email && item?.password)) {
  throw new Error("The protected C11 synthetic account fixture is incomplete.");
}
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for C11 department browser acceptance.");

const tempRoot = path.join(reviewRoot, "tmp");
const evidenceDir = path.join(reviewRoot, "evidence");
mkdirSync(tempRoot, { recursive: true });
mkdirSync(evidenceDir, { recursive: true });
const profile = mkdtempSync(path.join(tempRoot, "browser-c11-dept-"));
const screenshotPath = path.join(evidenceDir, "c11-department-finance-390.png");
const sqlitePath = path.join(reviewRoot, "data", "business", "reporting.sqlite3");
const sqliteSha256AtStart = createHash("sha256").update(readFileSync(sqlitePath)).digest("hex");
let chrome;
let socket;
let sequence = 0;
const pending = new Map();
const responses = new Map();

function command(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser timed out at ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 90_000) {
  const result = await command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("A C11 department browser action failed.");
  return result.result?.value;
}
async function waitFor(expression, label, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(100);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function waitForResponse(urlPart, afterCount, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if ((responses.get(urlPart)?.count ?? 0) > afterCount) return responses.get(urlPart);
    await delay(50);
  }
  throw new Error(`Timed out waiting for ${urlPart}.`);
}
async function clickText(text, selector = "button") {
  return Boolean(await evaluate(`(()=>{const el=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(text)});if(!el)return false;el.click();return true})()`));
}
async function setSelectByLabel(labelText, value) {
  return Boolean(await evaluate(`(()=>{const form=document.querySelector('.database-analysis-form');const label=[...(form?.querySelectorAll('label')??[])].find(x=>x.textContent.includes(${JSON.stringify(labelText)}));const el=label?.querySelector('select');if(!el)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`));
}
async function setInputByLabel(labelText, value) {
  return Boolean(await evaluate(`(()=>{const form=document.querySelector('.database-analysis-form');const label=[...(form?.querySelectorAll('label')??[])].find(x=>x.textContent.includes(${JSON.stringify(labelText)}));const el=label?.querySelector('input');if(!el)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`));
}
async function signIn(account) {
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "synthetic login form");
  const submitted = await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const pass=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!pass||!form)return false;for(const [el,value] of [[email,${JSON.stringify(account.email)}],[pass,${JSON.stringify(account.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
  if (!submitted) throw new Error("The synthetic login form could not be submitted.");
  await waitFor("Boolean(document.querySelector('.shell')||document.querySelector('.first-run-setup h1')||document.querySelector('input[name=\"code\"]'))", "authenticated synthetic account", 45_000);
  const state = await evaluate("({shell:!!document.querySelector('.shell'),setup:!!document.querySelector('.first-run-setup h1'),mfa:!!document.querySelector('input[name=\"code\"]'),error:!!document.querySelector('.inline-alert-error')})");
  if (state.mfa || state.error) throw new Error("Synthetic C11 account sign-in requires unsupported follow-up.");
  if (state.setup) {
    await waitFor("Boolean(document.querySelector('.deployment-form input[required]'))", "synthetic first-run setup form");
    const setFields = await evaluate(`(()=>{const labels=[...document.querySelectorAll('.deployment-form label')];const set=(name,value)=>{const label=labels.find(x=>x.textContent.trim().startsWith(name));const el=label?.querySelector('input');if(!el)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true};return {company:set('Company name','C11 Department QA'),industry:set('Industry','Synthetic reporting'),brand:set('Product brand','C11 QA')}})()`);
    if (!setFields.company || !setFields.industry || !setFields.brand || !await clickText("Save company profile")) {
      throw new Error("Synthetic C11 first-run company setup form could not be saved.");
    }
    await waitFor("Boolean(document.querySelector('.shell'))", "configured synthetic workspace", 35_000);
  } else if (!state.shell) {
    throw new Error("Synthetic C11 login ended in an unsupported state.");
  }
  const identity = await evaluate("(async()=>{const r=await fetch('/api/auth/me');const d=r.ok?await r.json():null;return {status:r.status,role:d?.role}})()");
  if (identity?.status !== 200) throw new Error("The C11 synthetic role session could not be verified.");
  return identity.role;
}
async function signOut() {
  const clicked = await evaluate(`(()=>{const b=document.querySelector('button[aria-label="Sign out"]');if(!b)return false;b.click();return true})()`);
  if (!clicked) throw new Error("The current synthetic account could not sign out through the visible control.");
  await waitFor("Boolean(document.querySelector('form.login-form'))", "login after sign out");
}
async function openAnalysis() {
  if (!await clickText("Knowledge", "nav[aria-label='Main navigation'] button")) throw new Error("Knowledge navigation is unavailable for this role.");
  await waitFor("document.querySelector('main h1')?.textContent.trim()==='Knowledge'", "Knowledge page");
  await waitFor("Boolean(document.querySelector('.database-analysis-toggle'))", "business reporting panel for this role");
  await evaluate("document.querySelector('.database-analysis')?.scrollIntoView({block:'center'})");
  if (!await clickText("Analyze business database", ".database-analysis-toggle")) throw new Error("Database analysis panel did not open.");
}
async function apiRequest(pathname, method = "GET", body) {
  return evaluate(`(async()=>{const r=await fetch(${JSON.stringify(pathname)},{method:${JSON.stringify(method)},credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'},${body === undefined ? "" : `body:JSON.stringify(${JSON.stringify(body)}),`}});let data=null;try{data=await r.json()}catch{}return {status:r.status,data}})()`);
}

try {
  chrome = spawn(browserPath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profile, "DevToolsActivePort");
  const startBy = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < startBy) await delay(100);
  if (!existsSync(portFile)) throw new Error("Isolated C11 Chromium did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!target?.webSocketDebuggerUrl) throw new Error("Isolated C11 Chromium did not expose its page target.");
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method === "Network.responseReceived") {
      const url = message.params.response.url;
      for (const part of ["/api/rag/database-analysis/catalog", "/api/rag/database-analysis/query"]) {
        if (url.includes(part)) {
          const previous = responses.get(part) ?? { count: 0, status: 0 };
          responses.set(part, { count: previous.count + 1, status: message.params.response.status });
        }
      }
    }
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error("A Chrome DevTools command failed.")) : item.resolve(message.result);
  });
  await command("Page.enable"); await command("Runtime.enable"); await command("Network.enable");
  await command("Emulation.setDeviceMetricsOverride", { width: 1365, height: 900, deviceScaleFactor: 1, mobile: false });
  await command("Page.navigate", { url: base.href });

  const adminRole = await signIn(admin);
  if (adminRole !== "admin") throw new Error("C11 synthetic Admin role did not authenticate.");
  await signOut();

  const employeeRole = await signIn(employee);
  if (employeeRole !== "employee") throw new Error("C11 Finance synthetic Employee role did not authenticate.");
  await openAnalysis();
  await waitFor("Boolean(document.querySelector('.database-analysis-form'))", "department-filtered read-only source", 20_000);
  const employeeCatalog = await apiRequest("/api/rag/database-analysis/catalog");
  if (employeeCatalog.status !== 200 || !employeeCatalog.data?.enabled || employeeCatalog.data.tables?.map((row) => row.name).join(",") !== "main.orders") {
    throw new Error("The Finance Employee did not receive the expected scoped business catalog.");
  }
  if (!await setSelectByLabel("Filter column", "department") || !await setInputByLabel("Filter value", "Finance")) {
    throw new Error("The department query controls were unavailable in the current browser UI.");
  }
  let previous = responses.get("/api/rag/database-analysis/query")?.count ?? 0;
  if (!await clickText("Run read-only analysis")) throw new Error("The bounded read-only query button was unavailable.");
  await waitForResponse("/api/rag/database-analysis/query", previous);
  await waitFor("Boolean(document.querySelector('.database-analysis-result tbody tr'))", "Finance query result");
  const financeResult = await evaluate(`(()=>({provenance:document.querySelector('.database-analysis-provenance')?.textContent??'',rows:[...document.querySelectorAll('.database-analysis-table tbody tr')].map(tr=>[...tr.querySelectorAll('td')].map(td=>td.textContent.trim())),width:{viewport:document.documentElement.clientWidth,root:document.documentElement.scrollWidth,body:document.body.scrollWidth}}))()`);
  const financeQueryStatus = responses.get("/api/rag/database-analysis/query")?.status;
  if (financeQueryStatus !== 200 || financeResult.rows.length !== 2 || !financeResult.rows.flat().includes("Finance") || financeResult.rows.flat().some((value) => value.includes("Sales") || value.includes("900"))) {
    throw new Error("The current-source Finance query exposed a non-Finance row or missed Finance rows.");
  }
  if (!financeResult.provenance.includes("Synthetic Finance reporting copy")) throw new Error("The Employee result omitted its source provenance.");

  if (!await setInputByLabel("Filter value", "Sales")) throw new Error("The attempted scope-escape filter could not be set.");
  previous = responses.get("/api/rag/database-analysis/query")?.count ?? 0;
  if (!await clickText("Run read-only analysis")) throw new Error("The scope-escape query could not be submitted.");
  await waitForResponse("/api/rag/database-analysis/query", previous);
  await waitFor("Boolean(document.querySelector('.database-analysis-result'))", "empty scoped query result");
  const attemptedEscape = await evaluate("({status:200,rows:[...document.querySelectorAll('.database-analysis-table tbody tr')].map(tr=>tr.innerText.trim())})");
  const attemptedSalesFilterStatus = responses.get("/api/rag/database-analysis/query")?.status;
  if (attemptedSalesFilterStatus !== 200 || !attemptedEscape.rows.some((row) => row.includes("No matching rows"))) {
    throw new Error("A user-supplied Sales filter bypassed the Finance server-side row predicate.");
  }

  await command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true, screenWidth: 390, screenHeight: 844 });
  await evaluate("document.querySelector('.database-analysis')?.scrollIntoView({block:'center'})");
  await delay(250);
  const mobile = await evaluate("(()=>{const el=document.querySelector('.database-analysis-toggle');const r=el?.getBoundingClientRect();return {viewport:document.documentElement.clientWidth,root:document.documentElement.scrollWidth,body:document.body.scrollWidth,controlVisible:!!r&&r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth,controlRect:r?{top:r.top,bottom:r.bottom,left:r.left,right:r.right}:null}})()");
  if (mobile.viewport !== 390 || mobile.root > 390 || mobile.body > 390 || !mobile.controlVisible) throw new Error("C11 Finance analysis control was not visible within the 390px viewport.");
  const screenshot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"));
  await signOut();

  const adminAfter = await signIn(admin);
  if (adminAfter !== "admin") throw new Error("C11 synthetic Admin could not return to revoke department membership.");
  const members = await apiRequest(`/api/admin/access/groups/${groupId}/members`);
  const member = members.data?.members?.find((item) => item.email?.toLowerCase() === employee.email.toLowerCase());
  if (members.status !== 200 || !member?.user_id) throw new Error("Finance membership could not be identified for revocation.");
  const revoked = await apiRequest(`/api/admin/access/groups/${groupId}/members/${member.user_id}`, "DELETE");
  if (revoked.status !== 204) throw new Error("The synthetic Finance membership revocation failed.");
  await signOut();

  const employeeAfter = await signIn(employee);
  if (employeeAfter !== "employee") throw new Error("Revoked Finance Employee could not authenticate for fresh access checks.");
  await openAnalysis();
  const revokedCatalog = await apiRequest("/api/rag/database-analysis/catalog");
  const revokedQuery = await apiRequest("/api/rag/database-analysis/query", "POST", { table: "main.orders", operation: "rows", limit: 10 });
  if (revokedCatalog.status !== 200 || revokedCatalog.data?.enabled !== false || revokedCatalog.data?.tables?.length !== 0 || revokedQuery.status !== 403) {
    throw new Error("Department revocation did not disable catalog visibility and deny the next query.");
  }
  await signOut();

  const outsiderRole = await signIn(outsider);
  if (outsiderRole !== "employee") throw new Error("C11 unassigned Employee role did not authenticate.");
  await openAnalysis();
  const outsiderCatalog = await apiRequest("/api/rag/database-analysis/catalog");
  const outsiderQuery = await apiRequest("/api/rag/database-analysis/query", "POST", { table: "main.orders", operation: "rows", limit: 10 });
  if (outsiderCatalog.status !== 200 || outsiderCatalog.data?.enabled !== false || outsiderCatalog.data?.tables?.length !== 0 || outsiderQuery.status !== 403) {
    throw new Error("Out-of-department Employee received a table or query result.");
  }

  const sqliteSha256AtEnd = createHash("sha256").update(readFileSync(sqlitePath)).digest("hex");
  if (sqliteSha256AtStart !== sqliteSha256AtEnd) throw new Error("The read-only reporting source changed during browser acceptance.");
  const sourceFiles = ["app/api/routes/rag.py", "app/services/database_analysis.py", "frontend/src/components/DatabaseAnalysisPanel.tsx", "frontend/src/components/KnowledgePage.tsx", "scripts/browser_c11_department_analysis_acceptance.mjs"];
  const sourceSha256 = Object.fromEntries(sourceFiles.map((file) => [file, createHash("sha256").update(readFileSync(file)).digest("hex")]));
  const evidence = {
    result: "PASS",
    reviewUrl: base.href,
    apiHealthUrl: "http://127.0.0.1:63014/health/ready",
    database: "infraspine_c11dept_20261029_test",
    migration: "0063_cloud_source_acl_intersection",
    sourceLabel: "Synthetic Finance reporting copy",
    sqliteSourceSha256: sqliteSha256AtEnd,
    financeCatalog: employeeCatalog.data.tables.map((item) => item.name),
    financeRows: financeResult.rows,
    financeQueryStatus,
    provenance: financeResult.provenance,
    attemptedSalesFilterRows: attemptedEscape.rows,
    attemptedSalesFilterStatus,
    membershipRevocation: { deleteStatus: revoked.status, catalogEnabledAfter: revokedCatalog.data.enabled, queryStatusAfter: revokedQuery.status },
    outsiderDenial: { catalogEnabled: outsiderCatalog.data.enabled, queryStatus: outsiderQuery.status },
    mobile390: mobile,
    screenshot: path.relative(process.cwd(), screenshotPath).replaceAll("\\", "/"),
    browserResponseStatuses: Object.fromEntries([...responses.entries()].map(([key, value]) => [key, value.status])),
    sourceSha256,
  };
  writeFileSync(path.join(evidenceDir, "c11-department-analysis-browser.json"), JSON.stringify(evidence, null, 2) + "\n", { mode: 0o600 });
  console.log("C11_CURRENT_SOURCE_DEPARTMENT_BROWSER=PASS; Finance row filter; Sales scope escape denied; revocation and outsider denied; mobile 390px");
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (chrome && chrome.exitCode === null) {
    chrome.kill();
    await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3_000)]);
  }
  const safeTempRoot = path.resolve(tempRoot);
  const safeProfile = path.resolve(profile);
  if (safeProfile.startsWith(`${safeTempRoot}${path.sep}`) && path.basename(safeProfile).startsWith("browser-c11-dept-")) {
    await import("node:fs/promises").then(({ rm }) => rm(safeProfile, { recursive: true, force: true }));
  }
}
