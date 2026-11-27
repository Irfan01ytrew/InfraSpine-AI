import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.argv[2] ?? "http://127.0.0.1:5182/admin/");
const fixturePath = path.resolve(process.env.INFRASPINE_C11_REVIEW_FIXTURE ?? "");
const reviewRoot = path.resolve(process.env.INFRASPINE_C11_REVIEW_ROOT ?? "");
if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || !fixturePath || reviewRoot === path.parse(reviewRoot).root) {
  throw new Error("C11 acceptance requires the loopback review URL and protected synthetic fixture.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
if (!fixture.admin?.email || !fixture.admin?.password) throw new Error("The protected Admin fixture is incomplete.");
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for C11 acceptance.");

const tempRoot = path.join(reviewRoot, "tmp");
mkdirSync(tempRoot, { recursive: true });
const profile = mkdtempSync(path.join(tempRoot, "browser-c11-sqlite-"));
const downloads = path.join(reviewRoot, "downloads");
const evidenceDir = path.join(reviewRoot, "evidence");
mkdirSync(downloads, { recursive: true });
mkdirSync(evidenceDir, { recursive: true });
let chrome;
let socket;
let sequence = 0;
const pending = new Map();
const responseStatuses = {};

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
  if (response.exceptionDetails) throw new Error("A C11 browser action failed.");
  return response.result?.value;
}
async function waitFor(expression, label, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await delay(100);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function clickText(text, selector = "button") {
  return evaluate(`(()=>{const node=[...document.querySelectorAll(${JSON.stringify(selector)})].find((el)=>el.textContent.trim()===${JSON.stringify(text)});if(!node)return false;node.click();return true})()`);
}
async function setSelectByLabel(labelText, value) {
  return evaluate(`(()=>{const form=document.querySelector('.database-analysis-form');const label=[...(form?.querySelectorAll('label')??[])].find((el)=>el.textContent.includes(${JSON.stringify(labelText)}));const select=label?.querySelector('select');if(!select)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,${JSON.stringify(value)});select.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
}
async function setInputByLabel(labelText, value) {
  return evaluate(`(()=>{const form=document.querySelector('.database-analysis-form');const label=[...(form?.querySelectorAll('label')??[])].find((el)=>el.textContent.includes(${JSON.stringify(labelText)}));const input=label?.querySelector('input');if(!input)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(value)});input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
}
async function waitForDownload(filename, timeoutMs = 12_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const files = readdirSync(downloads);
    if (files.includes(filename) && !files.some((item) => item.endsWith(".crdownload"))) return path.join(downloads, filename);
    await delay(100);
  }
  throw new Error("The UI export did not finish downloading within its bounded wait.");
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
      const url = message.params.response.url;
      for (const pathName of ["/api/rag/database-analysis/catalog", "/api/rag/database-analysis/query", "/api/rag/database-analysis/export"]) {
        if (url.includes(pathName)) responseStatuses[pathName] = message.params.response.status;
      }
    }
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error("A Chrome DevTools command failed.")) : item.resolve(message.result);
  });
  await command("Page.enable");
  await command("Runtime.enable");
  await command("Network.enable");
  await command("Page.setDownloadBehavior", { behavior: "allow", downloadPath: downloads });
  await command("Page.navigate", { url: base.href });
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "login form");
  await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const pass=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!pass||!form)return false;for(const [el,value] of [[email,${JSON.stringify(fixture.admin.email)}],[pass,${JSON.stringify(fixture.admin.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
  await waitFor("Boolean(document.querySelector('.shell')||document.querySelector('.first-run-setup h1')||document.querySelector('input[name=\"code\"]'))", "authenticated Admin state", 45_000);
  const authenticatedState = await evaluate(`(()=>({shell:!!document.querySelector('.shell'),setup:!!document.querySelector('.first-run-setup h1'),mfa:!!document.querySelector('input[name="code"]'),loginError:!!document.querySelector('.inline-alert-error')}))()`);
  if (authenticatedState.mfa || authenticatedState.loginError) throw new Error(`Synthetic Admin sign-in requires an unsupported follow-up: ${JSON.stringify(authenticatedState)}.`);
  let clonedReviewSetupCompleted = false;
  if (authenticatedState.setup) {
    await waitFor("Boolean(document.querySelector('.deployment-form input[required]'))", "synthetic clone company profile");
    const setupFields = await evaluate(`(()=>{const labels=[...document.querySelectorAll('.deployment-form label')];const set=(name,value)=>{const label=labels.find((item)=>item.textContent.trim().startsWith(name));const input=label?.querySelector('input');if(!input)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,value);input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));return true};return {company:set('Company name','C11 Synthetic QA'),industry:set('Industry','Software testing'),brand:set('Product brand','C11 QA')}})()`);
    if (!setupFields.company || !setupFields.industry || !setupFields.brand || !(await clickText("Save company profile"))) {
      throw new Error("The cloned synthetic company setup form was unavailable.");
    }
    await waitFor("Boolean(document.querySelector('.shell'))", "configured synthetic Admin shell", 30_000);
    clonedReviewSetupCompleted = true;
  } else if (!authenticatedState.shell) {
    throw new Error(`Synthetic Admin authenticated into an unsupported review state: ${JSON.stringify(authenticatedState)}.`);
  }
  if (!(await clickText("Knowledge", "nav[aria-label='Main navigation'] button"))) throw new Error("Knowledge navigation entry was unavailable.");
  await waitFor("document.querySelector('main h1')?.textContent.trim()==='Knowledge'", "Knowledge page");
  await evaluate("document.querySelector('.database-analysis')?.scrollIntoView({block:'center'})");
  if (!(await clickText("Analyze business database", ".database-analysis-toggle"))) throw new Error("Database analysis panel did not open.");
  await waitFor("Boolean(document.querySelector('.database-analysis-form select'))", "configured SQLite catalog", 15_000);
  const catalog = await evaluate("({status:document.querySelector('.database-analysis-heading')?.innerText??'',tables:[...document.querySelectorAll('.database-analysis-form label:first-child option')].map((option)=>option.value),sourceVisible:document.querySelector('.database-analysis-heading')?.innerText.includes('Read-only business database')??false})");
  if (!catalog.tables.includes("main.orders") || !catalog.sourceVisible) throw new Error("The synthetic SQLite allowlist was not rendered by the current UI.");
  if (!(await setSelectByLabel("Filter column", "customer"))) throw new Error("The C11 filter column control was unavailable.");
  await waitFor("Boolean([...document.querySelectorAll('.database-analysis-form label')].find((el)=>el.textContent.includes('Filter value'))?.querySelector('input'))", "text filter control");
  if (!(await setSelectByLabel("Filter rule", "contains"))) throw new Error("The C11 text filter rule control was unavailable.");
  if (!(await setInputByLabel("Filter value", "SUM"))) throw new Error("The C11 filter value control was unavailable.");
  if (!(await clickText("Run read-only analysis"))) throw new Error("The bounded query control was unavailable.");
  await waitFor("Boolean(document.querySelector('.database-analysis-result tbody tr'))", "SQLite query results", 15_000);
  const queryResult = await evaluate(`(()=>({provenance:document.querySelector('.database-analysis-provenance')?.textContent??'',headers:[...document.querySelectorAll('.database-analysis-table thead th')].map((cell)=>cell.textContent.trim()),cells:[...document.querySelectorAll('.database-analysis-table tbody td')].map((cell)=>cell.textContent.trim()),rowCount:document.querySelectorAll('.database-analysis-table tbody tr').length}))()`);
  if (!queryResult.provenance.includes("Synthetic QA reporting copy") || !queryResult.cells.includes("=SUM(1,1)") || responseStatuses["/api/rag/database-analysis/query"] !== 200) {
    throw new Error("The SQLite query result did not match the synthetic source or successful API response.");
  }
  if (!(await clickText("Export this result as CSV"))) throw new Error("The result export control was unavailable.");
  const exportPath = await waitForDownload("business-analysis-main-orders.csv");
  const csv = readFileSync(exportPath, "utf8").replace(/^\uFEFF/, "");
  if (!csv.includes("'=SUM(1,1)") || responseStatuses["/api/rag/database-analysis/export"] !== 200) {
    throw new Error("The exported CSV did not pass formula escaping or HTTP success checks.");
  }

  const responsive = [];
  for (const [width, direction] of [[1365, "ltr-desktop"], [390, "ltr-mobile"]]) {
    await command("Emulation.setDeviceMetricsOverride", { width, height: 844, deviceScaleFactor: 1, mobile: width < 600, screenWidth: width, screenHeight: 844 });
    await delay(200);
    responsive.push(await evaluate(`({width:document.documentElement.clientWidth,root:document.documentElement.scrollWidth,body:document.body.scrollWidth,tableWrapClient:document.querySelector('.database-analysis-table-wrap')?.clientWidth??null,tableWrapScroll:document.querySelector('.database-analysis-table-wrap')?.scrollWidth??null,dir:document.documentElement.dir||'ltr',sample:${JSON.stringify(direction)}})`));
  }
  const rtlEnabled = await evaluate(`(()=>{const button=document.querySelector('button[aria-label*="Right-to-Left"]');if(!button)return false;button.click();return document.documentElement.dir==='rtl'})()`);
  if (!rtlEnabled) throw new Error("The RTL direction control was unavailable on the current C11 route.");
  await command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true, screenWidth: 390, screenHeight: 844 });
  await delay(200);
  responsive.push(await evaluate("({width:document.documentElement.clientWidth,root:document.documentElement.scrollWidth,body:document.body.scrollWidth,tableWrapClient:document.querySelector('.database-analysis-table-wrap')?.clientWidth??null,tableWrapScroll:document.querySelector('.database-analysis-table-wrap')?.scrollWidth??null,dir:document.documentElement.dir||'ltr',sample:'rtl-mobile'})"));
  if (responsive.some((item) => item.width !== 390 && item.sample !== "ltr-desktop" || item.root > item.width || item.body > item.width)) {
    throw new Error("C11 database analysis overflowed a tested page viewport.");
  }
  const evidence = {
    review_url: base.href,
    synthetic_admin_login: true,
    cloned_synthetic_review_setup_completed: clonedReviewSetupCompleted,
    synthetic_source: "rooted SQLite copy, 3 rows, main.orders allowlist",
    database_analysis_catalog_http: responseStatuses["/api/rag/database-analysis/catalog"],
    query_http: responseStatuses["/api/rag/database-analysis/query"],
    export_http: responseStatuses["/api/rag/database-analysis/export"],
    allowlisted_table: catalog.tables[0],
    query: { filter_column: "customer", operation: "contains", matching_rows: queryResult.rowCount, provenance_rendered: queryResult.provenance.includes("Synthetic QA reporting copy") },
    csv_download: { filename: path.basename(exportPath), formula_escaped: csv.includes("'=SUM(1,1)"), bytes: Buffer.byteLength(csv) },
    responsive,
    checks: ["current-source authenticated Admin UI", "real API catalog/query/export", "rooted SQLite read-only source", "UI result provenance", "downloaded CSV formula escaping", "desktop, 390px mobile and RTL layout"],
  };
  writeFileSync(path.join(evidenceDir, "c11-sqlite-export-browser.json"), `${JSON.stringify(evidence, null, 2)}\n`, "utf8");
  console.log("C11 current-source SQLite analysis and CSV export browser acceptance passed.");
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (chrome && chrome.exitCode === null) {
    chrome.kill();
    await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3_000)]);
  }
  const safeTempRoot = path.resolve(tempRoot);
  const safeProfile = path.resolve(profile);
  if (safeProfile.startsWith(`${safeTempRoot}${path.sep}`) && path.basename(safeProfile).startsWith("browser-c11-sqlite-")) {
    rmSync(safeProfile, { recursive: true, force: true });
  }
}
