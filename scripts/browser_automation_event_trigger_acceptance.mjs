#!/usr/bin/env node
/** Current-source event-trigger workflow UI acceptance with a synthetic in-page API. */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { createServer } from "node:net";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const frontend = path.join(repo, "frontend");
const viteEntry = path.join(frontend, "node_modules", "vite", "bin", "vite.js");
const browserCandidates = process.platform === "win32"
  ? ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"), path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe")]
  : ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/microsoft-edge"];
const browserPath = browserCandidates.find((candidate) => candidate && existsSync(candidate));
if (!existsSync(viteEntry)) throw new Error("Frontend dependencies are not installed; Vite was not found.");
if (!browserPath) throw new Error("Chrome, Chromium or Edge was not found.");

let browser; let vite; let socket; let profile; let port; let sequence = 0;
const pending = new Map();

async function freePort() {
  const server = createServer();
  await new Promise((resolve, reject) => server.once("error", reject).listen(0, "127.0.0.1", resolve));
  const value = server.address().port;
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  return value;
}

function call(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser command timed out: ${method}`)); }, 20_000);
    pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error("Browser action raised an exception.");
  return result.result?.value;
}

async function waitFor(expression, timeout = 20_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { if (await evaluate(expression)) return true; await delay(150); }
  return false;
}

function mockApiScript() {
  return `(() => {
    const group={id:"department-1",name:"Support Operations",kind:"department",source:"local",external_id:null,member_count:1,created_at:new Date().toISOString()};
    const member={user_id:"employee-1",email:"employee@example.test"};
    const state=window.__automationEvent={calls:[],workflows:[],group,member};
    const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{"Content-Type":"application/json"}});
    const originalFetch=window.fetch.bind(window);
    window.fetch=async(input,init={})=>{
      const url=new URL(typeof input==="string"?input:input.url,location.href);
      if(!url.pathname.startsWith("/api/"))return originalFetch(input,init);
      const method=String(init.method??(typeof input==="string"?"GET":input.method)??"GET").toUpperCase();
      const body=typeof init.body==="string"?JSON.parse(init.body):null;
      state.calls.push({method,path:url.pathname,body:url.pathname==="/api/automation/workflows"&&method==="POST"?body:null,csrf:new Headers(init.headers).get("X-Requested-With")});
      if(method==="GET"&&url.pathname==="/api/auth/me")return json({id:"manager-1",email:"manager@example.test",role:"manager"});
      if(method==="GET"&&url.pathname==="/api/deployment/profile")return json({company_name:"Synthetic Company",industry:"Testing",brand_name:"Synthetic QA",primary_color:"#1765ad",setup_complete:true,template_id:null,template_version:null});
      if(method==="GET"&&url.pathname==="/api/departments/managed")return json({groups:[group]});
      if(method==="GET"&&url.pathname==="/api/departments/department-1/members")return json({members:[member]});
      if(method==="GET"&&url.pathname==="/api/automation/workflows")return json({workflows:state.workflows});
      if(method==="GET"&&url.pathname==="/api/automation/runs")return json({runs:[]});
      if(method==="GET"&&url.pathname==="/api/automation/tasks")return json({tasks:[]});
      if(method==="GET"&&url.pathname==="/api/automation/notifications")return json({items:[]});
      if(method==="POST"&&url.pathname==="/api/automation/workflows"){
        const workflow={id:"workflow-event-1",definition_key:"definition-1",version:1,name:body.name,department_group_id:group.id,department_name:group.name,
          trigger_type:body.trigger_type,interval_minutes:body.interval_minutes,assignee_user_id:body.assignee_user_id,task_title:body.task_title,task_body:body.task_body,
          due_days:body.due_days,enabled:true,next_run_at:null,created_at:new Date().toISOString(),can_manage:true};
        state.workflows.push(workflow);return json(workflow,201);
      }
      if(method==="POST"&&url.pathname==="/api/automation/workflows/workflow-event-1/dry-run")return json({
        workflow_id:"workflow-event-1",workflow_version:1,valid:true,side_effects_performed:false,
        nodes:[{id:"trigger",node_type:"trigger",title:"Department help request submitted",description:"Queues a request for separate approval."},
          {id:"approval",node_type:"approval",title:"Independent department approval",description:"A second person approves the request."},
          {id:"task",node_type:"action",title:"Create one internal task",description:"Runs only after approval."},
          {id:"completion",node_type:"completion",title:"Record completion",description:"Retains the run history."}],
        edges:[{source:"trigger",target:"approval"},{source:"approval",target:"task"},{source:"task",target:"completion"}],
        checks:[{code:"department_access",passed:true,summary:"Department access is current."},{code:"active_assignee",passed:true,summary:"Assignee is an active department member."},{code:"approval_gate",passed:true,summary:"A separate approval is required."},{code:"allowlisted_action",passed:true,summary:"Action is allowlisted."}],
        task_preview:{title:body?.task_title??"Review access membership",body:"Review the submitted department request.",assignee_email:member.email,due_days:5},
      });
      return json({error:{code:"unmocked_route",message:"The workflow UI called an unmocked endpoint."}},501);
    };
  })();`;
}

async function run() {
  port = await freePort();
  vite = spawn(process.execPath, [viteEntry, "--host", "127.0.0.1", "--port", String(port), "--strictPort"], { cwd: frontend, stdio: "inherit", windowsHide: true });
  const url = `http://127.0.0.1:${port}/admin/`;
  const ready = Date.now() + 30_000;
  while (Date.now() < ready && vite.exitCode === null) { try { if ((await fetch(url)).ok) break; } catch {} await delay(150); }
  if (vite.exitCode !== null) throw new Error("Vite exited before becoming ready.");

  profile = mkdtempSync(path.join(tmpdir(), "infraspine-automation-event-ui-"));
  const activePort = path.join(profile, "DevToolsActivePort");
  browser = spawn(browserPath, ["--headless=new","--disable-gpu","--no-first-run","--no-default-browser-check","--disable-extensions","--disable-background-networking","--disable-sync","--remote-debugging-port=0",`--user-data-dir=${profile}`,"about:blank"], { stdio:"ignore", windowsHide:true });
  const browserReady = Date.now() + 30_000;
  while (!existsSync(activePort) && Date.now() < browserReady && browser.exitCode === null) await delay(100);
  if (!existsSync(activePort)) throw new Error("Browser debugger did not start.");
  const debugPort=Number(readFileSync(activePort,"utf8").split(/\r?\n/)[0]);
  let target;
  while(!target&&Date.now()<browserReady){try{const response=await fetch(`http://127.0.0.1:${debugPort}/json/list`);if(response.ok)target=(await response.json()).find((item)=>item.type==="page"&&item.webSocketDebuggerUrl);}catch{}if(!target)await delay(150);}
  if(!target)throw new Error("Browser page target did not become ready.");
  socket=new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.addEventListener("open",resolve,{once:true});socket.addEventListener("error",()=>reject(new Error("Browser debugger connection failed.")),{once:true});});
  socket.addEventListener("message",(event)=>{const message=JSON.parse(event.data);if(!message.id||!pending.has(message.id))return;const entry=pending.get(message.id);clearTimeout(entry.timer);pending.delete(message.id);if(message.error)entry.reject(new Error("Browser command failed."));else entry.resolve(message.result);});
  await call("Page.enable"); await call("Runtime.enable");
  await call("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await call("Page.addScriptToEvaluateOnNewDocument",{source:mockApiScript()});
  await call("Page.navigate",{url:`${url}#/automation`});
  if(!await waitFor(`document.querySelector('h1')?.textContent.includes('Workflows & tasks')`))throw new Error("Workflows page did not render.");
  if(!await waitFor(`(()=>{const selects=[...document.querySelectorAll('.automation-form select')];return selects.length>=3&&selects[0].value==='department-1'&&[...selects[2].options].some(option=>option.value==='employee-1');})()`)){
    const readiness=await evaluate(`({selects:[...document.querySelectorAll('.automation-form select')].map(select=>({value:select.value,options:[...select.options].map(option=>option.value)})),error:document.querySelector('[role="alert"]')?.textContent,calls:window.__automationEvent?.calls.map(call=>call.path)})`);
    throw new Error(`Workflow department/member data did not load: ${JSON.stringify(readiness)}`);
  }

  const fill=(selector,value)=>`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value').set;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`;
  if(!await evaluate(fill('.automation-form label:nth-of-type(1) input',"Help request triage")))throw new Error("Workflow name field was missing.");
  if(!await evaluate(fill('.automation-form label:nth-of-type(3) select',"service_request")))throw new Error("Workflow trigger selector was missing.");
  if(!await waitFor(`document.querySelector('.automation-wide.small.muted')?.textContent.includes('HR and requests without a department are excluded')`))throw new Error("The event trigger privacy boundary was not shown.");
  if(!await evaluate(fill('.automation-form label:nth-of-type(4) select',"employee-1")))throw new Error("Workflow assignee selector was missing.");
  if(!await evaluate(fill('.automation-form label:nth-of-type(5) input',"Review submitted help request")))throw new Error("Workflow task title field was missing.");
  if(!await evaluate(fill('.automation-form textarea',"Review the submitted request and document next steps.")))throw new Error("Workflow task instructions field was missing.");
  await evaluate(`([...document.querySelectorAll('.automation-form button')].find(button=>button.textContent.includes('Create workflow'))?.click())`);
  if(!await waitFor(`document.querySelector('.automation-item-heading p')?.textContent.includes('On department help request')`))throw new Error("The event-trigger workflow was not listed after creation.");
  const post=await evaluate(`window.__automationEvent?.calls.find(call=>call.method==='POST'&&call.path==='/api/automation/workflows')`);
  if(post?.body?.trigger_type!=="service_request"||post.body.interval_minutes!==null)throw new Error(`Event trigger was not saved as a non-scheduled trigger: ${JSON.stringify(post)}`);
  const layout=await evaluate(`({viewport:innerWidth,width:document.documentElement.scrollWidth})`);
  if(layout.viewport!==390||layout.width>390)throw new Error(`Workflow event UI overflows at 390px: ${JSON.stringify(layout)}`);
  console.log("F14_EVENT_TRIGGER_CREATE_AND_PRIVACY_UI_390PX=PASS");

  await evaluate(`([...document.querySelectorAll('.automation-item button')].find(button=>button.textContent.includes('Validate flow'))?.click())`);
  if(!await waitFor(`document.querySelector('[aria-label="Workflow execution path"]')?.textContent.includes('Department help request submitted')`)){
    const dryRunState=await evaluate(`({calls:window.__automationEvent?.calls.map(call=>({method:call.method,path:call.path})),error:document.querySelector('[role="alert"]')?.textContent})`);
    throw new Error(`The event trigger dry-run diagram did not render: ${JSON.stringify(dryRunState)}`);
  }
  const calls=await evaluate(`window.__automationEvent?.calls??[]`);
  if(!calls.some(call=>call.method==='POST'&&call.path.endsWith('/dry-run'))||calls.some(call=>call.path.startsWith('/api/partner/')))throw new Error("The workflow dry-run did not use its expected internal API.");
  console.log("F14_EVENT_TRIGGER_DRY_RUN_UI=PASS");
  console.log("F14_CURRENT_SOURCE_AUTOMATION_BROWSER_ACCEPTANCE=PASS (synthetic in-page API; backend workflow integration tested separately)");
}

run().catch((error)=>{console.error(`F14_EVENT_TRIGGER_UI=FAIL ${error?.message??"unknown failure"}`);process.exitCode=1;}).finally(async()=>{
  try{socket?.close();}catch{}
  for(const child of [browser,vite]){if(child&&child.exitCode===null){child.kill();await new Promise((resolve)=>{const timer=setTimeout(resolve,2000);child.once("exit",()=>{clearTimeout(timer);resolve();});});}}
  if(profile&&existsSync(profile))rmSync(profile,{recursive:true,force:true});
});
