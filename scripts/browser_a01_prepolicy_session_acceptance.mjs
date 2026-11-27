import {existsSync,mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {setTimeout as delay} from 'node:timers/promises';

const root=path.join(process.cwd(),'.verification-local-review-20261027-native','a01-org-mfa-current-20261027');
const base='http://127.0.0.1:5182/admin/';
const fixture=JSON.parse(readFileSync(path.join(root,'legacy-session.json'),'utf8'));
const chrome='C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const profile=path.join(root,'chrome-prepolicy-'+Date.now());
const evidence=path.join(root,'evidence');mkdirSync(evidence,{recursive:true});
let chromeProcess,socket,seq=0;const pending=new Map();
function cdp(method,params={}){const id=++seq;return new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('CDP timeout: '+method)),30000);pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));});}
async function evaluate(expression){const r=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error('Browser expression failed: '+(r.exceptionDetails.exception?.description||r.exceptionDetails.text||'unknown').split('\n')[0].slice(0,120));return r.result?.value;}
async function waitFor(expression,label){const until=Date.now()+30000;while(Date.now()<until){if(await evaluate(expression))return;await delay(200);}throw Error('Timed out waiting for '+label+'.');}
async function api(url){return evaluate(`(async()=>{const r=await fetch(${JSON.stringify(url)},{credentials:'same-origin',cache:'no-store'});let body=null;try{body=await r.json()}catch{}return {status:r.status,body}})()`);}
try{
  if(!fixture?.session_token)throw Error('Protected old-session fixture is unavailable.');
  if(!existsSync(chrome))throw Error('Local Chrome was unavailable.');
  chromeProcess=spawn(chrome,['--headless=new','--incognito','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,'about:blank'],{stdio:'ignore',windowsHide:true});
  const active=path.join(profile,'DevToolsActivePort'),deadline=Date.now()+30000;while(!existsSync(active)&&Date.now()<deadline)await delay(100);
  if(!existsSync(active))throw Error('Chrome did not create its DevTools endpoint.');
  const port=readFileSync(active,'utf8').split(/\r?\n/)[0];const page=(await(await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(x=>x.type==='page');
  if(!page)throw Error('Chrome page target was unavailable.');socket=new WebSocket(page.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
  socket.addEventListener('message',event=>{const m=JSON.parse(event.data),p=pending.get(m.id);if(!p)return;clearTimeout(p.timer);pending.delete(m.id);m.error?p.reject(Error('CDP command failed.')):p.resolve(m.result);});
  await cdp('Page.enable');await cdp('Runtime.enable');await cdp('Network.enable');
  const cookie=await cdp('Network.setCookie',{url:base,name:'infraspine_session',value:fixture.session_token,path:'/',httpOnly:true,secure:false,sameSite:'Strict'});
  if(cookie.success!==true)throw Error('Chrome refused the protected synthetic session cookie.');
  await cdp('Page.navigate',{url:base+'#/'});
  await waitFor(`(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;const x=await r.json();return x.mfa_reauthentication_required===true})()`,'pre-policy session state');
  await waitFor(`Boolean(document.body.innerText.includes('Verify MFA to continue'))`,'mandatory reauthentication UI');
  const identity=await api('/api/auth/me');const protectedResult=await api('/api/admin/deployment');
  if(identity.status!==200||!identity.body.mfa_reauthentication_required||protectedResult.status!==403||protectedResult.body?.error?.code!=='mfa_reauthentication_required')throw Error('Pre-policy session was not restricted to reauthentication.');
  await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  if(await evaluate('document.documentElement.scrollWidth>window.innerWidth||window.innerWidth!==390'))throw Error('Reauthentication UI overflowed at 390px.');
  const screenshot=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});const target=path.join(evidence,'a01-org-mfa-reauth-390.png');writeFileSync(target,Buffer.from(screenshot.data,'base64'));
  console.log('A01_PREPOLICY_SESSION_REAUTHENTICATION_API_AND_UI=PASS');console.log('A01_PREPOLICY_SESSION_390PX=PASS '+target);
}catch(error){console.error('A01_PREPOLICY_SESSION_ACCEPTANCE=FAIL '+(error?.message||'unknown'));globalThis.process.exitCode=1;}finally{try{socket?.close();}catch{}if(chromeProcess?.exitCode===null){chromeProcess.kill();await Promise.race([new Promise(resolve=>chromeProcess.once('exit',resolve)),delay(3000)]);}}
