#!/usr/bin/env node
/** Current-source browser/API acceptance for department service-request workflow triggers. */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_F14_CURRENT_URL ?? "http://127.0.0.1:5179/admin/");
const fixturePath = process.env.INFRASPINE_F14_FIXTURE;
const reviewRoot = process.env.INFRASPINE_F14_REVIEW_ROOT;
if (base.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(base.hostname)) throw new Error("F14 browser acceptance is restricted to loopback HTTP.");
if (!fixturePath || !reviewRoot || !existsSync(fixturePath)) throw new Error("The protected synthetic F14 fixture is unavailable.");
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const employeeAccounts = Array.isArray(fixture.employees) ? fixture.employees : fixture.employee ? [fixture.employee] : [];
if (typeof fixture.admin?.email !== "string" || typeof fixture.admin?.password !== "string" ||
    !employeeAccounts.some((item) => typeof item?.email === "string" && typeof item?.password === "string")) {
  throw new Error("The protected synthetic Admin/Employee fixture is incomplete.");
}
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((candidate) => candidate && existsSync(candidate));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for local F14 acceptance.");

const stamp = new Date().toISOString().replace(/[-:.]/g, "").replace("T", "-").replace("Z", "");
const workflowPrefix = "QA help-request workflow ";
let workflowName = `${workflowPrefix}${stamp}`;
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-f14-current-"));
const evidenceRoot = path.join(reviewRoot, "evidence");
mkdirSync(evidenceRoot, { recursive: true });
let browser; let socket; let sequence = 0;
const pending = new Map();

function call(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser acceptance timed out during ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method }); socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression, timeoutMs = 20_000) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error("A current-source F14 browser action failed.");
  return result.result?.value;
}

async function waitFor(expression, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) { if (await evaluate(expression)) return true; await delay(200); }
  return false;
}

async function request(method, route, body) {
  return await evaluate(`(async()=>{const init={method:${JSON.stringify(method)},credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'XMLHttpRequest'}};const body=${body === undefined ? "undefined" : JSON.stringify(body)};if(body!==undefined){init.headers['Content-Type']='application/json';init.body=JSON.stringify(body)}const r=await fetch(${JSON.stringify(route)},init);let json=null;try{json=await r.json()}catch{}return {status:r.status,json}})()`);
}

async function signIn(account, role) {
  await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const password=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!password||!form)return false;const set=(el,value)=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))};set(email,${JSON.stringify(account.email)});set(password,${JSON.stringify(account.password)});form.requestSubmit();return true})()`);
  if (!await waitFor(`(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;return (await r.json()).role===${JSON.stringify(role)}})()`, 40_000)) throw new Error(`Synthetic ${role} authentication did not complete.`);
}

async function signOut() {
  const clicked = await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Sign out"]');button?.click();return !!button})()`);
  if (!clicked || !await waitFor(`Boolean(document.querySelector('form.login-form'))`)) throw new Error("The local QA browser could not sign out.");
}

async function openRoute(hash, title) {
  await evaluate(`window.location.hash=${JSON.stringify(hash)}`);
  if (!await waitFor(`Boolean([...document.querySelectorAll('h1')].some(item=>item.textContent.trim()===${JSON.stringify(title)}))`)) throw new Error(`The current-source ${title} page did not render.`);
  if (!await waitFor(`!document.querySelector('.loading')`)) throw new Error(`The current-source ${title} data did not finish loading.`);
}

async function setValue(selector, value) {
  const set = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:el instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!set) throw new Error("A required visible workflow/request form field was unavailable.");
}

async function submitRequest(category, departmentId, subject) {
  if (!await waitFor(`!document.querySelector('.service-request-form button')?.disabled || !document.querySelector('.service-request-form input')?.value`)) throw new Error("Request form remained busy.");
  await delay(500);
  await setValue(".service-request-form label:nth-of-type(1) select", category);
  await setValue(".service-request-form label:nth-of-type(2) select", "normal");
  await setValue(".service-request-form label:nth-of-type(3) select", departmentId ?? "");
  await setValue(".service-request-form > label:nth-of-type(1) input", subject);
  await setValue(".service-request-form > label:nth-of-type(2) textarea", "Synthetic F14 event-trigger acceptance. Contains no customer content or secrets.");
  if (!await waitFor(`Boolean(document.querySelector('.service-request-form button:not(:disabled)'))`)) throw new Error("Request submit did not become enabled.");
  const clicked = await evaluate(`(()=>{const button=document.querySelector('.service-request-form button');if(!button||button.disabled)return false;button.click();return true})()`);
  if (!clicked) throw new Error("The visible service-request submit control was not available.");
  const deadline = Date.now() + 20_000;
  let result;
  while (Date.now() < deadline) {
    result = await request("GET", "/api/service-requests");
    if (result.status !== 200) throw new Error("The authenticated service-request API did not return HTTP 200.");
    const found = result.json?.items?.find((item) => item.subject === subject);
    if (found) return found;
    await delay(200);
  }
  throw new Error("The synthetic service request did not persist through the visible form.");
}

async function screenshot(name) {
  const shot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  const file = path.join(evidenceRoot, `${name}-${stamp}.png`);
  writeFileSync(file, Buffer.from(shot.data, "base64"), { flag: "wx" });
  return file;
}


async function sqlDue(ids) {
  if (!ids.every(id=>/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(id))) throw new Error('Invalid synthetic request ID');
  const query = `UPDATE service_requests SET response_due_at=now()-interval '1 second' WHERE id IN (${ids.map(id=>`'${id}'::uuid`).join(',')})`;
  const proc=spawn('C:\\Program Files\\PostgreSQL\\18\\bin\\psql.exe',['postgresql://postgres@127.0.0.1:55888/infraspine_f14browser_27b1_test','-X','-v','ON_ERROR_STOP=1','-c',query],{stdio:'ignore',windowsHide:true});
  const code=await new Promise(resolve=>proc.once('exit',resolve)); if(code!==0)throw new Error('Synthetic due-time setup failed');
}
async function run() {
 const childEnv={SYSTEMROOT:process.env.SYSTEMROOT,PATH:process.env.PATH,TEMP:process.env.TEMP,TMP:process.env.TMP,USERPROFILE:process.env.USERPROFILE,LOCALAPPDATA:process.env.LOCALAPPDATA};
 browser=spawn(browserPath,['--headless=new','--disable-gpu','--no-first-run','--disable-extensions','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,base.href],{stdio:'ignore',windowsHide:true,env:childEnv});
 const activePort=path.join(profile,'DevToolsActivePort'); const deadline=Date.now()+30000;
 while(!existsSync(activePort)&&Date.now()<deadline)await delay(100); if(!existsSync(activePort))throw new Error('Browser startup failed');
 const port=Number(readFileSync(activePort,'utf8').split(/\r?\n/)[0]);let page;
 while(!page&&Date.now()<deadline){try{let r=await fetch(`http://127.0.0.1:${port}/json/list`);page=(await r.json()).find(x=>x.type==='page'&&x.webSocketDebuggerUrl)}catch{}if(!page)await delay(150)}
 socket=new WebSocket(page.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true})});
 socket.addEventListener('message',event=>{const msg=JSON.parse(event.data);if(!msg.id||!pending.has(msg.id))return;const item=pending.get(msg.id);clearTimeout(item.timer);pending.delete(msg.id);if(msg.error)item.reject(new Error('Browser protocol failed'));else item.resolve(msg.result)});
 await call('Page.enable');await call('Runtime.enable');await call('Page.navigate',{url:base.href});
 if(!await waitFor(`Boolean(document.querySelector('form.login-form input[name="email"]'))`,45000))throw new Error('Login unavailable');
 await signIn(fixture.admin,'admin');
 const setup=await request('PUT','/api/admin/deployment/company',{company_name:'F14 SLA Acceptance',industry:'Services',brand_name:'F14 SLA',primary_color:'#146B61',setup_complete:true});if(setup.status!==200)throw new Error('Company setup failed');
 await evaluate('location.reload()');await delay(1500);await openRoute('/automation','Workflows & tasks');
 const groups=await request('GET','/api/admin/access/groups');const users=await request('GET','/api/admin/users');
 const department=groups.json?.groups?.find(x=>x.name==='Synthetic F14 Operations');const employee=users.json?.users?.find(x=>x.email===employeeAccounts[0].email);
 if(!department||!employee)throw new Error('Synthetic department or employee missing');
 await setValue('.automation-form label:nth-of-type(1) input',workflowName);await setValue('.automation-form label:nth-of-type(2) select',department.id);
 if(!await waitFor(`Boolean([...document.querySelectorAll('.automation-form label:nth-of-type(4) select option')].find(x=>x.value===${JSON.stringify(employee.id)}))`))throw new Error('Assignee absent');
 await setValue('.automation-form label:nth-of-type(3) select','request_sla');await setValue('.automation-form label:nth-of-type(4) select',employee.id);
 await setValue('.automation-form label:nth-of-type(5) input','Review missed response target');await setValue('.automation-form textarea','Review the synthetic escalation and record next steps.');
 await evaluate(`[...document.querySelectorAll('.automation-form button')].find(x=>x.textContent.trim()==='Create workflow').click()`);
 if(!await waitFor(`[...document.querySelectorAll('.automation-item h3')].some(x=>x.textContent===${JSON.stringify(workflowName)})`))throw new Error('Workflow UI save failed');
 const workflows=await request('GET','/api/automation/workflows');const workflow=workflows.json?.workflows?.find(x=>x.name===workflowName);if(workflow?.trigger_type!=='request_sla')throw new Error('SLA trigger not persisted');
 const dry=await request('POST',`/api/automation/workflows/${workflow.id}/dry-run`,{});if(!dry.json?.valid||dry.json?.nodes?.[0]?.title!=='Department help request response target missed'||dry.json?.side_effects_performed!==false)throw new Error('Dry run failed');
 console.log('F14_SLA_UI_AND_DRY_RUN=PASS');
 await signOut();await signIn(employeeAccounts[0],'employee');await openRoute('/requests','Help & requests');
 const hr=await submitRequest('hr',department.id,`F14 SLA synthetic HR ${stamp}`);const unscoped=await submitRequest('access',null,`F14 SLA synthetic unscoped ${stamp}`);const scoped=await submitRequest('access',department.id,`F14 SLA synthetic scoped ${stamp}`);
 let runs=await request('GET','/api/automation/runs');if(runs.json?.runs?.some(x=>x.workflow_id===workflow.id))throw new Error('Run created before deadline');
 await sqlDue([hr.id,unscoped.id,scoped.id]);let matching=[];const stop=Date.now()+70000;
 while(Date.now()<stop){runs=await request('GET','/api/automation/runs');matching=(runs.json?.runs??[]).filter(x=>x.workflow_id===workflow.id);if(matching.length)break;await delay(500)}
 if(hr.department_group_id!==department.id||unscoped.department_group_id!==null||scoped.department_group_id!==department.id)throw new Error('Synthetic request scopes incorrect');
 if(matching.length!==1||matching[0].status!=='pending_approval')throw new Error('Scoped SLA escalation did not create exactly one pending run');const run=matching[0];
 let denied=await request('POST',`/api/automation/runs/${run.id}/decision`,{approved:true,rationale:'Self approval is prohibited'});if(denied.status!==403)throw new Error('Requester approval not denied');
 let tasks=await request('GET','/api/automation/tasks');if(tasks.json?.tasks?.some(x=>x.run_id===run.id))throw new Error('Task before approval');
 await delay(8000);runs=await request('GET','/api/automation/runs');if(runs.json.runs.filter(x=>x.workflow_id===workflow.id).length!==1)throw new Error('Duplicate run');
 console.log('F14_SLA_DEADLINE_SCOPE_DUPLICATE_AND_DENIAL=PASS');
 await signOut();await signIn(fixture.admin,'admin');await openRoute('/automation','Workflows & tasks');
 let notices=await request('GET','/api/automation/notifications');if(!notices.json?.items?.some(x=>x.run_id===run.id&&x.notification_type==='approval_required'))throw new Error('Approval notification missing');
 const removed=await request('DELETE',`/api/admin/access/groups/${department.id}/members/${employee.id}`);if(removed.status!==204)throw new Error('Cannot induce membership retry');
 if(!await waitFor(`[...document.querySelectorAll('.automation-item')].some(x=>x.querySelector('h3')?.textContent===${JSON.stringify(workflowName)}&&x.innerText.includes('pending approval')&&x.querySelector('.automation-approval'))`))throw new Error('Approval UI missing');
 await evaluate(`(()=>{const a=[...document.querySelectorAll('.automation-item')].find(x=>x.querySelector('h3')?.textContent===${JSON.stringify(workflowName)}&&x.querySelector('.automation-approval'));const t=a.querySelector('textarea');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(t,'Independent synthetic SLA review');t.dispatchEvent(new Event('input',{bubbles:true}));t.dispatchEvent(new Event('change',{bubbles:true}))})()`);
 await waitFor(`[...document.querySelectorAll('.automation-approval button')].some(x=>x.textContent.trim()==='Approve'&&!x.disabled)`);
 await evaluate(`(()=>{const a=[...document.querySelectorAll('.automation-item')].find(x=>x.querySelector('h3')?.textContent===${JSON.stringify(workflowName)}&&x.querySelector('.automation-approval'));[...a.querySelectorAll('button')].find(x=>x.textContent.trim()==='Approve').click()})()`);
 let failed;const failureDeadline=Date.now()+25000;while(Date.now()<failureDeadline){runs=await request('GET','/api/automation/runs');failed=runs.json?.runs?.find(x=>x.id===run.id);if(failed?.attempts>=1&&failed.last_error_code==='assignee_membership_revoked')break;await delay(400)}
 if(failed?.last_error_code!=='assignee_membership_revoked'||failed.status!=='queued')throw new Error('Worker retry was not observed');
 const restored=await request('POST',`/api/admin/access/groups/${department.id}/members`,{email:employee.email});if(restored.status!==204)throw new Error('Cannot restore synthetic membership');
 const completeDeadline=Date.now()+45000;let completed;while(Date.now()<completeDeadline){runs=await request('GET','/api/automation/runs');completed=runs.json?.runs?.find(x=>x.id===run.id);if(completed?.status==='completed')break;await delay(500)}
 if(completed?.status!=='completed'||completed.attempts<2)throw new Error('Worker retry did not complete');
 tasks=await request('GET','/api/automation/tasks');if(tasks.json?.tasks?.filter(x=>x.run_id===run.id).length!==1)throw new Error('Task duplication or missing');
 console.log('F14_SLA_APPROVAL_WORKER_RETRY_SINGLE_TASK=PASS');
 await signOut();await signIn(employeeAccounts[0],'employee');notices=await request('GET','/api/automation/notifications');if(!notices.json?.items?.some(x=>x.run_id===run.id&&x.notification_type==='task_assigned'))throw new Error('Task notification missing');
 await openRoute('/automation','Workflows & tasks');if(!await waitFor(`document.body.innerText.includes('Review missed response target')`))throw new Error('Task UI missing');
 await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});const fits=await evaluate('document.documentElement.scrollWidth<=window.innerWidth');if(!fits)throw new Error('Mobile overflow');
 const shot=await screenshot('sla-task-390');console.log('F14_SLA_TASK_NOTIFICATION_AND_390PX=PASS');console.log('F14_SLA_BROWSER_ACCEPTANCE=PASS '+shot);
}
try{await run()}catch(error){console.error('F14_SLA_BROWSER_ACCEPTANCE=FAIL '+(error?.message??'unknown'));process.exitCode=1}finally{try{socket?.close()}catch{}if(browser?.exitCode===null){browser.kill();await Promise.race([new Promise(resolve=>browser.once('exit',resolve)),delay(5000)])}try{rmSync(profile,{recursive:true,force:true})}catch{}}
