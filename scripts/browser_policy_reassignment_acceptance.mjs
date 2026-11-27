#!/usr/bin/env node
/** Current-source policy exception reassignment UI acceptance with a synthetic in-page API. */
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
    const ownerOne={user_id:"owner-one",email:"owner.one@example.test",role:"manager"};
    const ownerTwo={user_id:"owner-two",email:"owner.two@example.test",role:"employee"};
    const makeException=(id,entryId,owner)=>({id,process_id:"process-1",process_name:"Access review",entry_id:entryId,revision_id:"revision-1",
      policy_statement:id==="exception-one"?"First exception requiring a new owner":"Second exception with no alternate owner",status:"open",
      assigned_to:owner,created_by_email:"reviewer@example.test",rationale:"Document the required remediation.",due_at:null,created_at:new Date().toISOString(),events:[],
      can_acknowledge:false,can_resolve:false,can_reassign:true});
    const state=window.__policyReassignment={calls:[],exceptions:[makeException("exception-one","entry-one",ownerOne),makeException("exception-two","entry-two",ownerTwo)]};
    const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{"Content-Type":"application/json"}});
    const originalFetch=window.fetch.bind(window);
    window.fetch=async(input,init={})=>{
      const url=new URL(typeof input==="string"?input:input.url,location.href);
      if(!url.pathname.startsWith("/api/"))return originalFetch(input,init);
      const method=String(init.method??(typeof input==="string"?"GET":input.method)??"GET").toUpperCase();
      const body=typeof init.body==="string"?JSON.parse(init.body):null;
      state.calls.push({method,path:url.pathname,search:url.search,body});
      if(method==="GET"&&url.pathname==="/api/auth/me")return json({id:"manager-1",email:"manager@example.test",role:"manager"});
      if(method==="GET"&&url.pathname==="/api/deployment/profile")return json({company_name:"Synthetic Company",industry:"Testing",brand_name:"Synthetic QA",primary_color:"#1765ad",setup_complete:true,template_id:null,template_version:null});
      if(method==="GET"&&url.pathname==="/api/governance/claims")return json({items:[]});
      if(method==="GET"&&url.pathname==="/api/governance/knowledge")return json({items:[]});
      if(method==="GET"&&url.pathname==="/api/governance/knowledge/stale")return json({items:[]});
      if(method==="GET"&&url.pathname==="/api/governance/knowledge/conflicts")return json({items:[],next_cursor:null});
      if(method==="GET"&&url.pathname==="/api/governance/policy-candidates")return json({items:[]});
      if(method==="GET"&&url.pathname==="/api/governance/processes")return json({items:[]});
      if(method==="GET"&&url.pathname==="/api/governance/policy-exceptions")return json({items:state.exceptions});
      if(method==="GET"&&url.pathname==="/api/governance/processes/process-1/assignees"){
        return url.searchParams.get("entry_id")==="entry-one"?json({items:[ownerOne,ownerTwo]}):json({items:[ownerTwo]});
      }
      if(method==="POST"&&url.pathname==="/api/governance/policy-exceptions/exception-one/actions"&&body?.action==="reassign"){
        const event={action:"reassigned",actor_email:"manager@example.test",from_assignee_email:ownerOne.email,to_assignee_email:ownerTwo.email,
          from_status:"open",to_status:"open",rationale:body.rationale,created_at:new Date().toISOString()};
        state.exceptions=state.exceptions.map((item)=>item.id==="exception-one"?{...item,assigned_to:ownerTwo,events:[...item.events,event]}:item);
        return json(state.exceptions[0]);
      }
      return json({error:{code:"unmocked_route",message:"The policy UI called an unmocked endpoint."}},501);
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

  profile = mkdtempSync(path.join(tmpdir(), "infraspine-policy-reassignment-ui-"));
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
  await call("Page.navigate",{url:`${url}#/approvals`});
  if(!await waitFor(`document.querySelector('h1')?.textContent.includes('Approvals')`))throw new Error("Approvals page did not render.");
  if(!await waitFor(`document.querySelector('h3')?.textContent.includes('Assigned policy actions')&&document.querySelectorAll('.approval-card').length>=2`))throw new Error("Synthetic policy exceptions did not load.");

  const firstCardSelector=".approval-list > .approval-card:nth-child(1)";
  const firstCard=`document.querySelector(${JSON.stringify(firstCardSelector)})`;
  await evaluate(`(()=>{const card=${firstCard};card.querySelector('button')?.click();})()`);
  if(!await waitFor(`document.querySelector('label.field select')?.options.length===2`))throw new Error("Different in-scope assignee did not load.");
  const fill=(selector,value)=>`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value').set;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`;
  const firstSelect=`${firstCardSelector} label.field select`;
  if(!await evaluate(fill(firstSelect,"owner-two")))throw new Error("Reassignment selector was missing.");
  const firstRationale=`${firstCardSelector} textarea`;
  if(!await evaluate(fill(firstRationale,"Hand this remediation to the alternate active department owner.")))throw new Error("Reassignment rationale field was missing.");
  await evaluate(`(()=>{const card=${firstCard};[...card.querySelectorAll('button')].find(button=>button.textContent.includes('Reassign'))?.click();})()`);
  if(!await waitFor(`document.querySelector('.approval-card')?.textContent.includes('Assigned to owner.two@example.test')&&document.querySelector('.approval-card')?.textContent.includes('reassigned')`))throw new Error("Successful reassignment was not reflected in the history UI.");
  const posted=await evaluate(`window.__policyReassignment?.calls.find(call=>call.method==='POST'&&call.path.endsWith('/exception-one/actions'))`);
  if(posted?.body?.action!=="reassign"||posted.body.assigned_to!=="owner-two"||posted.body.rationale.length<3)throw new Error("Reassignment did not submit the alternate owner and rationale.");
  console.log("F17_POLICY_REASSIGNMENT_UI=PASS");

  const secondCardSelector=".approval-list > .approval-card:nth-child(2)";
  const secondCard=`document.querySelector(${JSON.stringify(secondCardSelector)})`;
  await evaluate(`(()=>{const card=${secondCard};card.querySelector('button')?.click();})()`);
  if(!await waitFor(`document.querySelector(${JSON.stringify(secondCardSelector)})?.textContent.includes('No other active in-scope assignee is available.')`))throw new Error("No-alternative state was not shown.");
  const layout=await evaluate(`({viewport:innerWidth,width:document.documentElement.scrollWidth})`);
  if(layout.viewport!==390||layout.width>390)throw new Error(`Policy reassignment UI overflows at 390px: ${JSON.stringify(layout)}`);
  console.log("F17_POLICY_NO_ALTERNATIVE_AND_390PX=PASS");
  console.log("F17_CURRENT_SOURCE_POLICY_REASSIGNMENT_BROWSER_ACCEPTANCE=PASS (synthetic in-page API; PostgreSQL authorization tests remain separate)");
}

run().catch((error)=>{console.error(`F17_POLICY_REASSIGNMENT_UI=FAIL ${error?.message??"unknown failure"}`);process.exitCode=1;}).finally(async()=>{
  try{socket?.close();}catch{}
  for(const child of [browser,vite]){if(child&&child.exitCode===null){child.kill();await new Promise((resolve)=>{const timer=setTimeout(resolve,2000);child.once("exit",()=>{clearTimeout(timer);resolve();});});}}
  if(profile&&existsSync(profile))rmSync(profile,{recursive:true,force:true});
});
