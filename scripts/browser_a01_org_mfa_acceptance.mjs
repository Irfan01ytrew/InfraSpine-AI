import {createHmac} from 'node:crypto';
import {existsSync,mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {setTimeout as delay} from 'node:timers/promises';

const repo=process.cwd();
const root=path.join(repo,'.verification-local-review-20261027-native','a01-org-mfa-current-20261027');
const base='http://127.0.0.1:5182/admin/';
const credentials=JSON.parse(readFileSync(path.join(root,'review-credentials.json'),'utf8'));
const admin=credentials.admin;
const employee=credentials.employees[0];
const oldSession=JSON.parse(readFileSync(path.join(root,'legacy-session.json'),'utf8'));
const chrome='C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const profile=path.join(root,'chrome-profile-'+Date.now());
const evidence=path.join(root,'evidence');
mkdirSync(evidence,{recursive:true});
if(!admin?.email||!admin?.password||!employee?.email||!employee?.password||!oldSession?.session_token)throw Error('Protected synthetic QA fixtures are incomplete.');

let chromeProcess,socket,sequence=0,stage='startup';
const pending=new Map();
function cdp(method,params={}){
  const id=++sequence;
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(Error('CDP timeout: '+method)),30000);
    pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));
  });
}
async function evaluate(expression){
  const response=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true,userGesture:true});
  if(response.exceptionDetails){let message=response.exceptionDetails.exception?.description||response.exceptionDetails.text||'unknown';for(const secret of [admin.password,employee.password,oldSession.session_token])message=message.replaceAll(secret,'[redacted]');throw Error('Browser expression failed: '+message.split('\n')[0].slice(0,150));}
  return response.result?.value;
}
async function waitFor(expression,label,timeout=30000){
  const until=Date.now()+timeout;while(Date.now()<until){if(await evaluate(expression))return;await delay(200);}throw Error('Timed out waiting for '+label+'.');
}
function setForm(selector,values){
  return `(()=>{const f=document.querySelector(${JSON.stringify(selector)});if(!f)return false;const nodes=[...f.querySelectorAll('input,textarea')];if(nodes.length!==${values.length})return false;const set=(n,v)=>{const p=n.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(n,v);n.dispatchEvent(new Event('input',{bubbles:true}));n.dispatchEvent(new Event('change',{bubbles:true}))};${values.map((v,i)=>`set(nodes[${i}],${JSON.stringify(v)});`).join('')}f.requestSubmit();return true})()`;
}
function submitNamedField(formSelector,inputSelector,value){
  return `(()=>{const f=document.querySelector(${JSON.stringify(formSelector)}),n=f?.querySelector(${JSON.stringify(inputSelector)});if(!f||!n)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(n,${JSON.stringify(value)});n.dispatchEvent(new Event('input',{bubbles:true}));n.dispatchEvent(new Event('change',{bubbles:true}));f.requestSubmit();return true})()`;
}
async function api(method,url,payload){
  return evaluate(`(async()=>{const init={method:${JSON.stringify(method)},credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'A01QA'}};${payload===undefined?'':`init.headers['Content-Type']='application/json';init.body=JSON.stringify(${JSON.stringify(payload)});`}const r=await fetch(${JSON.stringify(url)},init);let body=null;try{body=await r.json()}catch{}return {status:r.status,body}})()`);
}
async function login(account){
  stage='login '+account.email;
  if(!await evaluate(`(()=>{const f=document.querySelector('form.login-form'),e=f?.querySelector('[name=email]'),p=f?.querySelector('[name=password]');if(!f||!e||!p)return false;const set=(x,v)=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(x,v);x.dispatchEvent(new Event('input',{bubbles:true}));x.dispatchEvent(new Event('change',{bubbles:true}))};set(e,${JSON.stringify(account.email)});set(p,${JSON.stringify(account.password)});f.requestSubmit();return true})()`))throw Error('Login form was unavailable.');
  await waitFor(`Boolean(document.querySelector('.account-security-page')||document.querySelector('.login-form input[name="code"]')||document.querySelector('.nav'))`,'login destination');
}
async function logout(){
  if(!await evaluate(`(()=>{const b=document.querySelector('button[aria-label="Sign out"]');if(!b)return false;b.click();return true})()`))throw Error('Sign-out control was unavailable.');
  await waitFor(`Boolean(document.querySelector('form.login-form'))`,'signed-out state');
}
function decodeBase32(value){
  const alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';let bits=0,acc=0,out=[];
  for(const c of value.toUpperCase().replace(/=+$/,'')){const n=alphabet.indexOf(c);if(n<0)throw Error('Invalid test TOTP setup key.');acc=(acc<<5)|n;bits+=5;if(bits>=8){bits-=8;out.push((acc>>bits)&255);}}
  return Buffer.from(out);
}
function totp(secret,counter=Math.floor(Date.now()/30000)){
  const message=Buffer.alloc(8);message.writeBigUInt64BE(BigInt(counter));const digest=createHmac('sha1',decodeBase32(secret)).update(message).digest();const offset=digest[digest.length-1]&15;const n=(digest.readUInt32BE(offset)&0x7fffffff)%1000000;return String(n).padStart(6,'0');
}
async function newerCode(secret,previousCounter){
  const deadline=Date.now()+35000;
  while(Date.now()<deadline){const counter=Math.floor(Date.now()/30000);if(counter>previousCounter)return {counter,code:totp(secret,counter)};await delay(150);}
  throw Error('Could not advance to a fresh TOTP time step.');
}
async function enrollCurrentAccount(account){
  stage='forced MFA enrollment UI';
  await waitFor(`Boolean(document.querySelector('.account-security-page h1')?.innerText.includes('Account security'))`,'forced account-security screen');
  const identity=await api('GET','/api/auth/me');
  if(identity.status!==200||identity.body.email!==account.email||identity.body.mfa_enrollment_required!==true)throw Error('MFA enrollment was not required for this account.');
  const denied=await api('GET','/api/admin/deployment');
  if(denied.status!==403||denied.body?.error?.code!=='mfa_enrollment_required')throw Error('Protected API was available before factor enrollment.');
  if(!await evaluate(setForm('.account-security-form',[account.password])))throw Error('MFA enrollment password form was unavailable.');
  await waitFor(`Boolean(document.querySelector('input[aria-label="Authenticator setup key"]'))`,'TOTP setup key');
  const secret=await evaluate(`document.querySelector('input[aria-label="Authenticator setup key"]')?.value||''`);
  if(!secret)throw Error('Authenticator setup key was not returned to the signed-in account.');
  const enrollmentCounter=Math.floor(Date.now()/30000);
  if(!await evaluate(submitNamedField('.account-security-form','[name=code]',totp(secret,enrollmentCounter))))throw Error('TOTP confirmation form was unavailable.');
  await waitFor(`Boolean(document.querySelector('.account-security-codes'))`,'one-use recovery-code confirmation');
  if(await evaluate(`document.querySelector('.account-security-codes pre')?.innerText.split('\\n').length!==10`))throw Error('The recovery-code confirmation did not show the expected count.');
  await waitFor(`(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;const x=await r.json();return x.email===${JSON.stringify(account.email)}&&!x.mfa_enrollment_required&&!x.mfa_reauthentication_required})()`,'enrollment gate removal');
  if(!await evaluate(`(()=>{const b=[...document.querySelectorAll('.account-security-codes button')].find(x=>x.innerText.trim()==='I saved these recovery codes');if(!b)return false;b.click();return true})()`))throw Error('The recovery-code acknowledgement action was unavailable.');
  await waitFor(`Boolean(document.querySelector('.nav')||document.querySelector('.first-run-setup-header'))`,'post-enrollment app');
  const status=await api('GET','/api/auth/mfa/status');
  if(status.status!==200||!status.body.enabled||!status.body.required_by_organization)throw Error('Persisted organization MFA status was not enabled.');
  if(await evaluate(`document.body.innerText.includes('Turn off MFA')`))throw Error('The mandatory MFA UI exposed a disable action.');
  const disable=await api('POST','/api/auth/mfa/disable',{password:account.password,code:'000000'});
  if(disable.status!==403||disable.body?.error?.code!=='forbidden')throw Error('The API allowed MFA disable while organization policy is active.');
  return {secret,enrollmentCounter};
}

try{
  if(!existsSync(chrome))throw Error('Local Chrome was unavailable.');
  stage='Chrome startup';
  chromeProcess=spawn(chrome,['--headless=new','--incognito','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,base],{stdio:'ignore',windowsHide:true});
  const active=path.join(profile,'DevToolsActivePort');const deadline=Date.now()+30000;
  while(!existsSync(active)&&Date.now()<deadline)await delay(100);
  if(!existsSync(active))throw Error('Chrome did not create its DevTools endpoint.');
  const port=readFileSync(active,'utf8').split(/\r?\n/)[0];
  const page=(await(await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(x=>x.type==='page');
  if(!page)throw Error('Chrome page target was unavailable.');
  socket=new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
  socket.addEventListener('message',event=>{const m=JSON.parse(event.data),item=pending.get(m.id);if(!item)return;clearTimeout(item.timer);pending.delete(m.id);m.error?item.reject(Error('CDP command failed.')):item.resolve(m.result);});
  await cdp('Page.enable');await cdp('Runtime.enable');await cdp('Network.enable');
  await waitFor(`Boolean(document.querySelector('form.login-form'))`,'initial login page');

  await login(admin);
  const adminInitial=await api('GET','/api/auth/me');
  if(adminInitial.status!==200||!adminInitial.body.mfa_enrollment_required)throw Error('Admin did not receive organization-required enrollment.');
  await enrollCurrentAccount(admin);
  console.log('A01_ADMIN_FORCED_ENROLLMENT_AND_DISABLE_DENIAL=PASS');
  await logout();

  await login(employee);
  const employeeFactor=await enrollCurrentAccount(employee);
  console.log('A01_EMPLOYEE_FORCED_ENROLLMENT_AND_API_GATE=PASS');
  await logout();

  stage='password plus TOTP login';
  await login(employee);
  await waitFor(`Boolean(document.querySelector('.login-form input[name="code"]'))`,'second-factor challenge');
  const beforeVerify=await api('GET','/api/auth/me');
  if(beforeVerify.status!==401)throw Error('Password-only login issued an authenticated session.');
  const freshCode=await newerCode(employeeFactor.secret,employeeFactor.enrollmentCounter);
  if(!await evaluate(setForm('.login-form',[freshCode.code])))throw Error('MFA challenge form was unavailable.');
  await waitFor(`(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;return (await r.json()).email===${JSON.stringify(employee.email)}})()`,'TOTP-authenticated employee session');
  const employeeAccess=await api('GET','/api/admin/deployment');
  if(employeeAccess.status!==403||employeeAccess.body?.error?.code!=='forbidden')throw Error('Employee role check did not resume after successful MFA.');
  const verified=await api('GET','/api/auth/me');
  if(verified.status!==200||verified.body.mfa_enrollment_required||verified.body.mfa_reauthentication_required)throw Error('MFA-verified session retained an enrollment gate.');
  console.log('A01_SECOND_FACTOR_LOGIN_AND_ROLE_AUTHORIZATION=PASS');
  await logout();

  stage='pre-policy session reauthentication';
  await cdp('Network.setCookie',{url:base,name:'infraspine_session',value:oldSession.session_token,path:'/',httpOnly:true,secure:false,sameSite:'Strict'});
  await cdp('Page.navigate',{url:base+'#/'});
  await waitFor(`Boolean(document.body.innerText.includes('Verify MFA to continue'))`,'old-session MFA reauthentication screen');
  const oldIdentity=await api('GET','/api/auth/me');
  const oldProtected=await api('GET','/api/admin/deployment');
  if(oldIdentity.status!==200||!oldIdentity.body.mfa_reauthentication_required||oldProtected.status!==403||oldProtected.body?.error?.code!=='mfa_reauthentication_required')throw Error('A pre-policy unverified session retained access.');
  await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  if(await evaluate('document.documentElement.scrollWidth>window.innerWidth||window.innerWidth!==390'))throw Error('Mandatory MFA screen overflowed at 390px.');
  const shot=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
  const shotPath=path.join(evidence,'a01-org-mfa-reauth-390.png');writeFileSync(shotPath,Buffer.from(shot.data,'base64'));
  console.log('A01_PREPOLICY_SESSION_REAUTHENTICATION_AND_390PX=PASS');
  console.log('A01_BROWSER_ACCEPTANCE=PASS '+shotPath);
}catch(error){
  console.error(`A01_BROWSER_ACCEPTANCE=FAIL stage=${stage} error=${error?.message||'unknown'}`);process.exitCode=1;
}finally{
  try{socket?.close();}catch{}
  if(chromeProcess?.exitCode===null){chromeProcess.kill();await Promise.race([new Promise(resolve=>chromeProcess.once('exit',resolve)),delay(3000)]);}
}
