# Live application audit — 2026-10-30

> Historical snapshot: this earlier pass saw the API down. The live services recovered afterward. Use the current [role and module audit](live-role-module-audit-2026-10-30.md) for the latest test results and release decision.

## Executive result

**Overall status: FAIL / release readiness NO-GO.** The React/Vite frontend loads at `http://127.0.0.1:5182/admin/`, but the API that it proxies to (`127.0.0.1:63011`) is not running. A sign-in request returned `Request failed (502)` and Vite logged `ECONNREFUSED 127.0.0.1:63011`. No authenticated role or in-app module could be reached in this audit.

The application worker is also absent. Its latest saved worker log contains `ModuleNotFoundError: faiss.swigfaiss_avx2`. A direct import check in the documented `.venv-verify-312` environment succeeds with `faiss-cpu 1.15.1`, so the logged worker failure points to an interpreter/package mismatch or an earlier environment state; it is not resolved by this audit.

The repository's protected synthetic review setup documents Administrator and Employee accounts, but no Manager account is present in the current review fixture. I did not read or print any password. I did not start services, run migrations, process the existing queue, change settings, upload files, or modify application records.

Independent frontend checks passed: `npm test` **8/8** and `npm run typecheck`. These checks do not establish that the app works end to end.

## Scope and evidence standard

- Target: `http://127.0.0.1:5182/` (served UI resolved to `/admin/`), local Windows laptop, 2026-10-30 Asia/Karachi.
- The UI was inspected with the CUA browser. Repository architecture and navigation were read from the current working tree and current docs. Role visibility below is **source-derived**, not verified in a signed-in session.
- A status of **BLOCKED** means the module could not be opened or its journey exercised because authentication depends on the unavailable API. It does not mean the module itself failed its functional assertions.
- A status of **FAIL** means a live service or request failed in this run. **PARTIAL** means only the visible/client-side portion was observed. No historical test result is promoted to a current PASS.
- The worktree already contained many unrelated modified files. This audit added this report only; it did not edit application source.

## Architecture and live services

| Layer | Purpose / dependencies | Observed state |
|---|---|---|
| Frontend | React 19, TypeScript, Vite 8; dev UI on port 5182. Vite proxies `/api` to the review API. | **PASS (shell only).** Node/Vite PID 35160 listened on `0.0.0.0:5182`; sign-in screen and styles rendered. |
| Backend API | FastAPI/Uvicorn, Python; expected loopback port 63011. | **FAIL.** No listener or Python API process. Direct `/health/ready` was refused; UI proxy health request returned 502. The saved stack manifest's API PID 31488 was not running. |
| Document worker | Separate `python -m app.worker`; parses, chunks and embeds files, then writes PostgreSQL for indexing. | **FAIL.** No worker process; saved stack manifest's worker PID 20620 was not running. Latest saved worker log reports missing `faiss.swigfaiss_avx2`. |
| Database | PostgreSQL 18. The review stack uses isolated loopback port 55929 and database `infraspine_livereview_20261029_test`; the separate local PostgreSQL listener on 5432 was observed but not queried or used. | **PARTIAL.** Port 55929 was live. A read-only `alembic_version` query on the named synthetic review DB returned `0070_invitation_custom_role`. No business/user rows were read and no database was changed. |
| Retrieval | PostgreSQL-backed document records/chunks plus local FAISS vector index; local MiniLM embedding model. | **BLOCKED end to end.** The documented Python 3.12 verification environment imports `faiss` successfully (`faiss-cpu 1.15.1`), but API and worker were offline. |
| Optional AI/media | Configured AI provider(s), local faster-whisper media support, optional Gemini/Google Search public research. | **NOT VERIFIED.** No AI request, media job, provider call, or external search was made. |
| Other listeners | PostgreSQL 5432 and Ollama 11434 were visible on the host. | Presence alone does not show that InfraSpine is connected to either service. |

The UI listener is bound to `0.0.0.0`, although this audit used loopback HTTP only. No remote URL or ngrok tunnel was used. The user-reported `ERR_NGROK_105` was outside the local test path.

## Accounts and roles

The current protected review fixture contains synthetic Administrator and Employee identities. It contains no Manager identity. The repository also has a credential-display helper for a different review fixture; that does not prove those accounts exist in this review database. I did not open either helper, expose credentials, or attempt sign-in with a test account because the login API was unavailable.

| Role | Test account setup | Result |
|---|---|---|
| Administrator | Documented in the current synthetic review setup. | **BLOCKED** before authentication: API proxy returns 502. |
| Manager / Management | No Manager identity in the current review fixture. | **BLOCKED**: account setup missing, and API is offline. |
| Employee | Documented in the current synthetic review setup. | **BLOCKED** before authentication: API proxy returns 502. |

## Browser checks completed

| Check | Expected | Actual | Status |
|---|---|---|---|
| Open local app | Sign-in page loads. | `/admin/` rendered the branded sign-in form. | **PASS** (frontend shell only) |
| Empty sign-in form | Required email/password fields prevent an empty login. | Both fields were empty and reported `required` / `valueMissing`; clicking Sign in left the form on screen. A prior inline 502 banner remained, so this did not prove the API response path. | **PARTIAL** |
| Malformed synthetic email | Invalid email is rejected as validation before authentication. | The browser reported `typeMismatch`, but the form produced `Request failed (502)`; the API target was refusing connections, so server-side validation could not be observed. Recheck in a normal browser once the API is healthy before attributing this to application code. | **PARTIAL** |
| Authenticated session | A valid authorized test user reaches the app. | No sign-in succeeded and no session was established. | **FAIL** |
| Browser console | No unexpected client errors. | CUA console returned no warning/error entries during the captured UI checks. The visible request error was an HTTP/proxy failure, not a console exception. | **PASS** for captured console only |
| API health through Vite | `/api/health/ready` returns ready JSON. | HTTP 502 from `http://127.0.0.1:5182/api/health/ready`. | **FAIL** |
| API direct health | `http://127.0.0.1:63011/health/ready` responds. | Connection refused; no API listener. | **FAIL** |

One masked password field was already non-empty in the browser context during the malformed-input check. Its value and source were not inspected. That one malformed-email request included the field in a request to the local Vite UI; the Vite proxy could not connect to the API at 63011. The field and email were cleared immediately afterward; no account was authenticated and no further login request was made with a populated field. The browser capture shows the empty form's placeholder, not a credential.

Vite's saved log records repeated `ECONNREFUSED` errors to `127.0.0.1:63011` around 1:12–1:15 PM local time. The API log had no activity after 12:47 PM; the worker log was last modified at 11:16 AM. The current Python 3.12 FAISS import check passed, so the worker's missing submodule remains an environment discrepancy to resolve.

## Feature and module inventory

The sidebar and page names below are read from `frontend/src/components/AppSidebar.tsx`. Purposes are summarized from the current UI components and README/API documentation. **Every authenticated journey below is BLOCKED in this live run**: expected result is that the listed role sees the module and can complete its documented flow; actual result is that the UI stops at sign-in because the API is unavailable. No module-level success or failure is claimed.

| Module | What it does | Intended role(s) | Status / observed result |
|---|---|---|---|
| Navigation and page search | Groups pages, collapses sections, and searches for pages/commands. | Signed-in roles | **BLOCKED** — sidebar is rendered only after authentication. |
| Home | Admin setup checklist and shortcuts into core configuration. | Administrator | **BLOCKED** — dashboard not opened. |
| Knowledge — documents | Uploads files, shows processing state/library, supports previews, replacement, retry/cancel and lifecycle actions. | Employee, Manager, Administrator (scope-dependent) | **BLOCKED** — no upload, worker completion, persistence-after-refresh, or download/preview test. |
| Knowledge — search and AI chat | Searches accessible documents and returns grounded answers with source/page citations and conversation context. | Signed-in roles | **BLOCKED** — no query, answer accuracy, citation, or follow-up tested. |
| Document access and department isolation | Applies private/shared/restricted access and checks user/group/department visibility. | Administrator; Manager within managed departments; readers | **BLOCKED** — allowed and denied reads/writes were not tested. |
| My work | Personal tasks, assignments, calendar/deadlines, reminders and read receipts. | Employee, Manager | **BLOCKED** — no task or notification journey tested. |
| Meeting minutes | Processes meeting audio/video into timestamped transcription/minutes and reviewable outputs. | Signed-in roles | **BLOCKED** — no upload, transcription, generation, or review tested. |
| Memory | Stores and edits the signed-in user's private assistant memories. | Signed-in roles | **BLOCKED** — no persistence/privacy test. |
| Company data research | Runs bounded, read-only analysis on an allow-listed business database with provenance. | Signed-in roles; Admin configures source | **BLOCKED** — no query, access-denial, provenance, or CSV test. |
| Tender & RFP coverage | Extracts requirements and compares them with approved company evidence. | Signed-in roles | **BLOCKED** — no source matching or report/export tested. |
| What-if forecasts | Compares scenario assumptions and produces source-backed forecasts. | Signed-in roles | **BLOCKED** — no forecast or saved-result check. |
| Market research | Optional public-web research with validated citations via Gemini/Google Search and explicit consent. | Signed-in roles, subject to deployment/licensing | **BLOCKED** — no consent, external request, result, or citation was attempted. |
| Org knowledge | Curates organization knowledge and evidence-backed material for governed use. | Signed-in roles; scoped reviewers/publishers | **BLOCKED** — no draft, review, publication or search visibility test. |
| Knowledge interviews | Captures expert interviews and turns answers into reviewable knowledge. | Signed-in roles | **BLOCKED** — no interview or review workflow tested. |
| Knowledge handoffs | Records process notes/open work for department continuity; managers can acknowledge. | Employee, Manager | **BLOCKED** — no create, acknowledgement or access-boundary test. |
| Expertise directory | Finds people by recorded expertise within the permitted organization scope. | Signed-in roles | **BLOCKED** — no search or department filtering tested. |
| Employee training | Builds source-backed learning plans/quizzes and manages assignments/reminders. | Employee, Manager, scoped training managers | **BLOCKED** — no plan, quiz, assignment or reminder tested. |
| Approvals | Reviews scoped claims, policy/action decisions and other approval queues with separation-of-duty rules. | Scoped reviewers/approvers; Admin oversight | **BLOCKED** — no allowed/denied or approval history journey. |
| Claims | Submits and tracks unverified employee claims without publishing them directly into RAG. | Signed-in roles | **BLOCKED** — no create/withdraw or publication-isolation test. |
| Workflows & tasks | Defines event-triggered automations, approval gates and internal tasks. | Signed-in roles; Admin/Manager scope varies | **BLOCKED** — no dry run, approval, trigger, execution, or retry test. |
| Agent profiles | Configures department assistants/tools, runs bounded jobs, reviews run history/catalog and retention controls. | Signed-in roles; Admin manages organization settings | **BLOCKED** — no create/run/review/retry test. |
| Help & requests | Creates service requests, comments, triage and status history; restricts HR details. | Employee, Manager, Administrator | **BLOCKED** — no privacy or request lifecycle test. |
| My departments | Manages membership only inside a Manager's assigned departments. | Manager | **BLOCKED** — Manager account also absent from fixture. |
| Invite employees | Invites accounts into permitted roles/departments and supports bulk CSV preview/import. | Administrator or scoped inviter | **BLOCKED** — no invite, CSV validation, acceptance or token handling tested. |
| People & access | Manages departments, accounts, onboarding, permissions/grants, role templates, and lifecycle. | Administrator | **BLOCKED** — no create/edit/deactivate, denied-scope or persistence test. |
| Partner access and portal | Manages external partner workspaces/identities and a separate scoped, cited portal. | Admin manages; Partner uses portal | **BLOCKED** — neither admin setup nor partner login was tested. |
| Client workspaces | Organizes client/project hierarchy, ownership, membership and summaries. | Administrator / scoped roles | **BLOCKED** — no hierarchy or isolation test. |
| Cloud connectors | Connects supported cloud drives, syncs sources and refreshes source ACLs. | Administrator | **BLOCKED** — no connector creation, sync, access denial or deletion tested. |
| AI providers | Configures provider/model endpoints, verifies and stages credentials, then activates a model. | Administrator | **BLOCKED** — no provider state read, key action or AI request attempted. |
| Settings | Manages parsers, OCR, embeddings, vector store, media processing, folder sync and database analysis configuration. | Administrator | **BLOCKED** — settings were not opened or changed. |
| Company setup | Sets brand/industry defaults and checks deployment/license features. | Administrator | **BLOCKED** — no configuration or license check. |
| Usage reports and exports | Shows aggregate activity/reliability/feedback/token/latency metrics and exports reports. | Administrator; permitted scoped report viewers | **BLOCKED** — no report, refresh persistence, download or file-content check. |
| Traces | Inspects request/processing traces for operational diagnosis. | Administrator | **BLOCKED** — no trace listing or filtering. |
| Audit logs | Shows administrative/security events without exposing credentials or document bodies. | Administrator | **BLOCKED** — no event or privacy check. |
| Account security | Manages the current user's password/MFA/recovery options. | Signed-in roles | **BLOCKED** — not opened; no security settings changed. |
| Invitation onboarding / SSO / SCIM | Accepts a valid invitation and supports configured identity lifecycle integrations. | Invitee; Administrator configures integrations | **BLOCKED** — no token, login, SSO, SCIM or directory request tested. |
| Notifications and delivery | Shows in-app workflow/training/task notices; optional reminder email delivery is worker-backed. | Employee, Manager, Administrator by scope | **BLOCKED** — no notification created/read or delivery tested. |

## Bugs, reproduction and recommended fixes

### B1 — API service unavailable; sign-in and all authenticated features fail

- **Severity:** P1 / release-blocking.
- **Evidence:** UI login request displays `Request failed (502)`; `/api/health/ready` through port 5182 returns 502; direct port 63011 refuses connections; Vite log records `ECONNREFUSED 127.0.0.1:63011`; saved manifest API PID 31488 is absent.
- **Reproduction:** Open `http://127.0.0.1:5182/admin/`; submit the sign-in form; observe the 502. Separately GET `http://127.0.0.1:5182/api/health/ready` and `http://127.0.0.1:63011/health/ready`.
- **Expected:** A healthy API responds with readiness JSON, and the login endpoint returns either an authenticated response or a safe auth rejection.
- **Actual:** The API connection is refused before any server-side auth result. No role can enter the application.
- **Recommended fix:** Restore the isolated review API on loopback port 63011, then confirm `/health/ready` is HTTP 200 and `/api/auth/me` returns the expected unauthenticated response before repeating sign-in. Keep the app database and other review stacks untouched.

### B2 — Worker is absent and its saved environment cannot import FAISS

- **Severity:** P1 for document ingestion; end-to-end document features remain blocked.
- **Evidence:** No worker process; saved worker log records missing module `faiss.swigfaiss_avx2`. In this audit `.venv-verify-312` reports `faiss-cpu 1.15.1` and imports `faiss` successfully.
- **Reproduction:** Read `.verification-live-review-20261029/logs/worker.log`; compare the worker launch interpreter with `.venv-verify-312/Scripts/python.exe`; run a guarded `python -m app.worker --check` only against the isolated review database after its API/queue state is confirmed.
- **Expected:** The worker starts with the same compatible Python/FAISS installation as the API and reports ready.
- **Actual:** The saved worker launch failed to import FAISS; no job can be relied upon to complete.
- **Recommended fix:** Pin and verify the same Python executable and `faiss-cpu` wheel for API and worker, then perform a one-document smoke test using a newly labelled synthetic file and an isolated queue. Do not start a continuous worker until existing queued items are confirmed safe to process.

### B3 — Manager role test account is missing from the current fixture

- **Severity:** P2 / acceptance coverage gap.
- **Evidence:** The current protected review fixture contains Administrator and Employee identities only; there is no Manager identity.
- **Reproduction:** Compare the role inventory in the current review fixture and the current review database setup documented in `docs/completion-checkpoint.md`.
- **Expected:** A documented Manager test identity in the same isolated review database.
- **Actual:** No Manager identity is documented in the current fixture. A separate older fixture is not proof of an account in this DB.
- **Recommended fix:** Add a Manager identity through the documented synthetic test-account setup on an isolated review database, preserving the password privately; then rerun Manager department and access-scope tests.

### B4 — Malformed sign-in validation needs a healthy-browser recheck

- **Severity:** P2 if reproducible in a normal browser; root cause not established.
- **Evidence:** A clearly synthetic malformed email had the browser `typeMismatch` flag set but produced a 502 request error. Because the API was unavailable and the test ran through the automation browser, this audit cannot determine whether normal end-user constraint validation or server validation is defective.
- **Reproduction:** With the API healthy, enter a malformed test email and no password in a normal browser and submit; confirm that the login request is not sent and that the user receives a validation message. Then test a well-formed unknown synthetic account and confirm a safe auth rejection without lockout of a real account.
- **Recommended fix:** Ensure client-side form validation prevents malformed email submissions and that server validation returns a safe 4xx response; avoid a generic proxy error as the user-facing result.

## Test coverage and release decision

**Completed:** local sign-in shell load; empty/malformed input inspection; failed API request observation; browser console check; process/port and safe log inspection; read-only isolated DB schema-version check; frontend unit tests (8/8); frontend TypeScript check; import-only FAISS check.

**Blocked:** all three role journeys; navigation after login; dashboard; document upload/processing/search/chat/citations; department permissions and denied actions; approvals; workflows; agents; reports/exports/downloads; notifications; account/security settings; integrations; persistence after refresh; application validation and backend error contracts.

**Not performed:** credential login attempts, uploads, exports, permission changes, service restarts, worker runs, application DB reads, migrations, credential changes, external provider calls, market-research calls, Docker/ngrok setup, or remote exposure. The documented launcher was not run because it applies migrations and starts a continuous worker against the existing review queue; the current UI process was preserved.

**Release readiness: NO-GO.** A frontend shell and passing frontend checks are insufficient while the API and worker are unavailable. The repository's current readiness docs also record open security, deployment/restore, live-provider and scale gates; those were not re-tested here.

## Screenshot evidence

CUA captured the sign-in/API failure screen and displayed it inline during this audit. The browser connector returned the image to the session but did not provide a persistent local export path, so no screenshot file is embedded in this Markdown report. The screen showed the generic 502 banner and no readable credential value.

## Roman Urdu summary

App ka frontend khul raha hai, lekin API band hai aur worker bhi nahi chal raha. Is liye login, documents, chat, permissions aur baqi modules test nahi ho sake. Frontend ke 8 tests aur typecheck pass huay. Release abhi ready nahi.
