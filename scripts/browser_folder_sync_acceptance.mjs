#!/usr/bin/env node
/** Browser acceptance for scheduled folder sync on a disposable synthetic restore candidate. */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_F08_URL ?? "http://127.0.0.1:18105/");
const fixturePath = process.env.INFRASPINE_QA_USERS_FILE;
const testDirectory = path.resolve(process.env.INFRASPINE_F08_TEST_DIR ?? "");
const importRoot = process.env.INFRASPINE_F08_IMPORT_ROOT ? path.resolve(process.env.INFRASPINE_F08_IMPORT_ROOT) : null;
const relativePath = importRoot
  ? path.relative(importRoot, testDirectory).split(path.sep).join("/")
  : path.basename(testDirectory);
const fixtureFile = path.join(testDirectory, "guide.txt");
const sourceName = `QA folder sync ${Date.now()}`;
const syntheticText = "Synthetic F08 operations guide. The compressor inspection interval is every 30 days. Keep this QA-only instruction in the isolated restore candidate.";
const roles = ["admin", "manager", "employee"];
const evidence = [];
let cdp;
let browser;
let renamedDirectory;

if (!["http:", "https:"].includes(base.protocol) || !["localhost", "127.0.0.1", "::1"].includes(base.hostname)) {
  throw new Error("Folder-sync browser acceptance is restricted to a loopback candidate.");
}
if (!fixturePath || !existsSync(fixturePath)) throw new Error("The protected synthetic role fixture is unavailable.");
if (!/^infraspine-combined-restore-[a-f0-9]{12}$/i.test(path.basename(path.resolve(testDirectory, "..", "..")))) {
  throw new Error("The folder-sync fixture directory is outside the expected disposable restore project.");
}
if (!relativePath || path.isAbsolute(relativePath) || relativePath.split("/").some((part) => part === ".." || part === "")) {
  throw new Error("The F08 source path is not safely relative to the configured import root.");
}
if (!/^f08-acceptance(?:-r\d+)?$/i.test(path.basename(testDirectory)) || path.basename(path.dirname(testDirectory)) !== "imports" || !existsSync(fixtureFile)) {
  throw new Error("The bounded synthetic F08 folder fixture is not ready.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const credentials = {
  admin: fixture.admin,
  manager: fixture.manager ?? fixture.managers?.[0],
  employee: fixture.employee ?? fixture.employees?.[0],
};
for (const role of roles) {
  if (typeof credentials?.[role]?.email !== "string" || typeof credentials?.[role]?.password !== "string") {
    throw new Error("The protected fixture does not contain all three synthetic accounts.");
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
  const selected = candidates.find((candidate) => candidate && existsSync(candidate));
  if (!selected) throw new Error("Chrome, Chromium or Edge was not found.");
  return selected;
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
    const slot = pending.get(message.id);
    clearTimeout(slot.timer);
    pending.delete(message.id);
    if (message.error) slot.reject(new Error("Browser command failed."));
    else slot.resolve(message.result);
  });
  return {
    socket,
    ready: () => opened,
    command(method, params = {}, timeoutMs = 15_000) {
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

async function evaluate(expression, timeoutMs = 15_000) {
  const result = await cdp.command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("Browser-side F08 action raised an exception.");
  return result.result?.value;
}

async function waitFor(expression, timeoutMs = 25_000) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    const result = await evaluate(expression);
    if (result) return result;
    await delay(250);
  }
  return false;
}

async function request(method, route, body) {
  const result = await evaluate(`(async()=>{const init={method:${JSON.stringify(method)},credentials:"same-origin",headers:{"X-Requested-With":"XMLHttpRequest"}};const body=${body === undefined ? "undefined" : JSON.stringify(body)};if(body!==undefined){init.headers["Content-Type"]="application/json";init.body=JSON.stringify(body)}const response=await fetch(${JSON.stringify(route)},init);let json=null;try{json=await response.json()}catch{}return {status:response.status,json}})()`);
  if (!result || typeof result.status !== "number") throw new Error("Browser API request did not return a status.");
  return result;
}

async function expect(method, route, status, body) {
  const result = await request(method, route, body);
  if (result.status !== status) throw new Error(`${method} ${route} returned HTTP ${result.status}; expected ${status}.`);
  return result.json;
}

async function signIn(role) {
  const account = credentials[role];
  const expectedRoles = role === "employee" ? ["employee", "user"] : [role];
  const submitted = await evaluate(`(()=>{const email=document.querySelector('input[name="email"]');const password=document.querySelector('input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!password||!form)return false;const set=(input,value)=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(input,value);input.dispatchEvent(new Event("input",{bubbles:true}))};set(email,${JSON.stringify(account.email)});set(password,${JSON.stringify(account.password)});form.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));return true})()`);
  if (!submitted || !await waitFor(`(async()=>{const response=await fetch('/api/auth/me',{credentials:'same-origin'});if(!response.ok)return false;const user=await response.json();return ${JSON.stringify(expectedRoles)}.includes(user.role)})()`)) {
    throw new Error(`Synthetic ${role} browser login failed.`);
  }
}

async function signOut() {
  const clicked = await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Sign out"]');if(!button)return false;button.click();return true})()`);
  if (!clicked || !await waitFor(`Boolean(document.querySelector('form.login-form'))`)) throw new Error("Browser logout failed.");
}

async function navigate(label, selector) {
  const clicked = await evaluate(`(()=>{const item=[...document.querySelectorAll('button.nav-item')].find(button=>button.textContent.trim()===${JSON.stringify(label)});if(!item)return false;item.click();return true})()`);
  if (!clicked || !await waitFor(`Boolean(document.querySelector(${JSON.stringify(selector)}))`)) throw new Error(`The ${label} page did not open.`);
}

async function sourceRowAction(label) {
  const available = await waitFor(`(()=>{const row=[...document.querySelectorAll('table tr')].find(item=>item.textContent.includes(${JSON.stringify(sourceName)}));return Boolean([...((row?.querySelectorAll('button'))??[])].some(item=>item.textContent.trim()===${JSON.stringify(label)}))})()`);
  if (!available) throw new Error(`The Admin source ${label.toLowerCase()} action was unavailable.`);
  const clicked = await evaluate(`(()=>{const row=[...document.querySelectorAll('.folder-sync-history,table tr')].find(item=>item.textContent.includes(${JSON.stringify(sourceName)}));const button=[...(row?.querySelectorAll('button')??[])].find(item=>item.textContent.trim()===${JSON.stringify(label)});if(!button||button.disabled)return false;button.click();return true})()`);
  if (!clicked) throw new Error(`The Admin source ${label.toLowerCase()} action was unavailable.`);
}

async function sourceState(id, enabled) {
  return waitFor(`(async()=>{const response=await fetch('/api/folder-sync/sources',{credentials:'same-origin'});if(!response.ok)return false;const body=await response.json();return body.items.some(item=>item.id===${JSON.stringify(id)}&&item.enabled===${enabled})})()`);
}

async function waitForRun(id, previousRunIds, matcher, timeoutMs = 100_000) {
  const previous = new Set(previousRunIds);
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    const data = await expect("GET", `/api/folder-sync/sources/${id}/runs`, 200);
    const run = data.items.find((item) => !previous.has(item.id));
    if (run && run.status !== "running" && matcher(run)) return run;
    await delay(1000);
  }
  throw new Error("The worker did not complete the expected bounded folder-sync run.");
}

async function runBrowserAcceptance() {
  const profile = (await import("node:fs")).mkdtempSync(path.join(tmpdir(), "infraspine-f08-browser-"));
  const activePortFile = path.join(profile, "DevToolsActivePort");
  browser = spawn(findBrowser(), [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-background-networking", "--disable-sync", "--remote-debugging-port=0", `--user-data-dir=${profile}`, base.href,
  ], { stdio: "ignore", windowsHide: true });
  try {
    const deadline = Date.now() + 30_000;
    while (!existsSync(activePortFile) && Date.now() < deadline && browser.exitCode === null) await delay(100);
    if (!existsSync(activePortFile)) throw new Error("Browser debugger port did not become available.");
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
    if (!await waitFor(`Boolean(document.querySelector('form.login-form'))`)) throw new Error("Candidate login form did not render.");

    await signIn("admin");
    evidence.push("F08_ADMIN_LOGIN=PASS");
    const groups = (await expect("GET", "/api/admin/access/groups", 200)).groups;
    let department = groups.find((group) => group.kind === "department" && group.source === "local");
    if (!department) {
      department = await expect("POST", "/api/admin/access/groups", 201, {
        name: `QA Folder Sync Department ${Date.now()}`, kind: "department",
      });
    }
    if (department.kind !== "department" || department.source !== "local") throw new Error("Synthetic department setup returned an unexpected scope.");
    await navigate("Settings", "#folder-sync-title");
    const formReady = await waitFor(`document.querySelectorAll('.folder-sync-form label').length===5`);
    if (!formReady) throw new Error("Folder-sync creation form was not visible in Admin Settings.");
    if (!await waitFor(`(()=>[...document.querySelectorAll('.folder-sync-form label:nth-of-type(4) option')].some(option=>option.value===${JSON.stringify(String(department.id))}))()`)) {
      throw new Error("The Admin department selector did not finish loading its local group options.");
    }
    const filled = await evaluate(`(()=>{const form=document.querySelector('.folder-sync-form');if(!form)return false;const labels=[...form.querySelectorAll('label')];const inputs=labels.map(label=>label.querySelector('input,select'));if(inputs.length!==5||inputs.some(item=>!item))return false;const set=(element,value)=>{const proto=element instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(element,value);element.dispatchEvent(new Event(element instanceof HTMLSelectElement?'change':'input',{bubbles:true}))};set(inputs[0],${JSON.stringify(sourceName)});set(inputs[1],${JSON.stringify(relativePath)});set(inputs[2],'restricted');set(inputs[3],${JSON.stringify(String(department.id))});set(inputs[4],'5');return true})()`);
    if (!filled) throw new Error("Admin folder-sync form fields could not be filled.");
    const formState = await evaluate(`(()=>{const form=document.querySelector('.folder-sync-form');const button=form?.querySelector('button[type="submit"]');return {disabled:button?.disabled??true,fieldCount:form?.querySelectorAll('label').length??0,departmentOptions:form?.querySelectorAll('label:nth-of-type(4) option').length??0,departmentSelected:Boolean(form?.querySelectorAll('label:nth-of-type(4) select')[0]?.value),sourceNameSet:Boolean(form?.querySelector('label:nth-of-type(1) input')?.value),pathSet:Boolean(form?.querySelector('label:nth-of-type(2) input')?.value)}})()`);
    if (formState?.disabled) throw new Error(`Admin folder-sync form was not ready (fields=${formState.fieldCount}, department_options=${formState.departmentOptions}, department_selected=${formState.departmentSelected}, name=${formState.sourceNameSet}, path=${formState.pathSet}).`);
    const created = await evaluate(`(()=>{const button=document.querySelector('.folder-sync-form button[type="submit"]');if(!button||button.disabled)return false;button.click();return true})()`);
    if (!created) throw new Error("Admin folder-sync submit action was unavailable.");
    if (!await waitFor(`document.body.innerText.includes('Scheduled folder source created')`)) {
      const uiError = await evaluate(`(()=>[...document.querySelectorAll('[role="alert"]')].map(item=>item.textContent.trim()).filter(Boolean).join(' ').slice(0,100))()`);
      const sourceCount = (await request("GET", "/api/folder-sync/sources")).json?.items?.length ?? -1;
      throw new Error(`Admin source creation did not complete (sources=${sourceCount}, visible_error=${uiError || "none"}).`);
    }
    const source = (await expect("GET", "/api/folder-sync/sources", 200)).items.find((item) => item.name === sourceName);
    if (!source || source.relative_path !== relativePath || source.visibility !== "restricted" || source.department_group_id !== department.id) {
      throw new Error("The persisted source did not retain the expected restricted department scope.");
    }
    evidence.push("F08_ADMIN_UI_RESTRICTED_SOURCE_CREATE=PASS");

    const firstRun = await waitForRun(source.id, [], (run) => run.status === "completed" && run.scanned === 1 && run.created === 1);
    await sourceRowAction("Run history");
    if (!await waitFor(`document.querySelector('.folder-sync-history')?.innerText.includes('added 1')`)) throw new Error("Admin source run history did not show the worker result.");
    evidence.push(`F08_WORKER_IMPORT_AND_ADMIN_HISTORY=PASS(${firstRun.scanned},${firstRun.created},${firstRun.failed})`);

    await navigate("My work", 'section[aria-label="Folder synchronization updates"]');
    if (!await waitFor(`document.querySelector('section[aria-label="Folder synchronization updates"]')?.innerText.includes(${JSON.stringify(sourceName)})`)) {
      throw new Error("The Admin owner did not receive the folder change notification in My work.");
    }
    const readClicked = await evaluate(`(()=>{const section=document.querySelector('section[aria-label="Folder synchronization updates"]');const item=[...(section?.querySelectorAll('li')??[])].find(row=>row.textContent.includes(${JSON.stringify(sourceName)}));const button=[...(item?.querySelectorAll('button')??[])].find(node=>node.textContent.trim()==='Mark read');if(!button)return false;button.click();return true})()`);
    if (!readClicked || !await waitFor(`(async()=>{const response=await fetch('/api/folder-sync/notifications',{credentials:'same-origin'});if(!response.ok)return false;const body=await response.json();return body.items.some(item=>item.source_name===${JSON.stringify(sourceName)}&&item.read_at!==null)})()`)) {
      throw new Error("The visible notification did not persist its read receipt.");
    }
    const prefChanged = await evaluate(`(()=>{const section=document.querySelector('section[aria-label="Folder synchronization updates"]');const input=section?.querySelector('input[type="checkbox"]');if(!input||!input.checked)return false;input.click();return true})()`);
    if (!prefChanged || !await waitFor(`(async()=>{const response=await fetch('/api/folder-sync/preferences',{credentials:'same-origin'});if(!response.ok)return false;const body=await response.json();return body.change_alerts_enabled===false})()`)) {
      throw new Error("The visible per-user alert preference did not persist.");
    }
    if (!await waitFor(`document.querySelector('section[aria-label="Folder synchronization updates"] input[type="checkbox"]')?.checked===false`)) throw new Error("The UI did not reflect the saved alert preference.");
    await evaluate(`document.querySelector('section[aria-label="Folder synchronization updates"] input[type="checkbox"]')?.click()`);
    if (!await waitFor(`(async()=>{const response=await fetch('/api/folder-sync/preferences',{credentials:'same-origin'});if(!response.ok)return false;return (await response.json()).change_alerts_enabled===true})()`)) throw new Error("The alert preference could not be restored to enabled.");
    evidence.push("F08_ADMIN_NOTIFICATION_READ_AND_PREFERENCE=PASS");

    await signOut();
    for (const role of ["manager", "employee"]) {
      await signIn(role);
      if (await evaluate(`Boolean([...document.querySelectorAll('button.nav-item')].some(item=>item.textContent.trim()==='Settings'))`)) {
        throw new Error(`${role} was shown the Admin Settings navigation.`);
      }
      if ((await request("GET", "/api/folder-sync/sources")).status !== 403) throw new Error(`${role} could read Admin folder-source schedules.`);
      const notices = await expect("GET", "/api/folder-sync/notifications", 200);
      if (notices.items.length !== 0) throw new Error(`${role} received another user's folder-source notification.`);
      await navigate("My work", 'section[aria-label="Folder synchronization updates"]');
      const toggled = await evaluate(`(()=>{const input=document.querySelector('section[aria-label="Folder synchronization updates"] input[type="checkbox"]');if(!input||!input.checked)return false;input.click();return true})()`);
      if (!toggled || !await waitFor(`(async()=>{const response=await fetch('/api/folder-sync/preferences',{credentials:'same-origin'});return response.ok&&(await response.json()).change_alerts_enabled===false})()`)) {
        throw new Error(`${role} personal alert preference could not be changed through My work.`);
      }
      if (!await waitFor(`document.querySelector('section[aria-label="Folder synchronization updates"] input[type="checkbox"]')?.checked===false`)) throw new Error(`${role} alert preference UI did not update.`);
      await evaluate(`document.querySelector('section[aria-label="Folder synchronization updates"] input[type="checkbox"]')?.click()`);
      if (!await waitFor(`(async()=>{const response=await fetch('/api/folder-sync/preferences',{credentials:'same-origin'});return response.ok&&(await response.json()).change_alerts_enabled===true})()`)) throw new Error(`${role} personal alert preference could not be restored.`);
      await signOut();
      evidence.push(`F08_${role.toUpperCase()}_ADMIN_BOUNDARY_AND_PRIVATE_ALERTS=PASS`);
    }

    await signIn("admin");
    await navigate("Settings", "#folder-sync-title");
    let beforeRuns = (await expect("GET", `/api/folder-sync/sources/${source.id}/runs`, 200)).items;
    await sourceRowAction("Pause");
    if (!await sourceState(source.id, false)) throw new Error("Admin Pause action did not disable the source.");
    unlinkSync(fixtureFile);
    await sourceRowAction("Resume");
    if (!await sourceState(source.id, true)) throw new Error("Admin Resume action did not enable the source.");
    const removalRun = await waitForRun(source.id, beforeRuns.map((run) => run.id), (run) => run.status === "completed" && run.removed === 1);
    evidence.push(`F08_REMOVAL_QUARANTINE=PASS(${removalRun.removed})`);

    writeFileSync(fixtureFile, syntheticText, { flag: "wx" });
    beforeRuns = (await expect("GET", `/api/folder-sync/sources/${source.id}/runs`, 200)).items;
    await sourceRowAction("Pause");
    if (!await sourceState(source.id, false)) throw new Error("Admin could not pause before source restoration.");
    await sourceRowAction("Resume");
    if (!await sourceState(source.id, true)) throw new Error("Admin could not resume after source restoration.");
    const restoreRun = await waitForRun(source.id, beforeRuns.map((run) => run.id), (run) => run.status === "completed" && run.scanned === 1 && run.removed === 0);
    evidence.push(`F08_RESTORED_SOURCE_REACTIVATION=PASS(${restoreRun.unchanged})`);

    beforeRuns = (await expect("GET", `/api/folder-sync/sources/${source.id}/runs`, 200)).items;
    await sourceRowAction("Pause");
    if (!await sourceState(source.id, false)) throw new Error("Admin could not pause before retry recovery.");
    renamedDirectory = `${testDirectory}.temporarily-unavailable`;
    renameSync(testDirectory, renamedDirectory);
    await sourceRowAction("Resume");
    if (!await sourceState(source.id, true)) throw new Error("Admin could not resume for retry recovery.");
    const failedRun = await waitForRun(source.id, beforeRuns.map((run) => run.id), (run) => run.status === "failed" && run.error_code === "InvalidInputError");
    renameSync(renamedDirectory, testDirectory);
    renamedDirectory = undefined;
    evidence.push(`F08_RETRY_FAILURE_RECORDED=PASS(${failedRun.attempt},${failedRun.error_code})`);

    beforeRuns = (await expect("GET", `/api/folder-sync/sources/${source.id}/runs`, 200)).items;
    await sourceRowAction("Pause");
    if (!await sourceState(source.id, false)) throw new Error("Admin could not pause before retry recovery.");
    await sourceRowAction("Resume");
    if (!await sourceState(source.id, true)) throw new Error("Admin could not re-enable the recovered source.");
    const recoveryRun = await waitForRun(source.id, beforeRuns.map((run) => run.id), (run) => run.status === "completed" && run.scanned === 1);
    const recoveredSource = (await expect("GET", "/api/folder-sync/sources", 200)).items.find((item) => item.id === source.id);
    if (!recoveredSource || recoveredSource.consecutive_failures !== 0 || recoveredSource.last_error_code !== null) {
      throw new Error("The source error state did not reset after a successful retry.");
    }
    if (new Set([firstRun.id, removalRun.id, restoreRun.id, failedRun.id, recoveryRun.id]).size !== 5) {
      throw new Error("The acceptance did not record five distinct persisted sync runs.");
    }
    evidence.push(`F08_RETRY_RECOVERY=PASS(${recoveryRun.status},0)`);
    await signOut();
  } finally {
    if (renamedDirectory && existsSync(renamedDirectory) && !existsSync(testDirectory)) renameSync(renamedDirectory, testDirectory);
    cdp?.socket.close();
    if (browser?.exitCode === null) {
      browser.kill();
      await Promise.race([new Promise((resolve) => browser.once("exit", resolve)), delay(3_000)]);
    }
  }
}

try {
  await runBrowserAcceptance();
  for (const item of evidence) console.log(item);
  console.log("F08_BROWSER_ACCEPTANCE=PASS");
} catch (error) {
  for (const item of evidence) console.log(item);
  console.error(`F08_BROWSER_ACCEPTANCE=FAIL ${error?.message ?? "unknown failure"}`);
  process.exitCode = 1;
}
