# Step U — Mobile app + offline mode: plan

Owner goal: use InfraSpine AI fully from a phone, and keep working when the
internet drops; everything syncs back on reconnect. PWA first (installable,
no app-store delay), native wrapper only later if the PWA proves insufficient.

## What exists (base `step-e-gaps-wip` `ca956c2`, checked 2026-11-28)
- Frontend: React 19 + Vite 8 + Tailwind 4, `base: "/admin/"`, built to
  `frontend/dist`, served same-origin by FastAPI `StaticFiles` at `/admin`
  (`app/main.py`, `ADMIN_UI_DIR`). `index.html` already has the viewport meta.
- Auth: HttpOnly session cookie, `credentials: "same-origin"` everywhere in
  `frontend/src/api.ts`. The JS never sees, stores or persists any token.
- Server already accepts an `Idempotency-Key` header (e.g. `runAgentStudio`,
  `api.ts:856`) — the offline queue reuses that pattern.
- **Missing entirely:** no `frontend/public/` directory, no web manifest, no
  service worker, no Workbox, no `navigator.onLine` handling anywhere in the
  UI (the only "offline" string is a "Document worker is offline" alert).
- Responsive gaps are owned by Agent 18 (frontend redesign); Step U does not
  restyle pages, it only adds installability + offline behaviour.

## Approach — PWA first
1. **Installable:** `frontend/public/manifest.webmanifest` (name, short name,
   display `standalone`, theme/background colour, 192 + 512 icons) + manifest
   link, `theme-color`, `apple-touch-icon` in `index.html`. Registration in
   `main.tsx`, production only, scope `/admin/`, guarded so dev (`vite dev`,
   port 5173) never registers a worker.
2. **App-shell caching (service worker `frontend/public/sw.js`):** versioned
   cache; cache-first for the shell (`/admin/`, JS/CSS, manifest, icons);
   network-first for everything under `/api/` — API responses are **never**
   cached, and auth/session endpoints are never intercepted for replay.
3. **Offline action queue (`frontend/src/lib/offlineQueue.mjs`):** actions the
   user takes while offline are stored in `localStorage`, replayed in order
   when `online` fires, each with a unique `Idempotency-Key` so a replay can
   never execute twice server-side.
4. **Conflict rules (non-negotiable):**
   - Server wins for approvals and payments. A queued action whose approval
     or subject changed on the server while offline is **dropped and shown
     to the user**, never auto-applied.
   - **Never queued, ever:** approvals, decisions, payments, transfers,
     admin changes, auth, workflow runs/cancels, deletes. Enforced by an
     allowlist of kinds + a denylist of URL patterns in the queue module
     (defence in depth — both must pass).
   - Queued entries expire after 24 h; entries older than that are dropped
     with a visible notice, never replayed stale.
   - The queue holds no credentials and no tokens — replay uses the browser's
     same-origin session cookie, exactly like a normal request.
5. **Offline UX (slice 2):** an offline banner + a "queued (n)" indicator
   with per-item status (queued / syncing / synced / dropped + reason).
   Slice 1 ships the transport + policy only; the banner is a separate commit.

## Slice 1 — done in this branch (Agent 28)
- `frontend/public/manifest.webmanifest` + 192/512 icons.
- `frontend/public/sw.js` (shell cache, network-first `/api/`, versioned cache).
- `frontend/src/lib/offlineQueue.mjs` (+ `.d.mts`) — enqueue/replay/persist
  with allowlist + denylist, 24 h expiry, idempotency keys, max 3 attempts.
- `frontend/test/offlineQueue.test.mjs` — policy + persistence + replay tests.
- `index.html` manifest/theme links; `main.tsx` guarded SW registration.
- Verified: `npm test`, `npm run typecheck`, `npm run build` clean; the built
  `dist/` contains manifest + `sw.js` + icons.

## Slice 2 — done in this branch (Agent 141)
- `frontend/src/lib/offlineStatus.mjs` (+ `.d.mts`) — status records (synced/dropped
  only, max 20, pruned after 7 days), `deriveItems`/`countByStatus`/labels;
  `frontend/test/offlineStatus.test.mjs` — 9 tests. A live queue entry is never
  shown as synced/dropped; drop reasons are always shown (expired / conflict /
  rejected / max-attempts).
- `frontend/src/lib/offlineSync.ts` — the replay wiring slice 1 never connected:
  drop-expired on start, replay on `online` (and on start when already online),
  syncing tracking while a replay runs, dismiss helpers, and the
  `infraspine:offline-queue-changed` event the banner listens to. Replay uses
  the same-origin session cookie; nothing here stores credentials.
- `frontend/src/components/OfflineBanner.tsx` — offline banner, back-online
  syncing banner, "Queued (n)" toggle with per-item status (queued / syncing /
  synced / dropped + reason), dismiss per item + dismiss-all, "Retry now" when
  online with queued items; mounted in `AdminConsole` above `<main>`; renders
  nothing when online with nothing to report. Two test-expectation bugs fixed
  in the tests (sort order), product code unchanged.
- `frontend/src/styles.css` — `.banner-info` + `.offline-banner` /
  `.offline-queue-*` styles (CRLF preserved).
- Verified: `npm test` 40 passed (31 pre-existing + 9 new), `npm run typecheck`
  clean, vite build clean (10.13s; two earlier attempts OOM-killed RC=137 under
  load 26–42, third attempt in the post-restart quiet window passed; built
  `dist/` contains the banner code). No backend change, no migration.

## Slice 3 — done in this branch (Agent 146)
- **Audit (queueable kinds from `offlineQueue.mjs` → endpoints they would hit):**
  - `knowledge-question` → `POST /api/rag/chat` and `POST
    /api/conversations/{id}/messages` (+ `/messages/stream`): did NOT honour
    the key — every replay re-ran the LLM and (conversations) appended
    duplicate messages. Now wired.
  - `draft-save` → `POST /api/workflow-engine/scopes/{scope_id}/workflows`
    and `.../workflows/{workflow_id}/versions`, plus `POST
    /api/workflow-engine/scopes/{scope_id}/draft-from-text`: did NOT honour
    the key. Now wired. `POST /api/rag/documents/{id}/financial-extraction`
    already idempotent by record identity (document version + sha256 +
    profile + date_order) — no change.
  - `note-create`: NO endpoint exists in the codebase — nothing to wire;
    recorded as a gap, not built.
  - Already honouring, unchanged: Agent Studio runs (SHA-256 digest, 409 on
    a different task), workflow TriggerRun/Signal (body `idempotency_key`),
    automation runs (unique `idempotency_key`).
- **Mechanism — new, deliberately separate from `execute_once`:**
  `app/platform/idempotency.py` + `IdempotencyRecord`
  (`app/platform/models.py`, table `idempotency_records`, migration
  `0108_offline_idempotency_records`, down_revision `0103`). Only the SHA-256
  of the key is stored, never the raw key; scope is (user, key). `execute_once`
  is for privileged approval-gated dispatches with read-back verification;
  these creates are synchronous, user-scoped, need no duty/approval — a
  second ledger would be the wrong reuse. Claim after authorization, before
  side effects; `fail_request` on exception; `complete_request` after the
  route's own commit. Same key + different request → 409; fresh pending →
  429 (retryable for the queue; 409 is not); stale pending (>10 min) or
  failed → reclaimed; records pruned after 90 days (never pending).
- **Replay endpoint:** `GET /api/offline/replay/{key}` (`app/api/routes/offline.py`,
  registered in `app/main.py`) returns the stored result for the caller's own
  key; 404 for unknown keys AND other users' keys (no leak); 401 unauthenticated.
- **Deviations:** `draft-from-text` wired although it persists nothing (replay
  only re-runs the LLM otherwise); replay endpoint is GET-only for now, client
  adoption is a later slice (the queue already sends the header on replay, so
  server-side dedupe covers the lost-response case). No frontend change.
- **Verified:** pure tests `tests/test_offline_idempotency_pure.py` 6 passed.
  Alembic downgrade 0108→0103 → upgrade → `alembic check` clean (single head
  0108; `alembic check` "No new upgrade operations detected"). `py_compile`
  clean on all touched files.
- **DB tests — CLOSED by Agent 166 (2026-11-28):** `tests/test_offline_idempotency.py`
  **14 passed, 0 failed** (252 s; plus 6 pure tests). The re-run found and fixed
  FOUR real bugs in slice 3's wiring (all in the shared ledger / call sites,
  all proven by the failing tests before the fix, green after):
  1. `claim_request` read the stored result AFTER `rollback()` — the completed
     row was expired, so every replay of a completed request 500'd with
     `MissingGreenlet` (rag/chat, conversations, workflow creates/versions,
     draft-from-text). Fix: read before the rollback; no rollback on replay.
  2. Same function rolled back the whole session on a key conflict, expiring
     the caller's loaded objects (the authenticated user) — a stale-pending
     reclaim then 500'd reading `user.id`. Fix: the claim insert runs in a
     SAVEPOINT (`begin_nested`), so a conflict rolls back only the savepoint.
  3. `conversations.py` passed `idempotency_key` positionally to
     `_answer_message`, whose parameter is keyword-only — every non-stream
     `POST /messages` 500'd with `TypeError` (stream path was already correct).
  4. `complete_request` stored row dicts with raw UUIDs/datetimes — the JSONB
     column refused them (`Object of type UUID is not JSON serializable`) and
     a plain `str()` fallback would have stored datetimes space-separated,
     so replays differed from the original ISO-8601 response. Fix: normalize
     with FastAPI's `jsonable_encoder` (identical to what the client received).
  Regressions after the fixes: `test_workflow_engine.py` 8 passed,
  `test_conversations.py` 10 passed, `test_rag.py` 31 passed (split 25 + 6
  after an OOM kill mid-file at test 25 — 0 failures in either run).

## Slice 4 — done in this branch (Agent 166)
- **Background Sync (sync event):** the page asks the browser for a
  one-off sync (`registration.sync.register("infraspine-offline-queue")`)
  whenever entries are still queued — at start, after a replay that leaves
  entries behind, and on going offline (`offlineSync.ts`). When connectivity
  returns the browser fires `sync` in `sw.js`, which nudges an open admin
  page to replay (`postMessage({ type: "infraspine:replay-queue" })`, no data
  in the message); the page runs the normal policy-checked replay. This
  covers `online` events a backgrounded tab missed.
- **Two hard limits, honoured not worked around:**
  - Background Sync is Chromium-only — where `registration.sync` is missing
    (Safari, Firefox) registration is a silent no-op and the `online`-event
    path from slice 2 stays the only trigger.
  - The queue lives in localStorage, which a service worker cannot read, so
    the worker never replays anything itself. When no admin page is open (or
    the nudge fails) the worker re-registers the sync for the next
    connectivity change — never silently consumed; the page also replays on
    start when online (slice 2) as the backstop.
- **New files:** `frontend/src/lib/offlineBackgroundSync.mjs` (+ `.d.mts`)
  — `registerBackgroundSync()` (never throws; probes for `sync` at runtime
  because the DOM lib has no such property) + `isReplayQueueMessage()`.
- **Policy unchanged:** the replay this triggers is the same replay as any
  other — allowlist/denylist, 24 h expiry, one Idempotency-Key per entry,
  server-side dedupe. The sync tag and the message carry no entry contents,
  keys, or credentials. No backend change, no migration.
- **Verified:** `frontend/test/offlineBackgroundSync.test.mjs` — 11 tests
  (helper guards + the real `sw.js` driven in `node:vm` with a mocked
  `self`: unknown tag ignored, no page → re-register, visible page nudged,
  hidden page used as fallback, failed nudge/lookup → re-register);
  full frontend `npm test` 51 passed, `npm run typecheck` clean.
  Slice 3's DB tests re-run green by Agent 166 (14 passed — see slice 3).
  Vite build exit 0 (built in 7.62 s, NODE_OPTIONS heap 2048 MB).

## Slice 5 — done in this branch (Agent 177)
- **Read-model cache:** last-known Knowledge answers + dashboard snapshot,
  served read-only while offline, clearly marked stale, never used for
  decisions.
- **New pure module** `frontend/src/lib/offlineReadCache.mjs` (+ `.d.mts`) —
  `recordKnowledgeAnswer` / `loadKnowledgeAnswers` (newest first, same
  question replaces the older copy, max 20, pruned after 7 days, answers
  truncated at 20,000 chars, citations keep filename + page only, no
  snippets) + `recordDashboardSnapshot` / `loadDashboardSnapshot` (single
  latest snapshot, same 7-day expiry) + `isSnapshotWorthKeeping` (an
  all-empty load — every source failed — is never recorded and never
  overwrites a good cache) + `staleLabel` (same thresholds as
  `format.timeAgo`) + `clearReadCache`. Same per-device limitation as the
  queue (slices 1–4): localStorage is per origin, not per user. The service
  worker is untouched — API responses are still never cached there; this
  cache lives in the page.
- **Recording:** `KnowledgeChat` saves every answered question (streamed chat
  via `streamMessage` and voice via `askSpokenQuestion`) as it arrives;
  `HomePage.refresh()` saves the dashboard snapshot after a successful load
  while online. All recording is best-effort try/catch — a cache failure
  never breaks the live page.
- **Serving while offline:** `OfflineReadCachePanel` on the Knowledge page
  (saved Q&A list, each labelled "Saved … ago", read-only, with a "Clear
  saved answers" action and the note that saved answers must never be used
  for decisions); the Home page shows the cached dashboard behind a warning
  banner ("Offline — showing a saved dashboard", saved time, read-only, do
  not use for decisions) and refreshes live data automatically when back
  online. Nothing in the cache feeds any action: no approval, payment,
  workflow or admin path reads it.
- **Carried gaps — NOT closed by slice 5:** `note-create` still has NO
  endpoint (nothing to cache for it); `draft-from-text` still persists
  nothing (unchanged); the replay endpoint is still GET-only (unchanged).
- **Verified:** `frontend/test/offlineReadCache.test.mjs` — 11 tests
  (round-trip, dedupe/replace, cap 20, 7-day prune, truncation, corrupt
  storage, stale labels, snapshot worth-keeping rule, overwrite/never-
  overwrite-empty, clear); full frontend `npm test` 62 passed (51 prior +
  11 new), `npm run typecheck` clean, vite build exit 0 (8.67 s; built
  `dist/` contains the panel + stale-banner strings). No backend change,
  no migration.
- **Fixes found by the gates (all in the new wiring, none in prior slices):**
  (1) the saved-answers panel was first inserted inside the chat
  `{loading ? (` ternary and broke the render — moved above it; (2) the
  cache state was first added to `KnowledgePage` but the chat logic lives in
  the separate `KnowledgeChat` component — moved there; (3) a leftover
  `${state.pendingInvites}` in the Home page steps detail read the live
  (empty offline) value instead of the cached one — now `displayState`.

## Remaining slices (Step U, in order)
6. Native wrapper evaluation (Capacitor) only if PWA limits block a real need
   (push notifications, background upload); decision needs owner input.
7. `CHANGES-step-U.md` + full suite / Alembic / frontend gates, then merge.

## Owner inputs needed later
- Push notifications: yes/no (needs a push service + keys).
- Which actions (beyond knowledge questions + drafts) may be queued offline.
- Native app (store) vs PWA-only: PWA is recommended first.
