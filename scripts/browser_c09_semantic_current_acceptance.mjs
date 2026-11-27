#!/usr/bin/env node
/** Current-source Admin/Manager/Employee acceptance for low-overlap local-model semantic conflict review. */
import { createHash } from "node:crypto";
import { spawn, execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL(process.env.INFRASPINE_C09_REVIEW_URL ?? "http://127.0.0.1:5178/admin/");
const fixturePath = process.env.INFRASPINE_C09_CREDENTIAL_FIXTURE;
const reviewRoot = process.env.INFRASPINE_C09_REVIEW_ROOT;
if (base.protocol !== "http:" || !["127.0.0.1", "localhost", "::1"].includes(base.hostname) || !fixturePath || !reviewRoot) {
  throw new Error("C09 browser acceptance requires the isolated loopback review and protected synthetic fixture.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const admin = fixture.admin;
const manager = fixture.managers?.[0];
const employee = fixture.employees?.[0];
const outsider = fixture.employees?.[1];
if (![admin, manager, employee, outsider].every((account) => account?.email && account?.password)) {
  throw new Error("The protected F03 role fixture is incomplete.");
}
const browserPath = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe"),
  path.join(process.env.LOCALAPPDATA ?? "", "Microsoft\\Edge\\Application\\msedge.exe"),
].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome or Edge is unavailable for C09 browser acceptance.");

const groupName = "F03 Knowledge Interview Operations";
const baselineFilename = "c09-incident-retention-source.pdf";
const candidateFilename = "c09-incident-deletion-source.pdf";
const baselineQuote = "Incident reports must be retained for seven years.";
const candidateQuote = "Incident documentation may be deleted after one year.";
const baselineStatement = "Managers must retain incident reports for seven years.";
const candidateStatement = "Incident documentation may be deleted after one year.";
const semanticProbe = JSON.parse(readFileSync(path.join(reviewRoot, "evidence", "c09-semantic-pair-current-model.json"), "utf8"));
if (semanticProbe.result !== "PASS" || semanticProbe.lexicalOnlyCandidate || !semanticProbe.semanticCandidate || semanticProbe.cosineSimilarity < semanticProbe.threshold) {
  throw new Error("Current local MiniLM did not qualify this low-overlap semantic pair.");
}
const evidenceDir = path.join(reviewRoot, "evidence");
mkdirSync(evidenceDir, { recursive: true });
const screenshotPath = path.join(evidenceDir, "c09-conflict-review-390.png");
const profile = mkdtempSync(path.join(tmpdir(), "infraspine-c09-conflicts-"));
let chrome;
let socket;
let sequence = 0;
const pending = new Map();

function command(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser timed out at ${method}.`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression, timeoutMs = 90_000) {
  const response = await command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (response.exceptionDetails) throw new Error("A current-source C09 browser action failed.");
  return response.result?.value;
}
async function waitFor(expression, label, timeoutMs = 30_000) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (await evaluate(expression)) return;
    await delay(120);
  }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function signIn(account) {
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "login form");
  const submitted = await evaluate(`(()=>{const email=document.querySelector('form.login-form input[name="email"]');const pass=document.querySelector('form.login-form input[name="password"]');const form=document.querySelector('form.login-form');if(!email||!pass||!form)return false;for(const [el,value] of [[email,${JSON.stringify(account.email)}],[pass,${JSON.stringify(account.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}form.requestSubmit();return true})()`);
  if (!submitted) throw new Error("The synthetic role login form could not be submitted.");
  await waitFor("Boolean(document.querySelector('.shell'))", "authenticated review shell", 40_000);
}
async function signOut() {
  const clicked = await evaluate(`(()=>{const b=document.querySelector('button[aria-label="Sign out"]');if(!b)return false;b.click();return true})()`);
  if (!clicked) throw new Error("The synthetic role could not sign out through the visible control.");
  await waitFor("Boolean(document.querySelector('form.login-form'))", "login form after sign out");
}
async function openApprovals() {
  await waitFor("Boolean([...document.querySelectorAll('button.nav-item')].some(x=>x.textContent.trim()==='Approvals'))", "Approvals navigation");
  const clicked = await evaluate(`(()=>{const b=[...document.querySelectorAll('button.nav-item')].find(x=>x.textContent.trim()==='Approvals');if(!b)return false;b.click();return true})()`);
  if (!clicked) throw new Error("Approvals was unavailable for this synthetic role.");
  await waitFor("document.querySelector('h1')?.textContent.trim()==='Approvals'", "Approvals page");
  await waitFor("!document.querySelector('.loading')", "loaded Approvals queues");
}
async function setRationaleInCard(statement, value) {
  const expression = `(()=>{const card=[...document.querySelectorAll('article.approval-card')].find(x=>x.innerText.includes(${JSON.stringify(statement)}));const field=[...(card?.querySelectorAll('label.field')??[])].find(x=>x.querySelector('.field-label')?.textContent.trim().startsWith('Comparison rationale'));const el=field?.querySelector('textarea');if(!el)return false;Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));return true})()`;
  if (!await evaluate(expression)) throw new Error("The scoped conflict comparison rationale field was unavailable.");
}
async function setDecisionRationaleInCard(statement, value) {
  const expression = `(()=>{const card=[...document.querySelectorAll('article.approval-card')].find(x=>x.innerText.includes(${JSON.stringify(statement)}));const field=[...(card?.querySelectorAll('label.field')??[])].find(x=>x.querySelector('.field-label')?.textContent.trim().startsWith('Decision rationale'));const el=field?.querySelector('textarea');if(!el)return false;Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event('input',{bubbles:true}));return true})()`;
  if (!await evaluate(expression)) throw new Error("The source-backed knowledge decision rationale field was unavailable.");
}
async function clickCardButton(statement, label) {
  return Boolean(await evaluate(`(()=>{const card=[...document.querySelectorAll('article.approval-card')].find(x=>x.innerText.includes(${JSON.stringify(statement)}));const b=[...(card?.querySelectorAll('button')??[])].find(x=>x.textContent.trim()===${JSON.stringify(label)});if(!b||b.disabled)return false;b.click();return true})()`));
}
async function readConflicts() {
  return evaluate(`(async()=>{const r=await fetch('/api/governance/knowledge/conflicts');return r.ok?await r.json():{status:r.status}})()`);
}
async function seedAccessAndBaseline() {
  return evaluate(`(async()=>{
    const headers={'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'};
    const [g,d,gr,k]=await Promise.all([
      fetch('/api/admin/access/groups').then(r=>r.json()),
      fetch('/api/rag/documents').then(r=>r.json()),
      fetch('/api/admin/access/grants').then(r=>r.json()),
      fetch('/api/knowledge').then(r=>r.json()),
    ]);
    const group=g.groups?.find(x=>x.name===${JSON.stringify(groupName)}&&x.kind==='department');
    const baselineDoc=d.documents?.find(x=>x.filename===${JSON.stringify(baselineFilename)}&&x.status==='ready');
    const candidateDoc=d.documents?.find(x=>x.filename===${JSON.stringify(candidateFilename)}&&x.status==='ready');
    if(!group||!baselineDoc||!candidateDoc) return {ok:false,stage:'synthetic department or two ready cited sources missing'};
    const grantRows=gr.grants??[];
    async function ensureGrant(email,permission){
      const found=grantRows.find(x=>x.email?.toLowerCase()===email.toLowerCase()&&x.permission===permission&&x.category==='policy'&&String(x.scope_group_id)===String(group.id)&&!x.revoked_at);
      if(found) return {id:found.id,status:200};
      const response=await fetch('/api/admin/access/grants',{method:'POST',headers,body:JSON.stringify({email,permission,category:'policy',scope_group_id:group.id})});
      // The access API returns 409 only when the unique active user/permission/category/scope grant already exists.
      if(response.status===409) return {id:'already-active',status:409};
      if(!response.ok) return {id:null,status:response.status};
      const row=await response.json();grantRows.push(row);return {id:row.id,status:response.status};
    }
    const reviewGrant=await ensureGrant(${JSON.stringify(manager.email)},'knowledge.review');
    const publishGrant=await ensureGrant(${JSON.stringify(employee.email)},'knowledge.publish');
    if(!reviewGrant.id||!publishGrant.id) return {ok:false,stage:'scoped reviewer/publisher grants',reviewStatus:reviewGrant.status,publishStatus:publishGrant.status};
    let entry=(k.items??[]).find(x=>x.current?.statement===${JSON.stringify(baselineStatement)});
    if(!entry){
      const created=await fetch('/api/knowledge',{method:'POST',headers,body:JSON.stringify({statement:${JSON.stringify(baselineStatement)},category:'policy',visibility:'restricted',scope_group_id:group.id,evidence:[{document_id:baselineDoc.id,quote:${JSON.stringify(baselineQuote)}}]})});
      if(!created.ok) return {ok:false,stage:'baseline draft creation',status:created.status};
      entry=await created.json();
    } else {
      const detail=await fetch('/api/knowledge/'+encodeURIComponent(entry.id));
      if(!detail.ok) return {ok:false,stage:'existing baseline revision detail',status:detail.status};
      entry=await detail.json();
    }
    const revision=entry.revisions?.[0];
    if(!revision) return {ok:false,stage:'baseline revision missing'};
    if(revision.status==='draft'){
      const submitted=await fetch('/api/knowledge/'+encodeURIComponent(entry.id)+'/submit',{method:'POST',headers});
      if(!submitted.ok) return {ok:false,stage:'baseline submission',status:submitted.status};
    }
    return {ok:true,groupId:group.id,documentId:baselineDoc.id,entryId:entry.id,baselineStatus:revision.status};
  })()`);
}

try {
  chrome = spawn(browserPath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profile, "DevToolsActivePort");
  const startBy = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < startBy) await delay(100);
  if (!existsSync(portFile)) throw new Error("Isolated Chrome did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!page?.webSocketDebuggerUrl) throw new Error("Isolated Chrome did not expose a page target.");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error("Chrome DevTools command failed.")) : item.resolve(message.result);
  });
  await command("Page.enable"); await command("Runtime.enable");
  await command("Page.navigate", { url: base.href });

  // Admin verifies the prepared source and creates the scoped review/publish grants and baseline draft.
  await signIn(admin);
  const prepared = await seedAccessAndBaseline();
  if (!prepared?.ok) throw new Error(`Admin synthetic conflict setup failed: ${JSON.stringify({ stage: prepared?.stage, status: prepared?.status, reviewStatus: prepared?.reviewStatus, publishStatus: prepared?.publishStatus })}`);
  await signOut();

  // A Manager records the baseline review in the actual Approvals UI.
  await signIn(manager);
  await openApprovals();
  if (prepared.baselineStatus !== "published") {
    await waitFor(`Boolean([...document.querySelectorAll('article.approval-card')].some(x=>x.innerText.includes(${JSON.stringify(baselineStatement)})))`, "department-scoped baseline review card");
    await setDecisionRationaleInCard(baselineStatement, "Reviewed the exact synthetic source passage and department scope before publication.");
    if (!await clickCardButton(baselineStatement, "Record review")) throw new Error("Manager could not record the baseline review in the UI.");
    await waitFor("Boolean(document.querySelector('.toasts')?.innerText.includes('Review recorded'))", "Manager baseline review confirmation");
  }
  await signOut();

  // A different Employee publishes the reviewed baseline, preserving two-person approval.
  await signIn(employee);
  await openApprovals();
  const publicationQueue = await evaluate("(async()=>{const r=await fetch('/api/governance/knowledge');return r.ok?await r.json():null})()");
  const baseline = publicationQueue?.items?.find((item) => item.statement === baselineStatement);
  if (prepared.baselineStatus !== "published" && !baseline?.allowed_stages?.includes("publish")) {
    throw new Error("The independent Employee did not receive the reviewed baseline publication item.");
  }
  if (prepared.baselineStatus !== "published") {
    await waitFor(`Boolean([...document.querySelectorAll('article.approval-card')].some(x=>x.innerText.includes(${JSON.stringify(baselineStatement)})))`, "Employee baseline publication card");
    await setDecisionRationaleInCard(baselineStatement, "Approved by a different employee after the independent Manager review.");
    if (!await clickCardButton(baselineStatement, "Approve and publish")) throw new Error("Employee could not publish the independently reviewed baseline in the UI.");
    await waitFor("Boolean(document.querySelector('.toasts')?.innerText.includes('Knowledge published'))", "Employee publication confirmation");
  }
  const candidate = await evaluate(`(async()=>{
    const headers={'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'};
    const data=await fetch('/api/rag/documents').then(r=>r.json());
    const doc=data.documents?.find(x=>x.filename===${JSON.stringify(candidateFilename)}&&x.status==='ready');
    if(!doc)return {ok:false,stage:'separately cited candidate source unavailable'};
    const knowledge=await fetch('/api/knowledge').then(r=>r.json());
    let entry=(knowledge.items??[]).find(x=>x.current?.statement===${JSON.stringify(candidateStatement)});
    if(!entry){
      const body={statement:${JSON.stringify(candidateStatement)},category:'policy',visibility:'restricted',scope_group_id:${JSON.stringify(prepared.groupId)},evidence:[{document_id:doc.id,quote:${JSON.stringify(candidateQuote)}}]};
      const created=await fetch('/api/knowledge',{method:'POST',headers,body:JSON.stringify(body)});
      if(!created.ok)return {ok:false,stage:'employee candidate creation',status:created.status};
      entry=await created.json();
    } else {
      const detail=await fetch('/api/knowledge/'+encodeURIComponent(entry.id));
      if(!detail.ok)return {ok:false,stage:'existing candidate revision detail',status:detail.status};
      entry=await detail.json();
    }
    const revision=entry.revisions?.[0];
    if(!revision)return {ok:false,stage:'candidate revision missing'};
    if(revision.status==='draft'){
      const submitted=await fetch('/api/knowledge/'+encodeURIComponent(entry.id)+'/submit',{method:'POST',headers});
      if(!submitted.ok)return {ok:false,stage:'employee candidate submission',status:submitted.status};
    }
    return {ok:true,entryId:entry.id,revisionStatus:revision.status};
  })()`, 120_000);
  if (!candidate?.ok) throw new Error("Employee could not submit the scoped contradictory knowledge candidate.");
  await signOut();

  // The assigned Manager confirms the conflict is in scope and obtains its opaque identifier.
  await signIn(manager);
  await openApprovals();
  await waitFor(`Boolean([...document.querySelectorAll('article.approval-card')].some(x=>x.innerText.includes(${JSON.stringify(candidateStatement)})))`, "scoped conflict review card");
  const queue = await readConflicts();
  const item = queue?.items?.find((row) => row.submitted?.statement === candidateStatement && row.status === "open");
  if (!item?.id || item.existing?.statement !== baselineStatement || item.submitted?.sources?.[0]?.filename !== candidateFilename || item.existing?.sources?.[0]?.filename !== baselineFilename) {
    throw new Error("Manager conflict queue did not preserve the expected two-sided source citations.");
  }
  await signOut();

  // The contributing Employee is not a reviewer, despite holding a separate publish grant.
  await signIn(employee);
  await openApprovals();
  const employeeDenied = await evaluate(`(async()=>{
    const list=await fetch('/api/governance/knowledge/conflicts');
    const value=list.ok?await list.json():null;
    const decision=await fetch('/api/governance/knowledge/conflicts/${item.id}/decisions',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'},body:JSON.stringify({decision:'confirmed',rationale:'Publisher role does not include conflict review.'})});
    return {listStatus:list.status,count:value?.items?.length??-1,pageContains:document.body.innerText.includes(${JSON.stringify(candidateStatement)}),decisionStatus:decision.status};
  })()`);
  if (!employeeDenied || employeeDenied.listStatus !== 200 || employeeDenied.count !== 0 || employeeDenied.pageContains || employeeDenied.decisionStatus !== 403) {
    throw new Error(`Publisher-only Employee boundary failed: ${JSON.stringify(employeeDenied)}`);
  }
  await signOut();

  // The out-of-department Employee sees no conflict content and receives opaque refusal on a known ID.
  await signIn(outsider);
  await openApprovals();
  const denied = await evaluate(`(async()=>{
    const list=await fetch('/api/governance/knowledge/conflicts');
    const value=list.ok?await list.json():null;
    const decision=await fetch('/api/governance/knowledge/conflicts/${item.id}/decisions',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'},body:JSON.stringify({decision:'confirmed',rationale:'Unauthorized out-of-department decision attempt.'})});
    return {listStatus:list.status,count:value?.items?.length??-1,pageContains:document.body.innerText.includes(${JSON.stringify(candidateStatement)}),decisionStatus:decision.status};
  })()`);
  if (!denied || denied.listStatus !== 200 || denied.count !== 0 || denied.pageContains || ![403, 404].includes(denied.decisionStatus)) {
    throw new Error(`Out-of-department Employee boundary failed: ${JSON.stringify(denied)}`);
  }
  await signOut();

  // Manager sees both source-backed statements and makes the advisory decision in the UI.
  await signIn(manager);
  await openApprovals();
  await waitFor(`Boolean([...document.querySelectorAll('article.approval-card')].some(x=>x.innerText.includes(${JSON.stringify(candidateStatement)})))`, "scoped conflict review card after role denial");
  await setRationaleInCard(candidateStatement, "The source passage and scoped operating rule conflict; human review confirmed the inconsistency.");
  if (!await clickCardButton(candidateStatement, "Confirm conflict")) throw new Error("Manager could not confirm the conflict in the Approvals UI.");
  await waitFor("Boolean(document.querySelector('.toasts')?.innerText.includes('Conflict confirmed'))", "Manager conflict decision confirmation");
  const confirmed = await readConflicts();
  const final = confirmed?.items?.find((row) => row.id === item.id);
  if (final?.status !== "confirmed" || final.review?.decision !== "confirmed" || !final.review?.rationale?.includes("human review")) {
    throw new Error("Manager conflict decision was not returned with its immutable rationale.");
  }
  const mobile = await command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await delay(350);
  const dimensions = await evaluate("({viewport:document.documentElement.clientWidth,body:document.body.clientWidth,root:document.documentElement.scrollWidth})");
  if (!dimensions || dimensions.viewport !== 390 || dimensions.body > 390) throw new Error("The current Approvals page body exceeded 390px.");
  const shot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(screenshotPath, Buffer.from(shot.data, "base64"));
  const files = [
    "app/rag/conflict_detection.py",
    "app/services/knowledge.py",
    "app/api/routes/governance.py",
    "frontend/src/components/ApprovalsPage.tsx",
    "scripts/browser_c09_semantic_current_acceptance.mjs",
  ];
  const sourceSha256 = Object.fromEntries(files.map((file) => [file, createHash("sha256").update(readFileSync(file)).digest("hex")]));
  const evidence = {
    result: "PASS",
    reviewUrl: base.href,
    apiUrl: "http://127.0.0.1:63013",
    database: "infraspine_c09curr_20261029_test",
    migration: "0063_cloud_source_acl_intersection",
    scope: "fresh current-source authenticated browser/API, current local MiniLM semantic-only candidate with low lexical overlap and two separately cited synthetic PDFs; no live provider",
    roles: ["Admin prepared the verified source and scoped grants", "Manager reviewed baseline and confirmed the conflict in Approvals", "Employee independently published baseline and submitted candidate but received 403 on conflict decision", "out-of-department Employee received empty queue and opaque denial"],
    decision: "confirmed",
    citationFilenames: [baselineFilename, candidateFilename],
    semanticModelProbe: {
      model: semanticProbe.model,
      cosineSimilarity: semanticProbe.cosineSimilarity,
      threshold: semanticProbe.threshold,
      lexicalOverlap: semanticProbe.lexicalOverlap,
      lexicalOnlyCandidate: semanticProbe.lexicalOnlyCandidate,
      semanticCandidate: semanticProbe.semanticCandidate,
    },
    conflictId: item.id,
    mobile390: dimensions,
    screenshot: path.relative(process.cwd(), screenshotPath).replaceAll("\\", "/"),
    gitHead: execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim(),
    sourceSha256,
  };
  writeFileSync(path.join(evidenceDir, "c09-semantic-conflict-current-source.json"), JSON.stringify(evidence, null, 2) + "\n", { mode: 0o600 });
  console.log("C09_CURRENT_SOURCE_SEMANTIC_CONFLICT_BROWSER=PASS; local MiniLM low-overlap candidate with two citations; two-person publish; Manager UI decision; out-of-department Employee denied; 390px");
} finally {
  socket?.close();
  if (chrome && chrome.exitCode === null) { chrome.kill(); await new Promise((resolve) => chrome.once("exit", resolve)); }
  try { await import("node:fs/promises").then(({ rm }) => rm(profile, { recursive: true, force: true })); } catch { /* only the harness's own temporary profile */ }
}
