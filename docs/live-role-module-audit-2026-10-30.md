# Live role and module audit — 2026-10-30

## Scope and result

I audited the local application at http://127.0.0.1:5182/admin/ in the browser, signed in as the synthetic Administrator, opened every Admin-visible page, and exercised safe synthetic/read-only checks. This follow-up supersedes the earlier report's “API unavailable” conclusion: the API is now reachable, but the AI provider and the Approvals page have live failures.

The software is an internal company knowledge and automation platform. It stores company documents, indexes them for search, answers questions with source citations, and exposes task, knowledge-review, training, research, and administration tools. It is **not ready for release** because cited AI answers currently fail, the Approvals screen returns 502, and Employee/Manager access-control journeys were not verified.

## Architecture and running services

| Layer | What is running or configured | Evidence |
|---|---|---|
| Frontend | React 19 + TypeScript + Vite UI, served at port 5182 | Live Admin page opened and navigated. |
| Backend | FastAPI / Uvicorn at 127.0.0.1:63011 | /health/ready returned ready; database returned ok. |
| Database | Local PostgreSQL at 127.0.0.1:55929 | API readiness returned database ok. No schema migration or database edits were run. |
| Document worker | Separate ingestion mode; one worker online | Readiness returned worker status available and workers_online=1. |
| Document understanding | Local MiniLM sentence-transformer, 384 dimensions; FAISS index owner | Settings showed model loaded in memory after its local health test; Search index showed Healthy and 10 vectors. |
| AI answer provider | DeepSeek V4.1 Flash is active | Knowledge chat returned HTTP 402: insufficient API balance. No key, provider, or billing setting was changed. |
| Parser and OCR dependencies | pypdf, Docling, openpyxl, local text/CSV parsers, RapidOCR/ONNX Runtime | Built-in fixture checks are recorded below. |

The Vite /api route was reachable: an unauthenticated /api/auth/me request returned the expected 401. The signed-in UI continued to work. The old Vite log contains earlier connection-refused messages, but the current readiness endpoint is healthy; those old lines do not explain the current 402 or page-level 502.

Repository instructions: no AGENTS.md file was found. I read the README, role/permission guidance, Windows development guidance, and release-readiness notes. The README identifies React/Vite, FastAPI, PostgreSQL, a separate document worker, local sentence-transformer embeddings, and FAISS. I did not read .env, inspect passwords, change credentials, run migrations, or stop/restart services.

## How roles and sign-in work

There is **one internal sign-in screen** for Admin, Manager, and Employee. The person enters their assigned work email and password there; they do not choose a role at sign-in. The Admin assigns a person's role and departments in **People & access**. An invitation link is single-use and valid for 72 hours; the UI says the app does not email the link. The invited user accepts it, sets their password, then uses the same sign-in screen. The screen can ask for an authenticator or recovery code if MFA is enabled.

After sign-in, the app chooses the role's starting page and navigation:

| Role | What this role is for | What the app should show |
|---|---|---|
| Employee | Ask questions about documents the employee is allowed to read; manage assigned work; submit a knowledge claim or help request; complete training; share skills if in a department. | Starts in Knowledge. Admin-only configuration pages are not in the employee navigation. |
| Manager | Do employee work plus manage only assigned departments, team tasks, review/approval queues and department resources permitted to that manager. | Starts in Knowledge; adds My departments. Reports or employee invitations appear only if the manager has the matching permission. |
| Administrator | Set up company identity, departments, users, roles and grants; manage providers, integrations, workspaces, settings and audit. | Starts on Home and sees Administration pages. Department membership and document ACL still matter to department-only data. |
| External partner | Use a separately scoped workspace and assistant, not the internal employee portal. | Separate Partner portal route and partner credentials/workspace. Not tested because there is no partner workspace. |

The source code confirms the shared sign-in component and role-filtered navigation. Admin lands on Home; non-admin users land on Knowledge. Partner is a separate portal. Therefore **role-specific dashboards/menus exist, but separate internal login pages do not**. The safe design is one login that determines role from the account; a user-selectable “I am Admin/Manager/Employee” switch would be unsafe.

Account coverage in this live fixture:
- Administrator: signed in and tested.
- Employee: an active fixture account exists, but its password was not accessed or exposed. Audit history already includes repeated failed sign-ins and a lockout event, so I did not try passwords or risk another lockout.
- Manager: the visible Manager account is still a pending invitation, not a signed-in account. No Manager login journey could be run.
- No role account was created, unlocked, deactivated, promoted, or had its password changed.

## Asaan Roman Urdu: kis role ko kya karna hai

**Login:** Admin, Manager aur Employee ke liye aik hi internal login page hai. Apna assigned work email aur password daalain; role khud account se pata chalta hai. Admin pehle **People & access** mein user ko role aur department deta hai. Invite par user account activate karke password set karta hai, phir usi login page se aata hai. Admin ko **Home**, Manager aur Employee ko **Knowledge** se shuru karna chahiye. Login screen par role chunne ka dropdown nahi hai, aur har role ke liye alag internal login screen maujood nahi. Partner ka portal alag hai.

| App ka hissa | Seedhi zaban mein kis kaam ka hai | Kaun use kare | Is audit ka nateeja |
|---|---|---|---|
| Home | Company setup ki halat aur Admin ke shortcuts. | Admin | Khula; tamam setup steps complete dikhaye. |
| Knowledge / Library | Company ke documents rakhna aur file naam se dhoondhna. | Employee, Manager, Admin; sirf allowed documents | Synthetic document upload, processing aur search chalay. |
| Knowledge / Chat | Documents se sawal ka jawab aur source citation lena. | Employee, Manager, Admin; jawab unki access ke mutabiq | Abhi jawab nahi milta: AI provider 402 balance error deta hai. |
| My work | Apne diye gaye tasks, deadline aur calendar dekhna/update karna. | Employee; Manager apni team ke scope mein | Page khula; task ko assign se completion tak nahi chalaya. |
| Claims / Org knowledge | Nayi maloomat ya correction propose karna; review/publish ke baad hi official banana. | Employee/Manager propose; authorized reviewer/Admin review kare | Synthetic claim save hua aur refresh ke baad raha; Approvals 502 ki wajah se uska review test na hua. |
| Approvals | Pending claims/changes ko approve ya reject karna aur history rakhna. | Assigned reviewer/Manager/Admin, policy ke mutabiq | Dono panels 502 error. |
| Workflows, agents, notifications | Department ke repeat hone wale tasks automate karna, agent chalana, aur update notice dena. | Manager apne departments; Admin setup; Employee assigned task/notice dekhe | Workflow scoped department na hone se blocked; agent scope khaali; notifications lifecycle verify nahi. |
| Interviews, handoffs, expertise | Logon ke tajurbe se procedure banana, role-change handoff, aur share ki hui skills se colleague dhoondhna. | Employee/Manager apne department mein; Admin authorized scope mein | Interview template nahi; Admin ke paas department membership nahi, is liye handoff/skill edit nahi chale. |
| Employee training | Source ke mutabiq lesson/quiz banana, assign karna aur completion dekhna. | Admin/Manager banaye ya assign kare; Employee seekhe | Page khula; AI generation provider 402 ki wajah se nahi chali. |
| Research, tender/RFP, forecast | Company sources se research, tender requirements ka evidence, aur source numbers par “what-if” hisaab. | Authorized Employee/Manager/Admin | Forms khule; full run nahi hua. Market research Gemini connect na hone se disabled. |
| People & access | Accounts, departments, roles aur permissions sambhalna. | Admin; Manager ko sirf delegated permissions | Admin lists khuli; koi account/permission nahi badli. Manager invite pending. |
| Reports, traces, audit logs | Usage/error dekhna, AI request ka stage trace karna, aur kis ne kya action kiya uska audit dekhna. | Admin/scoped report viewers | Metrics, traces aur synthetic events dikhay; CSV download verify nahi hua; 9 sawalon mein 6 errors. |
| Settings / connectors / company setup | Search index, parsers/OCR, integrations, AI provider aur company profile configure karna. | Admin | Parsers, OCR aur index checks pass; external connectors configured nahi; provider key/billing nahi chheri. |

### Login aur alag role screens ka asaan tareeqa

1. Admin **People & access** mein har test user ka role aur department check kare; user ko usi ke apne assigned account se sign in karna hota hai. Password kabhi share ya print na karein.
2. Har role ka alag session dekhne ke liye alag browser profile/window khol kar us role ke apne account se login karein. Ek hi browser session ko role dropdown se badalna app ka design nahi.
3. Employee aur Manager ke liye pehle active, unlocked QA accounts chahiye. Is pass mein Manager sirf pending invite tha aur Employee par purana lockout record tha; is liye unke passwords azma kar lockout barhane ke bajaye yahin rok diya.
4. Role test mein pehle allowed document/task khol kar access verify karein, phir doosre department ka synthetic item dhoondh kar ensure karein ke woh nazar ya jawab mein shamil na ho. Yeh allow aur deny dono checks abhi **BLOCKED** hain.

### Jo abhi theek karna chahiye

1. AI provider account ka balance uske owner se privately restore karwain; phir synthetic sawal dobara chala kar sahi citation aur source link check karein. Saath hi retrieval ke 87-second delay ko profile karein.
2. Approvals ke dono 502 API errors fix karein; synthetic claim ko reviewer se approve aur reject dono raaston par test karein.
3. Authorized QA setup se Manager invite activate karein aur Employee account unlock karein; passwords yahan share na karein. Dono roles ke liye alag browser profiles mein login, department boundary ke allowed/denied checks chalain.
4. Usage CSV ka asal downloaded file khol kar headers aur aggregate data verify karein; success/failure message bhi dikhna chahiye.
5. Gemini sirf tab connect karein jab Market research deployment mein chahiye ho; phir non-sensitive synthetic sawal aur citations test karein.

## Module-by-module evidence

**Status meanings:** PASS = the stated end-to-end journey or built-in health test completed; PARTIAL = page or part of the journey worked but a meaningful part remains unverified; FAIL = the tested function returned a real error; BLOCKED = a required account, provider, department, data source, or safe test setup is absent. Admin was the only role tested live.

| Module | What it is and who uses it | Steps and observed result | Status |
|---|---|---|---|
| Home | Admin setup checklist and shortcuts. | Opened Home. It rendered 5/5 setup steps complete, including provider, departments, people, documents and agents. Navigation shortcuts opened their destinations. | PASS |
| Knowledge: upload and processing | Employee/Manager/Admin upload company sources that become indexed and searchable under their access rules. | Uploaded a clearly labelled synthetic text file. It reached Ready with 1 passage; after refresh it remained Ready. Worker online and the Search index page also showed the document vectors. | PASS |
| Knowledge: library search | Find/filter uploaded sources; search is separate from asking a question. | Searched the library for the synthetic filename: it appeared. Searched a non-existent synthetic filename: “No documents match” appeared. Cleared the filter and restored the full library. | PASS |
| Knowledge: AI chat, answer and citations | Ask a natural-language question over permitted documents; receive evidence-backed answer with citations. | Asked only for facts in the new synthetic test document. It first showed “Searching your documents,” then returned “DeepSeek has insufficient API balance (HTTP 402).” Trace showed 89.2 seconds, 87.3 seconds in retrieval, 1.7 seconds answering, and 0/0 citations. No answer was returned, so answer accuracy and citations could not be validated. | FAIL — P1 |
| Knowledge: document access/department isolation | Limit answers, search results and document content to the user's ACL/department. | Admin library displayed shared documents. There was no active Manager/Employee session for allowed-vs-denied checks. No ACL was changed. | BLOCKED — P1 coverage gap |
| Knowledge: audio question and database analysis | Ask by voice or upload an audio question; run bounded, read-only calculations on approved database tables. | Controls were present. No microphone permission, audio transmission, or business query was run. | PARTIAL |
| Knowledge: reviewed bookkeeping CSV export | Download a reviewed business-data CSV. | The action was visible under More library actions, but the data scope was not clearly isolated to synthetic-only rows. It was not clicked. | BLOCKED |
| My work | Personal assignments, workflow tasks, due dates, updates and calendar. Employee sees their own work; Manager sees assigned department work. | Opened the page; headings, task form and calendar navigation rendered. No task was created, assigned or completed. | PARTIAL |
| Meeting minutes | Turn a meeting source into a minutes draft for review. | Opened the page and confirmed the source selector and draft action. Existing recordings were not opened or reprocessed; no new synthetic recording was provided. | PARTIAL |
| Memory | Save personal preferences, terminology or profile context for future assistance. | Opened memory editor and saw the memory-type choices and Remember action. No memory was saved because that would persist personal account context. | PARTIAL |
| Company data research | Ask multi-step questions across internal reports, policies and tables, using source citations and access checks. | Opened the research form and specialist sections. No query was submitted; the common LLM provider has insufficient balance. | PARTIAL |
| Tender & RFP coverage | Map each tender requirement to accessible source evidence and gaps; reviewer makes the judgment. | Opened case form, department selector and requirement drafting/extraction controls. No RFP case was created. | PARTIAL |
| What-if forecasts | Deterministic scenario math from historical values cited in an exact source passage; not a statistical forecast. | Opened scenario form and confirmed source excerpt/value validation inputs. No scenario was saved. | PARTIAL |
| Market research | Public web research with citations using Gemini and Google Search. | Page reports Gemini is not connected; question and confirmation controls are disabled. No external request or provider charge was made. | BLOCKED — P2 |
| Org knowledge | Save a private statement draft or send department knowledge for review and a separate publisher. | Opened statement/category/visibility/source/exact-quote fields. No statement was saved or published. | PARTIAL |
| Knowledge interviews | Collect human-authored employee answers and format them into an SOP draft for separate review. | Opened page. No interview template is configured; Template selector and Start are disabled. | BLOCKED |
| Knowledge handoffs | Preserve procedures, decisions, dependencies and open work across role changes. Only the author, department managers and Admin can read. | Opened page. Admin has no local department membership; the form says a department assignment is needed. | BLOCKED |
| Expertise directory | Find department colleagues by skill tags they choose to share. | Opened profile/search. Skill editor is disabled because the Admin is not in a department. No skill was saved or searched. | BLOCKED |
| Employee training | Create source-backed lessons/quizzes; assign them to employees and track deadlines. | Opened page; zero plans, completions and assignments showed. No lesson was generated because generation needs the failing AI provider. | PARTIAL |
| Approvals | Review scoped knowledge/actions with separation of duties; preserve the decision history. | Opened Approvals. Both the review queue and policy impact panel changed to “Request failed (502).” No approval or denial could be performed. | FAIL — P1 |
| Claims | Submit a proposed organizational fact or correction that remains unverified until reviewed. | Submitted a clearly synthetic proposal. UI confirmed it is unverified and not searchable/used in answers. Navigated away and returned; the item remained. Audit log recorded claim.submitted. | PASS |
| Workflows & tasks | Department automation for internal tasks/messages with an approval gate and durable run history. Employees see their assignments; Managers manage their departments. | Opened page. It said Admin manages no department, no workflows are configured and no runs/tasks are available. No workflow can be safely run without a scoped department. | BLOCKED |
| Agent profiles | Department-scoped assistants that must inherit the caller's document access; view catalog and run history. | Opened page. It showed no agent in Admin's department scope, despite Home's global count of one active agent. No agent run was started while AI calls fail. | PARTIAL |
| Help & requests | Employee asks IT/HR/facilities for help, follows status and comments; HR requests are private to requester and Admin. | Opened form and request tabs. Counts settled after initial loading; no new request was submitted. Earlier audit history contains synthetic request events, but this pass did not verify the full lifecycle. | PARTIAL |
| Workflow/task/request notifications | In-app notices for approvals, assigned work and service request changes; training reminders are worker-backed. | Workflow page showed its Notifications panel and zero unread; no notice lifecycle was triggered in this pass. | PARTIAL |
| My departments | Manager's tools for their assigned departments only. | This is in Manager navigation in source code but is not available to the Admin session. No active Manager identity was available. | BLOCKED |
| People & access | Admin invites users, assigns roles/departments, manages groups and permissions. | Opened page and let it finish loading. It showed 3 active accounts, 10 departments, 7 pending invitations and 8 custom roles. The one-person invitation form defaults to Employee; links are not emailed. No users, departments or grants were changed. | PARTIAL |
| Partner access and partner portal | Admin defines isolated external partner workspaces and identities; partners use a separate cited assistant. | Admin page said there are no partner workspaces. No external identity or portal session exists to test. | BLOCKED |
| Client workspaces | Admin organizes clients/projects, owners, members and document access. | Page loaded with two archived synthetic QA workspaces and no active memberships. No workspace or membership was changed. | PARTIAL |
| Cloud connectors | Admin imports Drive/OneDrive/SharePoint documents and rechecks source permissions on sync. | Form showed provider and credential fields, no saved connectors and no department options. No credentials or external sync were added. | BLOCKED |
| AI providers | Admin selects and configures the model that powers answers/research. | Read provider state only: DeepSeek Flash active; a stored key is present but was not copied or tested. No configuration, activation, key or billing change was made. The chat request demonstrated current insufficient balance. | PARTIAL / FAIL for live AI service |
| Settings: local embeddings | Local model creates meaning vectors for document search. | Ran the local health action. Model transitioned from “Not loaded” to “Loaded in memory”; FAISS dimension is 384. | PASS |
| Settings: PDF parser | Read PDF text in isolated parser process. | Built-in parser health test passed: 1 page, 1 chunk, 3.735 seconds. | PASS |
| Settings: DOCX parser | Read Word files using Docling with built-in fallback available. | Docling fixture passed: 1 chunk, 35.680 seconds. This was a cold/slow first run. | PASS, slow cold start |
| Settings: XLSX parser | Read spreadsheet content with openpyxl. | Fixture passed: 1 chunk, 3.215 seconds. | PASS |
| Settings: PPTX parser | Read PowerPoint content with Docling and built-in fallback available. | Docling fixture passed: 1 chunk, 27.964 seconds. | PASS, slow cold start |
| Settings: TXT/CSV parsers | Read text and delimited data. | TXT fixture passed with 1 chunk in 2.880 seconds; CSV fixture passed with 1 chunk in 2.840 seconds. | PASS |
| Settings: OCR | Read scanned pages locally through RapidOCR/ONNX Runtime. | Local scanned-PDF test passed on 1 page and recognized 26 characters in 13.432 seconds. Configuration reports default recognition language “ch”; English/Urdu scan quality was not tested. | PASS for synthetic sample; language fit PARTIAL |
| Settings: audio/video | Local transcription and video-frame text extraction. | Runtime page reported ready, enabled, installed and local model available. No existing media was processed. | PARTIAL |
| Settings: search index | Confirm embeddings are stored and index is healthy. | Page showed FAISS healthy, 10 vectors in DB/index, 384 dimensions. | PASS |
| Settings: imports and sync | Track large folder imports and schedule read-only folder sync. | Page loaded; there are no scheduled sources. No path or sync schedule was added. | PARTIAL |
| Company setup | Company identity, starter kit and deployment license. | Page showed the synthetic company profile and selected consulting starter kit. Did not save or apply a template. | PARTIAL |
| Usage reports | Admin/scoped viewers inspect aggregate volume, reliability, evidence gaps, costs and latency; report says it excludes question/answer text. | Report loaded: 9 questions, 6 errors (66.7%), 2 evidence gaps, median 648 ms, p95 68.6 seconds, 6 LLMUnavailableError entries. Clicked CSV export, but no file appeared in the Windows Downloads folder and the browser connector exposed no download artifact. File content could not be checked. | PARTIAL; export unverified |
| Traces | Admin investigates aggregate request outcomes, stages and latency. | Page loaded and showed 9 questions, 6 errors, 2 refusals and six provider errors. Opened latest synthetic chat trace: request was recorded, with 0/0 citations and LLMUnavailableError. | PASS for trace visibility; diagnosis limited by provider error |
| Audit logs | Admin reviews immutable sign-in and configuration events. | Page loaded. It recorded the synthetic document upload and synthetic claim submission. Failed sign-in history is visible; no credentials were shown by the UI. | PASS for basic event visibility |
| Account security | Each user manages their MFA/authenticator and recovery settings. | Opened account security. MFA is not enrolled for this Admin; page asks for current password to begin setup. No security changes were made. | PARTIAL |
| Browser console | Developer diagnostic output for frontend exceptions. | The current CUA browser interface did not expose console messages in its supported snapshot API. No console-clean claim is made for this follow-up. | BLOCKED by tooling |

## Issues, reproduction and recommended fixes

### P1 — Core AI chat cannot return an answer; balance error and slow retrieval

- **Reproduction:** Sign in as Admin → Knowledge → upload the labelled synthetic text file and wait for Ready → create a new chat → ask for its synthetic record ID and code phrase with a citation.
- **Expected:** Fast, cited answer quoting the synthetic source.
- **Actual:** UI shows “Searching your documents,” then reports HTTP 402 insufficient API balance. Trace records 89.2 seconds total, 87.3 seconds retrieval, 1.7 seconds answer stage, and 0/0 citations. No answer was produced.
- **Severity:** P1 release blocker for Knowledge answers, training generation and AI-backed research/agents.
- **Recommended fix:** Have the provider owner restore valid billing/balance privately; do not paste or rotate keys in chat. Separately profile the 87-second retrieval stage and set a bounded user-visible timeout/retry path. On 402, fail quickly and preserve the question so Retry works only after the provider is restored. Re-run the exact synthetic answer/citation test and verify the citation opens the source.

### P1 — Approvals queue and policy-impact requests return 502

- **Reproduction:** Sign in as Admin → Approvals → wait for page loading to settle.
- **Expected:** Review queue, policy-impact list and decision history load, or a safe empty state.
- **Actual:** Both panels show “Request failed (502)”. No review/decision journey is available.
- **Severity:** P1 for governance/knowledge publication.
- **Recommended fix:** Trace the exact two failing API routes in the current API process, fix their server-side exception/status mapping, and show a request ID plus retry action instead of an unexplained 502. Verify Admin, scoped reviewer and Employee-denial paths against fresh synthetic records.

### P2 — Manager/Employee authorization and department-isolation coverage is incomplete

- **Evidence:** Only the Admin session was tested. A Manager invitation is pending. The Employee test account is listed active, but password was not read and prior audit history already contains repeated failed sign-ins and a lockout event.
- **Expected:** Fresh authorized Manager and Employee sessions with separate role navigation, accepted invitation state, allowed/denied document reads, scoped approvals and revocation checks.
- **Actual:** Neither role was signed in during this pass; there is no safe way to infer that a visible route or old test trace proves current access controls.
- **Recommended fix:** In the isolated QA fixture, accept/activate a Manager invitation and clear the Employee test lockout through the approved test-account process. Keep passwords only in the protected on-device fixture. Then open separate browser profiles and test positive and negative department/document access without printing credentials.

### P2 — Market research is unavailable

- **Reproduction:** Admin → Market research.
- **Expected:** Question form and explicit external-data/charge confirmation are available after Gemini setup.
- **Actual:** The page says Connect Google Gemini; form and confirmation are disabled. No external query was sent.
- **Recommended fix:** If public web research is in scope, an administrator should configure Gemini privately, then test with a non-sensitive synthetic topic and verify the citations. If not in scope for this deployment, label it as not configured instead of presenting it as an available research feature.

### P2 — Usage CSV download was not verifiable

- **Reproduction:** Admin → Usage reports → Export report as CSV.
- **Expected:** A CSV file downloads, can be opened, and contains only the selected aggregate report.
- **Actual:** The button click left the page unchanged; no file appeared in the desktop Downloads directory and the in-app browser returned no saved download artifact. I cannot claim the export works.
- **Recommended fix:** Verify a browser-native download event in a supported browser, add a success/error notice, and cover CSV content/headers and Excel/Word/PDF/PPTX exports with synthetic report data.

### P2 — OCR default language may not fit English/Urdu scans

- **Evidence:** The local OCR health test passed, but its configuration reports language “ch” (RapidOCR default). This is not proof of English/Urdu OCR quality.
- **Recommended fix:** Test representative synthetic English and Urdu scans and configure a suitable model/language strategy, or clearly disclose the recognition-language limitation.

### P3 — First-load counters can briefly show zero while data is still loading

- **Evidence:** People & access initially showed zero counts, then after about 1.4 seconds showed 3 active accounts, 10 departments and 7 invitations. The eventual counts matched Home.
- **Recommended fix:** Keep a skeleton/loading label until the API response arrives; reserve zero for a confirmed empty result so users do not mistake loading for missing accounts.

## Release-readiness decision

**NO-GO for release.** Basic application pages, the synthetic document ingestion path, parser/OCR fixtures, search-index health, claims submission, and administrative audit visibility work. Release is blocked by the HTTP 402 AI service failure, 502 approval requests, high error rate/latency in Usage reports, unverified export downloads, and missing current Employee/Manager access-control tests. Market research also requires a separate provider configuration.

No ngrok tunnel or remote URL was used; this audit stayed on loopback. No security setting, provider key, password, real document, or real user record was changed. Synthetic test file and claim were left clearly labelled in the QA dataset; the UI search filter was cleared.

## Screenshot and evidence retention

The browser automation returned accessibility snapshots and live page evidence, but did not expose a supported persistent screenshot file path in this session. No screenshot is claimed or fabricated. The principal repeatable evidence is the UI route and results listed above, the API readiness JSON, and the app's Usage/Traces/Audit pages.

## Roman Urdu summary

App khul rahi hai aur document upload, processing, search, PDF/Word/Excel/PowerPoint/TXT/CSV parsing, OCR, aur claim submit ka test pass hua. Lekin AI jawab dene se pehle balance ki wajah se 402 error aata hai, Approvals page 502 deta hai, aur Manager/Employee login abhi verify nahi hue. Admin, Manager aur Employee ke liye alag internal login page nahi—ek hi login hai; role ke mutabiq menu aur dashboard badalte hain. Release abhi tayyar nahi.

## Follow-up fixes (2026-10-30 evening)

Each item: reproduced, root cause found, fixed, retested. Full evidence is in completion-checkpoint.md.

| Audit item | Root cause | Fix | Retest |
|---|---|---|---|
| Approvals 502 | API process was down (Vite proxy ECONNREFUSED); routes were healthy | Review-stack supervisor restarts API/UI/worker; clear "server could not be reached" message | API killed twice, back in ~18–24 s; Approvals 7/7 calls 200 |
| 87 s retrieval | Embedding model loaded lazily on the first question (~27–44 s cold import) | Background model warm-up at API startup | Retrieval 336 ms and 301 ms on the next chats |
| Chat HTTP 402 | DeepSeek account balance | Local Ollama fallback (`LLM_FALLBACK_PROVIDER`) | Same synthetic question answered correctly with citation by Local Ollama after DeepSeek 402 |

Still open: DeepSeek balance (owner), Manager/Employee browser journeys, Usage CSV download verification, OCR language for English/Urdu, Gemini for Market research.
