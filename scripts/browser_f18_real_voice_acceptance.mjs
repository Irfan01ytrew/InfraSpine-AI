import {spawn} from "node:child_process";
import {existsSync,mkdirSync,readFileSync,writeFileSync} from "node:fs";
import path from "node:path";
import {setTimeout as delay} from "node:timers/promises";
const root=path.resolve(".verification-local-review-20261027-native/f18-current-source"),base="http://127.0.0.1:5180/admin/";
const fixture=JSON.parse(readFileSync(path.join(root,"review-credentials.json"),"utf8")),audio=readFileSync(path.join(root,"f18-spoken-question.wav")),pdf=readFileSync(path.join(root,"f18-refund-policy-source.pdf"));
const chrome="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",profile=path.join(root,"f18-profile-upload-"+Date.now()),evidence=path.join(root,"evidence");
mkdirSync(evidence,{recursive:true});let child,socket,n=0;const pending=new Map();
function call(method,params={}){const id=++n;return new Promise((resolve,reject)=>{const t=setTimeout(()=>reject(Error("CDP timeout: "+method)),30000);pending.set(id,{resolve,reject,t});socket.send(JSON.stringify({id,method,params}));});}
async function ev(expression){const r=await call("Runtime.evaluate",{expression,awaitPromise:true,returnByValue:true,userGesture:true});if(r.exceptionDetails)throw Error("Browser script failed");return r.result?.value;}
async function wait(expression,label,ms=90000){const end=Date.now()+ms;while(Date.now()<end){if(await ev(expression))return;await delay(300);}throw Error("Timed out: "+label);}
async function login(a){const x="(()=>{const f=document.querySelector('form.login-form'),e=f?.querySelector('[name=email]'),p=f?.querySelector('[name=password]');if(!f||!e||!p)return false;const set=(x,v)=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(x,v);x.dispatchEvent(new Event('input',{bubbles:true}));x.dispatchEvent(new Event('change',{bubbles:true}))};set(e,"+JSON.stringify(a.email)+");set(p,"+JSON.stringify(a.password)+");f.requestSubmit();return true})()";await ev(x);await wait("(async()=>{const r=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;return (await r.json()).email==="+JSON.stringify(a.email)+"})()","Admin sign-in");}
function attach(selector,filename,mime,bytes){const b64=bytes.toString("base64");return "(()=>{const i=document.querySelector("+JSON.stringify(selector)+");if(!i)return false;const raw=atob("+JSON.stringify(b64)+"),data=new Uint8Array(raw.length);for(let n=0;n<raw.length;n++)data[n]=raw.charCodeAt(n);const t=new DataTransfer();t.items.add(new File([data],"+JSON.stringify(filename)+",{type:"+JSON.stringify(mime)+"}));i.files=t.files;i.dispatchEvent(new Event('change',{bubbles:true}));return true})()";}
try{
 if(!existsSync(chrome))throw Error("Chrome not installed");
 child=spawn(chrome,["--headless=new","--disable-gpu","--no-first-run","--no-default-browser-check","--disable-background-networking","--remote-debugging-port=0","--user-data-dir="+profile,base],{stdio:"ignore",windowsHide:true});
 const active=path.join(profile,"DevToolsActivePort"),end=Date.now()+30000;while(!existsSync(active)&&Date.now()<end)await delay(100);if(!existsSync(active))throw Error("Chrome did not start");
 const port=readFileSync(active,"utf8").split(/\r?\n/)[0],page=(await(await fetch("http://127.0.0.1:"+port+"/json/list")).json()).find(x=>x.type==="page");
 socket=new WebSocket(page.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{socket.addEventListener("open",resolve,{once:true});socket.addEventListener("error",reject,{once:true});});
 socket.addEventListener("message",e=>{const m=JSON.parse(e.data),p=pending.get(m.id);if(!p)return;clearTimeout(p.t);pending.delete(m.id);m.error?p.reject(Error("CDP failed")):p.resolve(m.result);});
 await call("Page.enable");await call("Runtime.enable");await call("Page.navigate",{url:base});await wait("Boolean(document.querySelector('form.login-form'))","login page");await login(fixture.admin);
 if(await ev("document.querySelector('h1')?.textContent==='Company setup'"))throw Error("Review company setup is incomplete");
 await ev("(()=>{const original=window.fetch.bind(window);window.fetch=async(...args)=>{const response=await original(...args);if(String(args[0]).includes('/api/rag/voice-question')){try{const body=await response.clone().json();window.__f18Result={status:response.status,transcript:body.transcript,citations:(body.answer?.citations||[]).map(x=>x.filename),answer:body.answer?.answer,insufficient:body.answer?.insufficient_evidence}}catch{window.__f18Result={status:response.status}}}return response}})()");
 await ev("window.location.hash='/'");await wait("Boolean(document.querySelector('.voice-question-button')&&document.querySelector('input[type=file][accept*=\\\".pdf\\\"]'))","knowledge UI");
 const existing=await ev("(async()=>{const r=await fetch('/api/rag/documents',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;const x=await r.json(),d=x.documents.find(x=>x.filename==='f18-refund-policy-source.pdf');if(d)window.__f18Document=d;return Boolean(d&&d.status==='ready'&&d.searchable&&d.chunk_count>0)})()");
 if(!existing&&!await ev(attach('input[type=file][accept*=".pdf"]',"f18-refund-policy-source.pdf","application/pdf",pdf)))throw Error("PDF upload input missing");
 await wait("(async()=>{const r=await fetch('/api/rag/documents',{credentials:'same-origin',cache:'no-store'});if(!r.ok)return false;const x=await r.json(),d=x.documents.find(x=>x.filename==='f18-refund-policy-source.pdf');if(d)window.__f18Document=d;return Boolean(d&&d.status==='ready'&&d.searchable&&d.chunk_count>0)})()","worker parsing, embedding and API FAISS sync",180000);
 const doc=await ev("window.__f18Document");if(!doc||!doc.searchable||doc.chunk_count<1)throw Error("PDF is not indexed and searchable");
 if(!await ev(attach('input[aria-label="Choose a short audio question"]',"synthetic-question.wav","audio/wav",audio)))throw Error("Audio file control not available");
 await wait("Boolean(window.__f18Result)","real voice response",90000);const result=await ev("window.__f18Result");
 if(result.status!==200||result.transcript!=="What does the refund policy say about the return period?"||!result.citations?.includes("f18-refund-policy-source.pdf")||result.insufficient)throw Error("Voice API did not return local transcription and cited source");
 await wait("document.body.innerText.includes('What does the refund policy say about the return period?')&&[...document.querySelectorAll('.source-name')].some(x=>x.innerText==='f18-refund-policy-source.pdf')","transcript and cited source rendered");
 const citationVisible=await ev("Boolean(document.querySelector('.sources-label')&&[...document.querySelectorAll('.source-name')].some(x=>x.innerText==='f18-refund-policy-source.pdf'))");
 if(!citationVisible)throw Error("Cited answer source chip did not render in the UI");
 if(!await ev("document.documentElement.scrollWidth<=window.innerWidth"))throw Error("Desktop layout overflow");
 const malformed=await ev("(async()=>{const f=new FormData();f.append('audio',new File([new Uint8Array([1,2,3,4])],'malformed.wav',{type:'audio/wav'}));const r=await fetch('/api/rag/voice-question',{method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineAdmin'},body:f});return r.status})()");
 if(!(malformed>=400&&malformed<500))throw Error("Malformed audio was not rejected");
 const oversized=await ev("(async()=>{const f=new FormData();f.append('audio',new File([new Uint8Array(8*1024*1024+1)],'oversized.wav',{type:'audio/wav'}));const r=await fetch('/api/rag/voice-question',{method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'InfraSpineAdmin'},body:f});return r.status})()");
 if(oversized<400||oversized>=500)throw Error("Over-limit audio was not rejected");
 await call("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});if(!await ev("document.documentElement.scrollWidth<=window.innerWidth&&window.innerWidth===390"))throw Error("Mobile layout overflow");
 const shot=await call("Page.captureScreenshot",{format:"png",captureBeyondViewport:true}),image=path.join(evidence,"f18-real-voice-cited-390.png");writeFileSync(image,Buffer.from(shot.data,"base64"));
 console.log("F18_WORKER_PDF_INDEXED_LOCAL_MINILM_FAISS=PASS document="+doc.filename+" chunks="+doc.chunk_count);
 console.log("F18_BROWSER_LOCAL_WHISPER_TRANSCRIPTION=PASS");console.log("F18_BROWSER_CITED_ANSWER=PASS source="+result.citations.join(","));
 console.log("F18_BROWSER_MALFORMED_AUDIO_REJECTED="+malformed);console.log("F18_BROWSER_OVERSIZE_AUDIO_REJECTED="+oversized);
 console.log("F18_BROWSER_MOBILE_390PX=PASS");console.log("F18_BROWSER_ACCEPTANCE=PASS "+image);
}catch(e){let diag="";try{diag=JSON.stringify(await ev("({doc:window.__f18Document&&{filename:window.__f18Document.filename,status:window.__f18Document.status,searchable:window.__f18Document.searchable,chunks:window.__f18Document.chunk_count},status:window.__f18Result?.status,transcript:window.__f18Result?.transcript,citations:window.__f18Result?.citations,insufficient:window.__f18Result?.insufficient})"));}catch{}console.error("F18_BROWSER_ACCEPTANCE=FAIL "+e.message+" evidence="+diag);process.exitCode=1;
}finally{try{socket?.close();}catch{}if(child?.exitCode===null){child.kill();await Promise.race([new Promise(r=>child.once("exit",r)),delay(3000)]);}}
