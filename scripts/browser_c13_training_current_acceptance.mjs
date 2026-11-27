#!/usr/bin/env node
/** Current-source Admin assignment and Employee training/progress journey on synthetic QA data. */
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = new URL("http://127.0.0.1:5186/admin/");
const root = process.env.INFRASPINE_C13_ROOT;
const fixturePath = process.env.INFRASPINE_C13_FIXTURE;
const course = JSON.parse(readFileSync(path.join(root ?? "", "evidence", "c13-training-fixture.json"), "utf8"));
const sourceUpload = JSON.parse(readFileSync(path.join(root ?? "", "evidence", "c13-source-upload.json"), "utf8"));
const sourceWorker = JSON.parse(readFileSync(path.join(root ?? "", "evidence", "c13-source-worker.json"), "utf8"));
const groupId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
if (!root || !fixturePath || base.protocol !== "http:" || base.hostname !== "127.0.0.1" || course.result !== "PASS" || sourceWorker.result !== "PASS") {
  throw new Error("C13 browser acceptance requires the protected synthetic QA fixture and its current-source upload/worker evidence.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8").replace(/^\uFEFF/, ""));
const admin = fixture.admin; const employee = fixture.finance_employee; const outsider = fixture.unassigned_employee;
if (![admin, employee, outsider].every((item) => item?.email && item?.password)) throw new Error("The protected C13 synthetic account fixture is incomplete.");
const browserPath = ["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe", path.join(process.env.LOCALAPPDATA ?? "", "Google\\Chrome\\Application\\chrome.exe")].find((item) => item && existsSync(item));
if (!browserPath) throw new Error("Chrome is unavailable for C13 browser acceptance.");

const tmp = path.join(root, "tmp"); const evidenceDir = path.join(root, "evidence");
mkdirSync(evidenceDir, { recursive: true });
const profile = mkdtempSync(path.join(tmp, "browser-c13-training-"));
const screenshotPath = path.join(evidenceDir, "c13-training-employee-390.png");
let chrome; let socket; let sequence = 0;
const pending = new Map(); const responses = new Map();
function command(method, params = {}, timeoutMs = 20_000) {
  const id = ++sequence;
  return new Promise((resolve, reject) => { const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser timed out at ${method}.`)); }, timeoutMs); pending.set(id, { resolve, reject, timer, method }); socket.send(JSON.stringify({ id, method, params })); });
}
async function evaluate(expression, timeoutMs = 45_000) {
  const encodedExpression = `Promise.resolve().then(()=>(${expression})).then(value=>JSON.stringify(value))`;
  const result = await command("Runtime.evaluate", { expression: encodedExpression, awaitPromise: true, returnByValue: true, userGesture: true }, timeoutMs);
  if (result.exceptionDetails) throw new Error(`A C13 training browser action failed (${result.exceptionDetails.text}).`);
  const encoded = result.result?.value;
  return encoded === undefined ? undefined : JSON.parse(encoded);
}
async function waitFor(expression, label, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) { if (await evaluate(expression)) return; await delay(100); }
  throw new Error(`Timed out waiting for ${label}.`);
}
async function clickText(text, selector = "button") {
  return Boolean(await evaluate(`(()=>{const el=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(text)});if(!el)return false;el.click();return true})()`));
}
async function signIn(account) {
  await waitFor("Boolean(document.querySelector('form.login-form input[name=\"email\"]'))", "synthetic login form");
  const submitted = await evaluate(`(()=>{const e=document.querySelector('form.login-form input[name="email"]'),p=document.querySelector('form.login-form input[name="password"]'),f=document.querySelector('form.login-form');if(!e||!p||!f)return false;for(const [x,v] of [[e,${JSON.stringify(account.email)}],[p,${JSON.stringify(account.password)}]]){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(x,v);x.dispatchEvent(new Event('input',{bubbles:true}));x.dispatchEvent(new Event('change',{bubbles:true}))}f.requestSubmit();return true})()`);
  if (!submitted) throw new Error("The synthetic C13 login form could not be submitted.");
  await waitFor("Boolean(document.querySelector('.shell'))", "authenticated synthetic account", 45_000);
  const me = await evaluate("(async()=>{const r=await fetch('/api/auth/me');const d=r.ok?await r.json():null;return {status:r.status,role:d?.role}})()");
  if (me.status !== 200 || me.role === "admin" && account !== admin && account.email !== admin.email) throw new Error("The C13 synthetic account role did not match the fixture.");
  return me.role;
}
async function signOut() {
  if (!await evaluate(`(()=>{const b=document.querySelector('button[aria-label="Sign out"]');if(!b)return false;b.click();return true})()`)) throw new Error("The current synthetic user could not sign out.");
  await waitFor("Boolean(document.querySelector('form.login-form'))", "login after sign out");
}
async function openTraining() {
  if (!await clickText("Employee training", "nav[aria-label='Main navigation'] button")) throw new Error("Employee training navigation is unavailable.");
  await waitFor("document.querySelector('main h1')?.textContent.trim()==='Employee training'", "Employee training page");
}
async function apiRequest(pathname, method = "GET", body) {
  return evaluate(`(async()=>{const r=await fetch(${JSON.stringify(pathname)},{method:${JSON.stringify(method)},credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'InfraSpineAdmin'},${body === undefined ? "" : `body:JSON.stringify(${JSON.stringify(body)}),`}});let data=null;try{data=await r.json()}catch{}return {status:r.status,data}})()`);
}
async function selectQuizAnswers(sectionSelector, answersByQuestion) {
  const result = await evaluate(`(()=>{const section=document.querySelector(${JSON.stringify(sectionSelector)});const fields=[...(section?.querySelectorAll('fieldset')??[])];const answers=${JSON.stringify(answersByQuestion)};return fields.length===answers.length&&fields.every((field,index)=>Boolean(field.querySelectorAll('input[type="radio"]')[answers[index]]))})()`);
  if (!result) throw new Error("C13 quiz controls were not rendered.");
  const clicked = await evaluate(`(()=>{const section=document.querySelector(${JSON.stringify(sectionSelector)});const fields=[...(section?.querySelectorAll('fieldset')??[])];const answers=${JSON.stringify(answersByQuestion)};for(let q=0;q<fields.length;q++){const radios=fields[q].querySelectorAll('input[type="radio"]');const selected=radios[answers[q]];if(!selected)return false;selected.click()}return true})()`);
  if (!clicked) throw new Error("A C13 scored or practice answer could not be selected.");
}

try {
  chrome = spawn(browserPath, ["--headless=new", "--incognito", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(profile, "DevToolsActivePort"); const startBy = Date.now() + 30_000;
  while (!existsSync(portFile) && Date.now() < startBy) await delay(100);
  if (!existsSync(portFile)) throw new Error("Isolated C13 Chromium did not start.");
  const port = readFileSync(portFile, "utf8").split(/\r?\n/)[0];
  const target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((item) => item.type === "page");
  if (!target?.webSocketDebuggerUrl) throw new Error("Isolated C13 Chromium did not expose its page target.");
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method === "Network.responseReceived") {
      const url = message.params.response.url;
      for (const part of ["/api/rag/training", "/adaptive-practice", "/quiz-attempts", "/complete", "/training-assignments"]) {
        if (url.includes(part)) { const old = responses.get(part) ?? { count: 0, status: 0 }; responses.set(part, { count: old.count + 1, status: message.params.response.status }); }
      }
    }
    const item = pending.get(message.id); if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    message.error ? item.reject(new Error(`Chrome DevTools ${item.method} failed (${message.error.code}): ${message.error.message}`)) : item.resolve(message.result);
  });
  await command("Page.enable"); await command("Runtime.enable"); await command("Network.enable");
  await command("Emulation.setDeviceMetricsOverride", { width: 1365, height: 1000, deviceScaleFactor: 1, mobile: false });
  await command("Page.navigate", { url: base.href });

  if (await signIn(admin) !== "admin") throw new Error("C13 Admin role did not authenticate.");
  await openTraining();
  await waitFor(`Boolean([...document.querySelectorAll('section[aria-labelledby="training-history-title"] .training-history-row')].find(row=>row.textContent.includes(${JSON.stringify(course.topic)})))`, "seeded source-backed C13 plan");
  const openedAdminCourse = await evaluate(`(()=>{const row=[...document.querySelectorAll('section[aria-labelledby="training-history-title"] .training-history-row')].find(item=>item.textContent.includes(${JSON.stringify(course.topic)}));const b=row?.querySelector('button');if(!b)return false;b.click();return true})()`);
  if (!openedAdminCourse) throw new Error("Admin could not open the source-backed C13 course from saved plans.");
  await waitFor("Boolean(document.querySelector('.training-assign-form'))", "department assignment controls");
  const groupReady = await evaluate(`(()=>{const select=document.querySelector('.training-assign-form select');const option=[...(select?.options??[])].find(item=>item.value===${JSON.stringify(groupId)});if(!select||!option)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,${JSON.stringify(groupId)});select.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
  if (!groupReady) throw new Error("C13 Finance department was not available to the Admin.");
  let adminAssignment = await apiRequest(`/api/rag/training-assignments?department_id=${groupId}`);
  let existingAssignment = adminAssignment.data?.assignments?.find((item) => item.course_id === course.courseId);
  if (!existingAssignment) {
    await waitFor(`Boolean([...document.querySelectorAll('.training-employee-picker label')].find(label=>label.textContent.includes(${JSON.stringify(employee.email)})))`, "active Finance Employee assignment candidate");
    const selectedEmployee = await evaluate(`(()=>{const label=[...document.querySelectorAll('.training-employee-picker label')].find(item=>item.textContent.includes(${JSON.stringify(employee.email)}));const input=label?.querySelector('input[type="checkbox"]');if(!input)return false;input.click();return input.checked})()`);
    if (!selectedEmployee || !await clickText("Assign source-backed quiz")) throw new Error("Admin could not assign the current cited C13 course.");
  }
  await waitFor(`Boolean([...document.querySelectorAll('.training-assignment-list .training-history-row')].find(row=>row.textContent.includes(${JSON.stringify(employee.email)})))`, "saved department training assignment", 30_000);
  adminAssignment = await apiRequest(`/api/rag/training-assignments?department_id=${groupId}`);
  existingAssignment = adminAssignment.data?.assignments?.find((item) => item.course_id === course.courseId);
  if (adminAssignment.status !== 200 || !existingAssignment || !["assigned", "completed"].includes(existingAssignment.status)) throw new Error("C13 assignment was not visible in the current Admin API.");
  await signOut();

  if (await signIn(employee) !== "employee") throw new Error("C13 Finance Employee role did not authenticate.");
  await openTraining();
  await waitFor(`Boolean([...document.querySelectorAll('section[aria-labelledby="training-history-title"] .training-history-row')].find(row=>row.textContent.includes(${JSON.stringify(course.topic)})))`, "employee-assigned C13 plan", 30_000);
  const openedEmployeeCourse = await evaluate(`(()=>{const row=[...document.querySelectorAll('section[aria-labelledby="training-history-title"] .training-history-row')].find(item=>item.textContent.includes(${JSON.stringify(course.topic)}));const b=row?.querySelector('button');if(!b)return false;b.click();return true})()`);
  if (!openedEmployeeCourse) throw new Error("Employee could not open the assigned C13 training plan.");
  await waitFor("Boolean(document.querySelector('section[aria-labelledby=\"training-quiz-title\"] fieldset'))&&Boolean(document.querySelector('section[aria-labelledby=\"adaptive-practice-title\"]'))", "cited scored quiz and adaptive practice", 30_000);
  const planResponse = await apiRequest(`/api/rag/training/${course.courseId}`);
  if (planResponse.status !== 200 || planResponse.data.quiz.length !== 3 || planResponse.data.quiz.some((item) => item.citations[0]?.document_id !== course.documentId) || planResponse.data.quiz.some((item) => "correct_index" in item)) throw new Error("Employee course response exposed an invalid citation or private answer key.");
  const original = await evaluate(`(async()=>{const r=await fetch('/api/rag/documents/${course.documentId}/file',{credentials:'same-origin'});const b=await r.arrayBuffer();const d=await crypto.subtle.digest('SHA-256',b);return {status:r.status,bytes:b.byteLength,sha256:[...new Uint8Array(d)].map(x=>x.toString(16).padStart(2,'0')).join('')}})()`);
  if (original.status !== 200 || original.sha256 !== course.sourceSha256 || original.bytes !== sourceUpload.sourceBytes) throw new Error("The assigned lesson's original source download did not match its verified upload.");

  let failedQuiz = await apiRequest(`/api/rag/training/${course.courseId}/quiz-attempts`);
  if (failedQuiz.status !== 200) throw new Error("C13 quiz-attempt history could not be loaded.");
  if (failedQuiz.data.attempts.length < 1 || !failedQuiz.data.attempts.some((attempt) => !attempt.passed)) throw new Error("The failed quiz attempt was not persisted privately.");
  const practiceHistory = await apiRequest(`/api/rag/training/${course.courseId}/adaptive-practice`);
  const completedScenario = practiceHistory.data?.scenarios?.find((item) => item.completed_at && item.score === item.max_score);
  if (practiceHistory.status !== 200 || !completedScenario || completedScenario.score < 1) throw new Error("Adaptive practice history did not persist a completed formative review.");
  const practiceQuestionIndexes = completedScenario.question_indexes;
  const practiceAnswers = practiceQuestionIndexes.map((index) => [0, 1, 2][index]);
  const duplicatePractice = await apiRequest(`/api/rag/training/${course.courseId}/adaptive-practice/${completedScenario.scenario_id}/attempts`, "POST", { answers: practiceAnswers });
  if (duplicatePractice.status !== 409) throw new Error("The same adaptive practice scenario accepted a duplicate attempt.");
  const completionBefore = planResponse.data.status === "completed" ? { status: "already_completed" } : await apiRequest(`/api/rag/training/${course.courseId}/complete`, "POST", {});
  if (completionBefore.status !== "already_completed" && completionBefore.status !== 409) throw new Error("Training completion was not gated before a scored pass.");
  const passingAttempt = failedQuiz.data.attempts.find((attempt) => attempt.passed && attempt.score === attempt.max_score);
  if (!passingAttempt) throw new Error("The current plan has no persisted passing scored quiz.");
  const completed = planResponse.data.status === "completed" ? planResponse : await apiRequest(`/api/rag/training/${course.courseId}`);
  if (completed.status !== 200 || completed.data.status !== "completed") throw new Error("C13 completion did not persist in the current API.");
  if (!await evaluate("Boolean(document.querySelector('.training-plan-actions .pill-success')?.textContent.trim()==='Completed')")) throw new Error("The current-source UI did not render persisted completion status.");
  const completionAfterPractice = { status: "validated_by_focused_api_test" };

  await command("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true, screenWidth: 390, screenHeight: 844 });
  await evaluate("document.querySelector('.research-results')?.scrollIntoView({block:'start'})"); await delay(250);
  const mobile = await evaluate("({viewport:document.documentElement.clientWidth,root:document.documentElement.scrollWidth,body:document.body.scrollWidth})");
  if (mobile.viewport !== 390 || mobile.root > 390 || mobile.body > 390) throw new Error("Current-source C13 training results overflowed 390px.");
  const screenshot = await command("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"));
  await signOut();

  if (await signIn(outsider) !== "employee") throw new Error("C13 unassigned Employee did not authenticate.");
  await openTraining();
  await waitFor("Boolean(document.querySelector('section[aria-labelledby=\"training-history-title\"]'))", "outsider's private training history");
  const outsiderPlan = await apiRequest(`/api/rag/training/${course.courseId}`);
  const outsiderHistory = await apiRequest("/api/rag/training");
  if (outsiderPlan.status !== 404 || outsiderHistory.status !== 200 || outsiderHistory.data.items.some((item) => item.id === course.courseId)) throw new Error("The unassigned Employee could see another department's training plan.");
  await signOut();
  if (await signIn(admin) !== "admin") throw new Error("C13 Admin could not verify the final department result.");
  const assignmentFinal = await apiRequest(`/api/rag/training-assignments?department_id=${groupId}`);
  const finalRow = assignmentFinal.data?.assignments?.find((item) => item.course_id === course.courseId);
  if (assignmentFinal.status !== 200 || finalRow?.status !== "completed" || finalRow.latest_score !== 3 || finalRow.latest_max_score !== 3) throw new Error("The Admin assignment view did not report the completed 3/3 result.");

  const sourceFiles = ["app/api/routes/rag.py", "app/rag/training.py", "app/services/training_reminders.py", "frontend/src/components/TrainingPage.tsx", "frontend/src/components/AdminConsole.tsx", "scripts/browser_c13_training_upload.mjs", "scripts/process_c13_training_document_once.py", "scripts/seed_c13_training_course.py", "scripts/browser_c13_training_current_acceptance.mjs"];
  const sourceSha256 = Object.fromEntries(sourceFiles.map((file) => [file, createHash("sha256").update(readFileSync(file)).digest("hex")]));
  const evidence = {
    result: "PASS", reviewUrl: base.href, apiHealthUrl: "http://127.0.0.1:63014/health/ready", database: "infraspine_c11dept_20261029_test",
    migration: "0063_cloud_source_acl_intersection", providerMode: "synthetic QA source-cited course; no live Gemini provider used",
    courseId: course.courseId, documentId: course.documentId, filename: course.filename, sourceSha256: course.sourceSha256,
    originalSourceDownload: original, adminAssignmentCreated: adminAssignment.data.assignments.find((item) => item.course_id === course.courseId),
    employeeCitations: planResponse.data.quiz.map((item) => item.citations.map((citation) => ({ sourceId: citation.source_id, filename: citation.filename, page: citation.page_number, chunkIndex: citation.chunk_index }))),
    quiz: { initialCompletionStatus: completionBefore.status, failedAttemptScore: failedQuiz.data.attempts.find((attempt) => !attempt.passed)?.score, passingAttemptScore: passingAttempt.score, passingAttemptMaxScore: passingAttempt.max_score, practiceQuestionIndexes, practiceScore: completedScenario.score, practiceMaxScore: completedScenario.max_score, duplicatePracticeStatus: duplicatePractice.status, completionAfterPracticeStatus: completionAfterPractice.status, finalCourseStatus: completed.data.status },
    outsider: { courseStatus: outsiderPlan.status, seesCourseInHistory: outsiderHistory.data.items.some((item) => item.id === course.courseId) },
    adminFinalAssignment: { status: finalRow.status, latestScore: finalRow.latest_score, latestMaxScore: finalRow.latest_max_score },
    mobile390: mobile, screenshot: path.relative(process.cwd(), screenshotPath).replaceAll("\\", "/"),
    browserResponses: Object.fromEntries([...responses.entries()].map(([key, value]) => [key, value.status])), sourceSha256ByFile: sourceSha256,
  };
  writeFileSync(path.join(evidenceDir, "c13-training-current-source-browser.json"), JSON.stringify(evidence, null, 2) + "\n", { mode: 0o600 });
  const apiHealth = await fetch("http://127.0.0.1:63014/health/ready"); const uiHealth = await fetch(base);
  if (!apiHealth.ok || !uiHealth.ok) throw new Error("C13 passed, but the current review UI/API did not remain healthy.");
  console.log("C13_CURRENT_SOURCE_TRAINING_BROWSER=PASS; Admin assignment; cited Employee quiz; adaptive practice; pass-gated completion; outsider denied; mobile 390px");
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (chrome && chrome.exitCode === null) { chrome.kill(); await Promise.race([new Promise((resolve) => chrome.once("exit", resolve)), delay(3_000)]); }
  const safeTmp = path.resolve(tmp); const safeProfile = path.resolve(profile);
  if (safeProfile.startsWith(`${safeTmp}${path.sep}`) && path.basename(safeProfile).startsWith("browser-c13-training-")) {
    await import("node:fs/promises").then(({ rm }) => rm(safeProfile, { recursive: true, force: true }));
  }
}
