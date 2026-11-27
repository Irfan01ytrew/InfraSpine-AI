#!/usr/bin/env node
/** Current-source reviewed agent catalog UI acceptance with a synthetic in-page API. */
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
const browserPath = process.env.INFRASPINE_BROWSER_PATH || browserCandidates.find((candidate) => candidate && existsSync(candidate));
if (!existsSync(viteEntry)) throw new Error("Frontend dependencies are not installed; Vite was not found.");
if (!browserPath || !existsSync(browserPath)) throw new Error("Chrome, Chromium or Edge was not found.");

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
    pending.set(id, { resolve, reject, timer, method }); socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  let result;
  try {
    result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  } catch (error) {
    throw new Error(`Runtime.evaluate expression failed (${expression.slice(0, 110)}): ${error?.message ?? "unknown error"}`);
  }
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
    const key="__infraspine_f19_catalog_state";
    const seed={actor:"publisher",template:null,calls:[]};
    const state=window.__f19Catalog=JSON.parse(localStorage.getItem(key)||"null")||seed;
    state.calls=[];
    const users={
      publisher:{id:"publisher-1",email:"publisher@example.test",role:"manager"},
      reviewer:{id:"reviewer-1",email:"reviewer@example.test",role:"manager"},
      employee:{id:"employee-1",email:"employee@example.test",role:"employee"}
    };
    const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{"Content-Type":"application/json"}});
    const originalFetch=window.fetch.bind(window);
    const save=()=>localStorage.setItem(key,JSON.stringify(state));
    const publicItems=()=>state.template&&state.template.published_version&&!state.template.revoked_at
      ? [{id:state.template.id,slug:state.template.slug,version:state.template.published_version,published_at:state.template.versions.find(v=>v.version===state.template.published_version)?.reviewed_at,
          name:state.template.versions.find(v=>v.version===state.template.published_version)?.name,
          summary:state.template.versions.find(v=>v.version===state.template.published_version)?.summary,
          agent_type:state.template.versions.find(v=>v.version===state.template.published_version)?.agent_type,
          knowledge_scope:state.template.versions.find(v=>v.version===state.template.published_version)?.knowledge_scope,
          tool_policy:state.template.versions.find(v=>v.version===state.template.published_version)?.tool_policy,can_instantiate:true}]
      : [];
    const managedItems=()=>state.template&&((state.actor==="reviewer")||(state.actor==="publisher"&&state.template.created_by_email===users.publisher.email))
      ? [{...state.template,versions:[...state.template.versions].sort((a,b)=>b.version-a.version)}] : [];
    const findVersion=(id)=>state.template?.versions.find(version=>version.id===id);
    window.fetch=async(input,init={})=>{
      const url=new URL(typeof input==="string"?input:input.url,location.href);
      if(!url.pathname.startsWith("/api/"))return originalFetch(input,init);
      const method=String(init.method??(typeof input==="string"?"GET":input.method)??"GET").toUpperCase();
      const body=typeof init.body==="string"?JSON.parse(init.body):null;
      state.calls.push({method,path:url.pathname,body});
      const actor=users[state.actor]||users.publisher;
      if(method==="GET"&&url.pathname==="/api/auth/me")return json(actor);
      if(method==="GET"&&url.pathname==="/api/deployment/profile")return json({company_name:"Synthetic Company",industry:"Testing",brand_name:"InfraSpine QA",primary_color:"#1765ad",setup_complete:true,template_id:null,template_version:null});
      if(method==="GET"&&url.pathname==="/api/agents")return json({agents:[]});
      if(method==="GET"&&url.pathname==="/api/agents/departments")return json({departments:[{id:"department-1",name:"Operations"}]});
      if(method==="GET"&&url.pathname==="/api/agent-catalog")return json({items:publicItems(),can_publish:state.actor==="publisher",can_review:state.actor==="reviewer"});
      if(method==="GET"&&url.pathname==="/api/agent-catalog/manage")return json({items:managedItems(),can_publish:state.actor==="publisher",can_review:state.actor==="reviewer"});
      if(method==="GET"&&url.pathname==="/api/agent-catalog/grants")return json({items:[]});
      if(method==="POST"&&url.pathname==="/api/agent-catalog/templates"){
        const now=new Date().toISOString();
        const version={id:"catalog-version-1",version:1,name:body.name,summary:body.summary,agent_type:body.agent_type,knowledge_scope:body.knowledge_scope,tool_policy:body.tool_policy,status:"draft",author_email:actor.email,created_at:now,submitted_by_email:null,submitted_at:null,reviewed_by_email:null,reviewed_at:null,review_rationale:null};
        state.template={id:"catalog-template-1",slug:body.slug,latest_version:1,published_version:null,created_by_email:actor.email,created_at:now,revoked_at:null,revoked_by_email:null,revoke_rationale:null,versions:[version]};
        save();return json({template_id:state.template.id,version_id:version.id,slug:state.template.slug,version:1,status:"draft"},201);
      }
      const submitMatch=url.pathname.match(/^\\/api\\/agent-catalog\\/versions\\/([^/]+)\\/submit$/);
      if(method==="POST"&&submitMatch){
        const version=findVersion(submitMatch[1]);if(!version)return json({error:{message:"Unknown version"}},404);
        version.status="pending_review";version.submitted_by_email=actor.email;version.submitted_at=new Date().toISOString();save();
        return json({id:version.id,template_id:state.template.id,version:version.version,status:"pending_review",submitted_at:version.submitted_at,reviewed_by_user_id:null,reviewed_at:null,review_rationale:null});
      }
      const reviewMatch=url.pathname.match(/^\\/api\\/agent-catalog\\/versions\\/([^/]+)\\/review$/);
      if(method==="POST"&&reviewMatch){
        const version=findVersion(reviewMatch[1]);if(!version||version.status!=="pending_review")return json({error:{message:"Unknown pending version"}},404);
        version.status=body.decision==="approve"?"approved":"rejected";version.reviewed_by_email=actor.email;version.reviewed_at=new Date().toISOString();version.review_rationale=body.rationale;
        if(body.decision==="approve")state.template.published_version=version.version;save();
        return json({id:version.id,template_id:state.template.id,version:version.version,status:version.status,submitted_at:version.submitted_at,reviewed_by_user_id:actor.id,reviewed_at:version.reviewed_at,review_rationale:version.review_rationale});
      }
      const revokeMatch=url.pathname.match(/^\\/api\\/agent-catalog\\/templates\\/([^/]+)\\/revoke$/);
      if(method==="POST"&&revokeMatch){
        state.template.revoked_at=new Date().toISOString();state.template.revoked_by_email=actor.email;state.template.revoke_rationale=body.rationale;save();
        return json({id:state.template.id,slug:state.template.slug,revoked_at:state.template.revoked_at,revoked_by_user_id:actor.id,revoke_rationale:body.rationale});
      }
      return json({error:{code:"unmocked_route",message:"The catalog UI called an unmocked endpoint."}},501);
    };
    save();
  })();`;
}

async function switchActor(actor) {
  await evaluate(`(()=>{const state=JSON.parse(localStorage.getItem("__infraspine_f19_catalog_state"));state.actor=${JSON.stringify(actor)};localStorage.setItem("__infraspine_f19_catalog_state",JSON.stringify(state));})()`);
  await call("Page.reload", { ignoreCache: true });
  if (!await waitFor(`document.querySelector('h1')?.textContent.includes('Agent profiles')`)) throw new Error(`Agent page did not reload as ${actor}.`);
  if (!await waitFor(`!!document.querySelector('[aria-label="Reviewed internal agent catalog"] h2')`)) throw new Error(`Catalog did not render for ${actor}.`);
}

async function fill(selector, value) {
  const result = await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});if(!el)return false;const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value').set;setter.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return true;})()`);
  if (!result) throw new Error(`Form control was missing: ${selector}`);
}

async function run() {
  port = await freePort();
  vite = spawn(process.execPath, [viteEntry, "--host", "127.0.0.1", "--port", String(port), "--strictPort"], { cwd: frontend, stdio: "inherit", windowsHide: true });
  const url = `http://127.0.0.1:${port}/admin/`;
  const ready = Date.now() + 30_000;
  while (Date.now() < ready && vite.exitCode === null) { try { if ((await fetch(url)).ok) break; } catch {} await delay(150); }
  if (vite.exitCode !== null) throw new Error("Vite exited before becoming ready.");

  profile = mkdtempSync(path.join(tmpdir(), "infraspine-f19-agent-catalog-ui-"));
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
  socket.addEventListener("message",(event)=>{const message=JSON.parse(event.data);if(!message.id||!pending.has(message.id))return;const entry=pending.get(message.id);clearTimeout(entry.timer);pending.delete(message.id);if(message.error)entry.reject(new Error(`Browser command failed for ${entry.method}: ${message.error.message??"unknown error"}`));else entry.resolve(message.result);});
  await call("Page.enable"); await call("Runtime.enable");
  await call("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await call("Page.addScriptToEvaluateOnNewDocument",{source:mockApiScript()});
  await call("Page.navigate",{url:`${url}#/agents`});
  if(!await waitFor(`document.querySelector('h1')?.textContent.includes('Agent profiles')`))throw new Error("Agent profiles page did not render.");
  if(!await waitFor(`document.querySelector('[aria-label="Reviewed internal agent catalog"] h2')?.textContent.includes('Reviewed internal agent catalog')`))throw new Error("Reviewed catalog did not render.");
  if(!await waitFor(`document.querySelector('[aria-label="Publisher template draft"] h3')?.textContent.includes('Create a template draft')`))throw new Error("Publisher draft form did not render.");

  await fill('[aria-label="Publisher template draft"] label.field:nth-of-type(1) input','operations-research');
  await fill('[aria-label="Publisher template draft"] label.field:nth-of-type(2) input','Operations research helper');
  await fill('[aria-label="Publisher template draft"] textarea','Find internal procedures the requesting employee can read.');
  await evaluate(`(()=>{const form=document.querySelector('[aria-label="Publisher template draft"] form');form?.requestSubmit();})()`);
  if(!await waitFor(`document.querySelector('[role="status"]')?.textContent.includes('Draft version 1 saved')`))throw new Error("Draft save did not produce the expected status.");
  if(!await waitFor(`[...document.querySelectorAll('.approval-card button')].some(button=>button.textContent.includes('Submit for independent review'))`))throw new Error("Saved draft did not appear in the publisher lifecycle list.");
  await evaluate(`(()=>{[...document.querySelectorAll('.approval-card button')].find(button=>button.textContent.includes('Submit for independent review'))?.click();})()`);
  if(!await waitFor(`document.querySelector('.approval-card')?.textContent.includes('pending review')`))throw new Error("Draft was not submitted to the review queue.");
  const submitted=await evaluate(`window.__f19Catalog?.calls.find(call=>call.method==='POST'&&call.path.endsWith('/submit'))`);
  if(!submitted)throw new Error("Submit request was not recorded by the synthetic API.");
  console.log("F19_PUBLISHER_DRAFT_AND_SUBMIT=PASS");

  await switchActor("reviewer");
  if(!await waitFor(`document.querySelector('.approval-card')?.textContent.includes('pending review')`))throw new Error("Independent reviewer did not see the pending version.");
  await fill('.approval-card textarea','The configuration matches the reviewed requester-access scope.');
  await evaluate(`(()=>{[...document.querySelectorAll('.approval-card button')].find(button=>button.textContent.includes('Approve version'))?.click();})()`);
  if(!await waitFor(`document.querySelector('[aria-label="Reviewed internal agent catalog"]')?.textContent.includes('Operations research helper')`))throw new Error("Approved template was not published to the catalog.");
  const reviewed=await evaluate(`window.__f19Catalog?.calls.find(call=>call.method==='POST'&&call.path.endsWith('/review'))`);
  if(reviewed?.body?.decision!=="approve"||reviewed.body.rationale.length<3)throw new Error("Reviewer decision/rationale was not submitted.");
  console.log("F19_INDEPENDENT_REVIEW_AND_PUBLICATION=PASS");

  await switchActor("publisher");
  if(!await waitFor(`[...document.querySelectorAll('[aria-label="Reviewed internal agent catalog"] button')].some(button=>button.textContent.includes('Use as department profile'))`)){
    const diagnostic=await evaluate(`(()=>{const state=JSON.parse(localStorage.getItem("__infraspine_f19_catalog_state"));return {actor:state.actor,published_version:state.template?.published_version,revoked_at:state.template?.revoked_at,public_text:document.querySelector('[aria-label="Reviewed internal agent catalog"]')?.textContent.slice(0,500),alerts:[...document.querySelectorAll('[role="alert"]')].map(item=>item.textContent)};})()`);
    throw new Error(`Published template was not available to a department manager: ${JSON.stringify(diagnostic)}`);
  }
  await evaluate(`(()=>{[...document.querySelectorAll('[aria-label="Reviewed internal agent catalog"] button')].find(button=>button.textContent.includes('Use as department profile'))?.click();})()`);
  if(!await waitFor(`document.querySelector('#department-agent-profile-form input')?.value==='Operations research helper'`))throw new Error("Template did not populate the department profile form.");
  const copied=await evaluate(`(()=>{const form=document.querySelector('#department-agent-profile-form');const selects=[...form.querySelectorAll('select')];return {name:form.querySelector('input')?.value,department:selects[0]?.value,type:selects[1]?.value,scope:selects[2]?.value};})()`);
  if(copied?.name!=="Operations research helper"||copied?.type!=="knowledge_research")throw new Error("Reviewed template fields were not copied to the department profile form.");
  const layout=await evaluate(`({viewport:innerWidth,width:document.documentElement.scrollWidth})`);
  if(layout.viewport!==390||layout.width>390)throw new Error(`Agent catalog/profile UI overflows at 390px: ${JSON.stringify(layout)}`);
  console.log("F19_TEMPLATE_APPLY_AND_390PX=PASS");

  await switchActor("reviewer");
  await fill('.approval-card textarea','Revoke this template after the review period.');
  await evaluate(`(()=>{[...document.querySelectorAll('.approval-card button')].find(button=>button.textContent.includes('Revoke catalog template'))?.click();})()`);
  if(!await waitFor(`document.querySelector('[aria-label="Reviewed internal agent catalog"] .empty')?.textContent.includes('No reviewed templates are published')`))throw new Error("Revoked template remained visible in the employee catalog.");
  const revoked=await evaluate(`window.__f19Catalog?.calls.find(call=>call.method==='POST'&&call.path.endsWith('/revoke'))`);
  if(revoked?.body?.rationale?.length<3)throw new Error("Revocation rationale was not submitted.");
  const auditState=await evaluate(`({revoked:window.__f19Catalog?.template?.revoked_at!==null,versionHistory:window.__f19Catalog?.template?.versions?.length,calls:window.__f19Catalog?.calls.length})`);
  if(!auditState.revoked||auditState.versionHistory!==1)throw new Error("Revocation did not preserve the synthetic version history.");
  console.log("F19_REVOCATION_AND_HISTORY=PASS");
  console.log("F19_CURRENT_SOURCE_AGENT_CATALOG_BROWSER_ACCEPTANCE=PASS (390px; synthetic in-page API; backend authorization is verified separately)");
}

run().catch((error)=>{console.error(`F19_AGENT_CATALOG_UI=FAIL ${error?.message??"unknown failure"}`);process.exitCode=1;}).finally(async()=>{
  try{socket?.close();}catch{}
  for(const child of [browser,vite]){if(child&&child.exitCode===null){child.kill();await new Promise((resolve)=>{const timer=setTimeout(resolve,2000);child.once("exit",()=>{clearTimeout(timer);resolve();});});}}
  if(profile&&existsSync(profile))rmSync(profile,{recursive:true,force:true});
});
