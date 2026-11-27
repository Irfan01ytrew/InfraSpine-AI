# InfraSpine AI — Developer Handover (2026-10-31)

Backup branch: `backup/2026-10-31-handover` (from `feature/enterprise-platform-ui`). Detailed, dated evidence for every
feature is in `docs/completion-checkpoint.md`; audits in `docs/live-application-audit-2026-10-30.md` and
`docs/live-role-module-audit-2026-10-30.md`.

## 1. What it is
A self-hosted enterprise AI operating platform: private document knowledge base with cited RAG chat (English/Urdu,
OCR, audio/video transcripts), plus enterprise modules — people & access, positions/org chart, help & requests with
approval limits, multi-step/parallel approval chains, delegation, reminders/escalation, budgets, custom request
forms (file uploads, conditional fields, audience), webhooks, training, meetings, research, reports, automation.
Users: administrators, department managers, employees (custom roles supported), partner-portal users.
Deploy: on-premises Windows/Linux server or VM, Docker (compose files in repo), any host with PostgreSQL 16+.

## 2. Architecture & dependencies
- Backend: Python 3.12, FastAPI, async SQLAlchemy, PostgreSQL (asyncpg), Alembic migrations (`migrations/versions`, head `0102_finance_invoices`; check with `alembic heads`).
- Frontend: React 19 + Vite + TypeScript + Tailwind v4 + Radix (`frontend/`), light/dark themes.
- RAG: local embeddings, FAISS index, RapidOCR (incl. Urdu), local Whisper media, LLM providers (DeepSeek/Gemini/OpenAI-compatible) with local Ollama fallback (`app/services/llm_fallback.py`).
- Worker (`app/worker.py`): document ingestion, SLA escalation, approval reminders/escalation/email, attachment cleanup, webhook delivery, training/personal reminders, folder sync, automation.
- Secrets: AES-GCM encrypted at rest with a master key from env (`app/core/crypto.py`). `.env`, databases, uploads and `data/` are NOT in git.

## 3. Start (local review stack)
```
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
cd frontend && npm install
python scripts/start_live_review_stack.py      # PostgreSQL 55929, API 63011, UI 5182 (http://127.0.0.1:5182/admin/)
python scripts/supervise_live_review_stack.py  # keeps API/UI/worker/PostgreSQL running (re-reads settings on restart)
alembic upgrade head                            # with the database URL from start_live_review_stack.database_url()
```
Tests: `TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55929/<name>_test python -m pytest tests/...`;
frontend: `npx tsc --noEmit`, `npm test`, `npm run build`.
DATABASE_URL must also be set (conftest refuses to run without it, so tests can prove they never use the app database).
Full suite on a throwaway cluster: `python scripts/irfanix_postgres_tests.py --pg-bin <postgres>/bin -- tests -q -rfEs`.
Install requirements-dev, -media, -reporting, -document-tables and -browser first. Some tests need public model files
on disk: the pinned embedding model in `data/models` (sentence-transformers/all-MiniLM-L6-v2 @ 1110a243…) and
Docling's layout model in the Hugging Face cache; without them those tests fail or skip.

## 4. Modules (app/services unless noted)
- auth, users, access_control, role_templates/role_library, sso, scim — sign-in, MFA, invitations (with position/manager, CSV), custom roles, permissions.
- org_structure, position_library — positions (grant no access), reporting lines, org chart, history.
- departments, collaboration, workspaces/partner_* — departments, client/project workspaces, partner portal.
- rag/* , knowledge, document_*, folder_sync, cloud_connectors — documents, ingestion, retrieval, citations, sync.
- conversations, memory, claims, knowledge_interviews, expertise, handoffs, meetings, forecasts, research — knowledge features.
- service_requests, service_request_worker — Help & requests, SLA escalation.
- approval_authority (limits), approval_chains (sequential + parallel all/any stages), approval_delegation, approval_reminders (reminders, escalation, email outbox), budgets.
- request_forms, request_attachments — custom forms, typed/conditional fields, audience, private file attachments.
- webhooks — signed HMAC deliveries, SSRF guard (`app/providers/endpoint_security.py`), retries, pause.
- automation, agents/agent_*, training_reminders, personal_task_*, report_export, audit — workflows, agents, reminders, exports, audit log.
- Frontend pages in `frontend/src/components/*Page.tsx`; shared live refresh hook `frontend/src/lib/useLiveRefresh.ts`.

## 5. Status (evidence: docs/completion-checkpoint.md)
PASS (tests + live on 5182): approval limits, multi-step and parallel chains, delegation, reminders, escalation,
budgets (warn/block), request forms, attachments, conditional fields, form audience, webhooks (signed, retry, HR excluded),
auto-refresh, positions/hierarchy, invites/CSV with positions, role isolation (manager/employee), AI 402 fallback to Ollama,
retrieval latency fix, CSV export, API supervision. Last focused run: 40 backend tests passed; frontend 8/8, build OK.
PARTIAL: Urdu OCR (~88% char accuracy).
Step A (2026-11-25, Linux box): full backend suite rerun end-to-end on PostgreSQL 17; bug fixes, the 0096 connector
migration, pause enforcement and test updates are listed in CHANGES-step-A.md (shipped beside the source zip).
BLOCKED: approval email delivery live (no SMTP configured); attachment virus scan (no AV engine); live AI answers for the
video question (needs a paid DeepSeek/Gemini key); Windows scheduled task for auto-start (user must run it).
Step C (2026-11-26): shared platform core `app/platform/` (scopes, memberships, channel identities, approvals,
execute_once, outbox, org time zone) — see docs/platform-core.md and CHANGES-step-C.md. Modules move onto it in step D.
NOT YET: "N of M" approvals, fiscal-year budget settings and alerts, delegation for single-step approvals,
per-form conditional on non-choice fields, real third-party webhook receiver test, exports in org time zone, final user docs.

## 6. Important paths & backups
- Repo: this directory; remote `origin` = github.com/abdul78600/infaspine-AI-RAG.
- Review stack state/logs/fixtures (NOT in git): `.verification-live-review-20261029/`, PostgreSQL cluster `.verification-local-review-20261029-c16-pg-isolated/cluster`.
- QA scripts: `scripts/*_qa.py`, receiver `scripts/webhook_receiver.py` (loopback test only).
- Backups: GitHub branch `backup/2026-10-31-handover`; earlier work on `feature/enterprise-platform-ui`.

## 7. Next steps
1. Run the full backend test suite on a fresh `_test` database and fix any regressions.
2. Configure SMTP (APPROVAL_EMAIL_ENABLED) and an AV scanner, then verify live.
3. Add a provider key (DeepSeek/Gemini) to finish live AI/video acceptance.
4. Build the "Not yet" items above, then final user/admin documentation and deployment guide.
5. Merge `feature/enterprise-platform-ui` into `main` via PR once reviewed.
