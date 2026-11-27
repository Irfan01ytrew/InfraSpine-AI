import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {existsSync,mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import path from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const runtime=path.resolve('.verification-local-review-20261027-native/f18-current-source/f20-current-source');
const base='http://127.0.0.1:5181/admin/';
const fixture=JSON.parse(readFileSync(path.resolve('.verification-local-review-20261027-native/f18-current-source/review-credentials.json'),'utf8'));
const admin=fixture.admin;
const employee=(fixture.employees||[]).find(item=>item.email==='f20-browser-requester@synthetic.test');
const chrome='C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const profile=path.join(runtime,'chrome-profile-'+Date.now());
const evidence=path.join(runtime,'evidence');
mkdirSync(evidence,{recursive:true});
if(!admin?.email||!admin?.password||!employee?.email||!employee?.password)throw Error('Protected synthetic accounts are incomplete.');

let mutationCount=0;
let unapprovedOriginHits=0;
let flakyRequests=0;
const server=createServer((req,res)=>{
  const url=new URL(req.url,'http://127.0.0.1:63120');
  if(req.method==='POST'){
    mutationCount++;
    req.resume();
    res.writeHead(204,{'content-length':'0'});res.end();
    return;
  }
  if(url.pathname==='/blocked')unapprovedOriginHits++;
  if(url.pathname==='/flaky'&&flakyRequests++===0){req.socket.destroy();return;}
  let body='';
  if(url.pathname==='/'){
    body='<!doctype html><html><head><title>Legacy index</title></head><body><h1>Synthetic legacy index</h1><script>document.body.append(document.cookie || "NO_COOKIE");fetch("/mutate",{method:"POST",body:"blocked"}).catch(()=>{});fetch("http://localhost:63120/blocked").catch(()=>{});</script><a href="/manual">Controlled manual</a></body></html>';
  }else if(url.pathname==='/manual'){
    body='<!doctype html><html><head><title>Legacy manual</title></head><body><h1>Approved manual text</h1><p>synthetic-f20-citation-evidence</p></body></html>';
  }else if(url.pathname==='/flaky'){
    body='<!doctype html><html><head><title>Recovered page</title></head><body>Transient fetch recovered on approved retry.</body></html>';
  }else{
    body='<!doctype html><html><head><title>Not found</title></head><body>Unavailable</body></html>';
  }
  res.writeHead(200,{'content-type':'text/html; charset=utf-8','content-length':Buffer.byteLength(body),'set-cookie':'fixture_cookie=must-not-forward; Path=/'});
  res.end(body);
});

let chromeProcess,socket,sequence=0;
const pending=new Map();
function cdp(method,params={}){
  const id=++sequence;
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(Error('CDP command timed out: '+method)),30000);
    pending.set(id,{resolve,reject,timer});
    socket.send(JSON.stringify({id,method,params}));
  });
}
async function evaluate(expression){
  const response=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true,userGesture:true});
  if(response.exceptionDetails){let detail=response.exceptionDetails.exception?.description||response.exceptionDetails.text||'unknown',safeExpression=expression;for(const secret of [admin?.password,employee?.password])if(secret){detail=detail.replaceAll(secret,'[redacted]');safeExpression=safeExpression.replaceAll(secret,'[redacted]');}throw Error('Browser expression failed: '+detail.split('\n')[0].slice(0,120)+' expr='+safeExpression.slice(0,260));}
  return response.result?.value;
}
async function waitFor(expression,label,timeoutMs=30000){
  const deadline=Date.now()+timeoutMs;
  while(Date.now()<deadline){if(await evaluate(expression))return;await delay(200);}
  throw Error('Timed out waiting for '+label+'.');
}
function setFormValues(selector,values){
  return `(()=>{const f=document.querySelector(${JSON.stringify(selector)});if(!f)return false;const nodes=[...f.querySelectorAll('input,textarea')];if(nodes.length!==${values.length})return false;const set=(node,value)=>{const proto=node.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(node,value);node.dispatchEvent(new Event('input',{bubbles:true}));node.dispatchEvent(new Event('change',{bubbles:true}))};${values.map((value,index)=>`set(nodes[${index}],${JSON.stringify(value)});`).join('')}f.requestSubmit();return true})()`;
}
async function login(account){
  stage='fill '+(account===employee?'Employee':'Admin')+' login form';
  const expression=`(()=>{const f=document.querySelector('form.login-form'),e=f?.querySelector('[name=email]'),p=f?.querySelector('[name=password]');if(!f||!e||!p)return false;const set=(x,v)=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(x,v);x.dispatchEvent(new Event('input',{bubbles:true}));x.dispatchEvent(new Event('change',{bubbles:true}))};set(e,${JSON.stringify(account.email)});set(p,${JSON.stringify(account.password)});f.requestSubmit();return true})()`;
  if(!await evaluate(expression))throw Error('Login form was unavailable.');
  stage='wait for '+(account===employee?'Employee':'Admin')+' session';
  await waitFor(`(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;return (await r.json()).email===${JSON.stringify(account.email)}})()`,'authenticated account');
  await waitFor(`Boolean(document.querySelector('.research-browser'))`,'research page');
}
async function logout(){
  if(!await evaluate(`(()=>{const button=document.querySelector('button[aria-label="Sign out"]');if(!button)return false;button.click();return true})()`))throw Error('Sign out control was unavailable.');
  await waitFor(`Boolean(document.querySelector('form.login-form'))`,'sign-out');
}
async function api(method,pathname,payload){
  const expression=`(async()=>{const init={method:${JSON.stringify(method)},credentials:'same-origin',headers:{'X-Requested-With':'F20QA'},cache:'no-store'};${payload===undefined?'':`init.headers['Content-Type']='application/json';init.body=JSON.stringify(${JSON.stringify(payload)});`}const r=await fetch(${JSON.stringify(pathname)},init);let body=null;try{body=await r.json()}catch{}return {status:r.status,body}})()`;
  return evaluate(expression);
}
async function submitRequest({label,url,links=[]}){
  const reason='Synthetic acceptance of bounded read-only legacy-site review.';
  if(!await evaluate(setFormValues('.research-browser form',[label,reason,url,links.join('\n')])))throw Error('Legacy browser request form was unavailable.');
  await waitFor(`(async()=>{const r=await fetch('/api/legacy-browser/runs',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;const j=await r.json();const x=j.items.find(v=>v.label===${JSON.stringify(label)});if(x)window.__f20RunId=x.id;return Boolean(x)})()`,'persisted browser request');
  return evaluate('window.__f20RunId');
}
async function clickRunAction(runId,label){
  stage='wait for Admin action '+label;
  await waitFor(`Boolean([...document.querySelectorAll('.research-browser-run')].some(card=>card.innerText.includes(${JSON.stringify(runId.slice(0,8))})&&[...card.querySelectorAll('button')].some(button=>button.innerText.trim()===${JSON.stringify(label)}&&!button.disabled)))`,'Admin action '+label);
  const result=await evaluate(`(()=>{const card=[...document.querySelectorAll('.research-browser-run')].find(x=>x.innerText.includes(${JSON.stringify(runId.slice(0,8))}));const button=[...(card?.querySelectorAll('button')||[])].find(x=>x.innerText.trim()===${JSON.stringify(label)});if(!button||button.disabled)return false;button.click();return true})()`);
  if(!result)throw Error(`Expected Admin review action ${label} was unavailable.`);
}

let stage='fixture-server';
try{
  if(!existsSync(chrome))throw Error('Local Chrome was unavailable.');
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(63120,'127.0.0.1',resolve);});
  stage='Chrome startup';
  chromeProcess=spawn(chrome,['--headless=new','--incognito','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,base],{stdio:'ignore',windowsHide:true});
  const active=path.join(profile,'DevToolsActivePort');
  const chromeDeadline=Date.now()+30000;
  while(!existsSync(active)&&Date.now()<chromeDeadline)await delay(100);
  if(!existsSync(active))throw Error('Chrome did not create its DevTools endpoint.');
  const port=readFileSync(active,'utf8').split(/\r?\n/)[0];
  const page=(await(await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(item=>item.type==='page');
  if(!page)throw Error('Chrome page target was unavailable.');
  socket=new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
  socket.addEventListener('message',event=>{
    const message=JSON.parse(event.data),item=pending.get(message.id);
    if(!item)return;clearTimeout(item.timer);pending.delete(message.id);
    message.error?item.reject(Error('CDP command failed.')):item.resolve(message.result);
  });
  await cdp('Page.enable');await cdp('Runtime.enable');
  await cdp('Page.navigate',{url:base+'#/research'});
  stage='Employee login';
  await waitFor(`Boolean(document.querySelector('form.login-form'))`,'login page');
  await login(employee);
  const status=await api('GET','/api/legacy-browser/status');
  if(status.status!==200||!status.body.enabled||status.body.maximum_pages!==4)throw Error('Current-source browser capability status or page budget was unexpected.');
  const unlisted=await api('POST','/api/legacy-browser/runs',{label:'F20 unlisted origin rejection',reason:'Must reject a host outside the operator allowlist.',start_url:'https://not-allowlisted.example/',link_labels:[]});
  if(unlisted.status!==422)throw Error('An unlisted origin was accepted as a browser request.');
  const overBudget=await api('POST','/api/legacy-browser/runs',{label:'F20 action budget rejection',reason:'Must enforce the bounded page action list.',start_url:'http://127.0.0.1:63120/',link_labels:['one','two','three','four']});
  if(overBudget.status!==422)throw Error('The API accepted more than three page actions.');
  const employeeRunsBefore=await api('GET','/api/legacy-browser/runs');
  if(employeeRunsBefore.status!==200)throw Error('Employee request list was not accessible.');
  for(const oldRun of employeeRunsBefore.body.items.filter(run=>run.status==='pending_approval'&&(run.label.startsWith('F20 approved manual')||run.label.startsWith('F20 transient capture retry')))){
    const cancelled=await api('POST',`/api/legacy-browser/runs/${oldRun.id}/cancel`);
    if(cancelled.status!==200)throw Error(`A pending request from the interrupted synthetic attempt could not be cancelled (HTTP ${cancelled.status}, ${cancelled.body?.error?.code||'no-code'}).`);
  }
  const normalRun=await submitRequest({label:'F20 approved manual '+Date.now(),url:'http://127.0.0.1:63120/',links:['Controlled manual']});
  const selfCheck=await api('POST',`/api/legacy-browser/runs/${normalRun}/approve`);
  if(selfCheck.status!==403)throw Error('Employee approval permission was not denied.');
  const employeeRunsAfter=await api('GET','/api/legacy-browser/runs');
  if(!employeeRunsAfter.body.items.some(run=>run.id===normalRun&&run.status==='pending_approval'))throw Error('Employee could not see the own pending request.');
  stage='Employee logout and Admin login';
  await logout();await login(admin);
  stage='Admin initial capture approval';
  await clickRunAction(normalRun,'Approve and capture');
  await waitFor(`(async()=>{const r=await fetch('/api/legacy-browser/runs',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;const j=await r.json(),x=j.items.find(v=>v.id===${JSON.stringify(normalRun)});return x?.status==='completed'})()`,'independent Admin approval');
  const adminCapture=await evaluate(`document.querySelector('.research-browser-capture')?.innerText||''`);
  if(!adminCapture.includes('NO_COOKIE')||!adminCapture.includes('synthetic-f20-citation-evidence')||!adminCapture.includes('bounded requests'))throw Error('Cookie-free two-page capture was not visible in the Admin UI.');
  if(await evaluate(`document.querySelectorAll('.research-browser-capture article.card').length!==2`))throw Error('Admin UI did not show exactly two approved pages.');
  if(mutationCount!==0||unapprovedOriginHits!==0)throw Error('A blocked write or unapproved origin reached the synthetic site.');
  const duplicateApproval=await api('POST',`/api/legacy-browser/runs/${normalRun}/approve`);
  if(duplicateApproval.status!==409)throw Error('Completed request could be approved a second time.');
  await waitFor(`Boolean(document.querySelector('.research-browser-capture')?.innerText.includes('synthetic-f20-citation-evidence'))`,'Admin-visible captured evidence');
  if(await evaluate('document.documentElement.scrollWidth>window.innerWidth'))throw Error('Desktop review layout overflowed.');

  stage='Admin self-approval denial';
  const ownRun=await submitRequest({label:'F20 self approval guard '+Date.now(),url:'http://127.0.0.1:63120/'});
  const ownApproval=await api('POST',`/api/legacy-browser/runs/${ownRun}/approve`);
  if(ownApproval.status!==403)throw Error('Requester self-approval was not denied.');
  const cancelled=await api('POST',`/api/legacy-browser/runs/${ownRun}/cancel`);
  if(cancelled.status!==200||cancelled.body.status!=='cancelled')throw Error('Pending requester cancellation failed.');

  stage='Transient failure and approved retry';
  await logout();await login(employee);
  const retryRun=await submitRequest({label:'F20 transient capture retry '+Date.now(),url:'http://127.0.0.1:63120/flaky'});
  const employeeApproval=await api('POST',`/api/legacy-browser/runs/${retryRun}/approve`);
  if(employeeApproval.status!==403)throw Error('Employee direct approval was not denied.');
  await logout();await login(admin);
  await clickRunAction(retryRun,'Approve and capture');
  await waitFor(`(async()=>{const r=await fetch('/api/legacy-browser/runs',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;const j=await r.json(),x=j.items.find(v=>v.id===${JSON.stringify(retryRun)});return x?.status==='failed'&&x?.attempts===1})()`,'first transient failure');
  await waitFor(`(async()=>{const r=await fetch('/api/legacy-browser/runs',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;const j=await r.json(),x=j.items.find(v=>v.id===${JSON.stringify(retryRun)});return x?.status==='failed'})()`,'failed run state visible to Admin');
  await clickRunAction(retryRun,'Approve one retry');
  await waitFor(`(async()=>{const r=await fetch('/api/legacy-browser/runs',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;const j=await r.json(),x=j.items.find(v=>v.id===${JSON.stringify(retryRun)});return x?.status==='completed'&&x?.attempts===2})()`,'approved retry completion');
  const latest=await api('GET','/api/legacy-browser/runs');
  const retryRecord=latest.body.items.find(run=>run.id===retryRun);
  if(!retryRecord||retryRecord.status!=='completed'||retryRecord.attempts!==2)throw Error('Retry completion readback did not match the audit state.');
  await waitFor(`Boolean(document.querySelector('.research-browser-capture')?.innerText.includes('Transient fetch recovered on approved retry.'))`,'retry result visible');
  if(JSON.stringify(latest.body).includes('Transient fetch recovered on approved retry.')||JSON.stringify(latest.body).includes('synthetic-f20-citation-evidence'))throw Error('Captured browser text was persisted in request metadata.');
  await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  if(await evaluate('document.documentElement.scrollWidth>window.innerWidth||window.innerWidth!==390'))throw Error('Mobile research/review layout overflowed.');
  const screenshot=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
  const screenshotPath=path.join(evidence,'f20-legacy-browser-390.png');
  writeFileSync(screenshotPath,Buffer.from(screenshot.data,'base64'));
  console.log('F20_OPERATOR_ALLOWLIST_AND_STATUS=PASS');
  console.log('F20_EMPLOYEE_REQUEST_AND_APPROVAL_DENIAL=PASS');
  console.log('F20_ADMIN_INDEPENDENT_APPROVAL_AND_VISIBLE_CAPTURE=PASS pages=2');
  console.log('F20_COOKIE_POST_EXTERNAL_ORIGIN_AND_DUPLICATE_GUARDS=PASS');
  console.log('F20_REQUESTER_SELF_APPROVAL_DENIAL_AND_CANCEL=PASS');
  console.log('F20_TRANSIENT_FAILURE_RETRY=PASS attempts=2');
  console.log('F20_MOBILE_390PX=PASS '+screenshotPath);
  console.log('F20_BROWSER_ACCEPTANCE=PASS');
}catch(error){
  console.error(`F20_BROWSER_ACCEPTANCE=FAIL stage=${stage} error=${error?.message||'unknown'}`);
  process.exitCode=1;
}finally{
  try{socket?.close();}catch{}
  if(chromeProcess?.exitCode===null){chromeProcess.kill();await Promise.race([new Promise(resolve=>chromeProcess.once('exit',resolve)),delay(3000)]);}
  await new Promise(resolve=>server.close(()=>resolve()));
}
