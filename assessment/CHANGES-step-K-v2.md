# Step K v2 — Digital Twin v2: what changed

Plain-language summary of the whole step (slices 1–8) for the
owner. The twin is a read-only mirror of the company: it compares
snapshots of the company graph against finance and other sources,
scores how healthy and trustworthy that mirror is, and proposes
fixes — it never changes the company itself. Every change below
goes through a human approval first.

## What the twin does now

- **Drift detection (slice 1).** Compares two snapshots of the
  company and reports what changed: entities and relationships
  added, removed or changed, money moved per currency, and a drift
  score 0–100 with a trust verdict (trustworthy / review / stale).
  Snapshots are fingerprinted (SHA-256), so the same snapshot is
  never stored twice.
- **Snapshot versions (slice 2).** Every recorded snapshot is kept
  as a version (30 versions / 90 days), with a drift report against
  the previous version and a read-only API. The list never returns
  a full snapshot or money amounts; money lives only in the drift
  report, owner-only.
- **Health score + watch (slice 3).** One 0–100 health score per
  version (100 minus drift minus a freshness penalty of 10 points
  per stale source, capped at 30) with bands healthy ≥75 / degraded
  ≥40 / critical <40. A watch evaluates rules over the latest
  health, dedupes alerts by fingerprint, respects quiet hours
  22:00–07:00 PKT, and lets critical alerts break through.
- **Engine unification (slice 4).** The twin's what-if engine now
  delegates failure scenarios to Step AL's simulator and absence
  scenarios to Step S's engine, instead of keeping a third copy.
  The twin-native engine is NOT retired yet — that happens at
  merge, gated on the differential test.
- **Drill reconciliation (slice 5).** Compares twin simulations
  against real Step G drills and files every disagreement as one
  review-queue finding (critical / warning / info), deduped by
  fingerprint, capped at 500. Findings never rewrite a drill or
  twin record; acknowledging one only marks the queue row as seen.
- **Twin-driven proposals (slice 6).** A finding can become a
  governance proposal — backup, drill review, supplier review,
  permission review, money review or data review — with the
  approver class fixed by the finding (money / privilege /
  data-restore needs the owner; backup / drill review needs a
  graph approver). Approvals are bound to the exact action,
  single-use, with separation of duties; dispatch is idempotent
  via `execute_once`. Nothing is ever auto-applied. A scheduled
  reconciliation cycle (`scripts/twin_reconcile.py`) runs the
  loop on demand.
- **Multi-source trust + conflicts + feeds (slice 7).** Trust is
  scored per source (company graph 30, finance 20, continuity 15,
  membership 10, workflow 10, Agent Studio 10, connectors 5):
  fresh sources score 100, stale sources 0, unknown sources drop
  coverage instead of counting as anything. The band is the worse
  of the freshness band and the drift verdict. Conflicts list both
  sides of every disagreement about the same entity field; money
  amounts are compared by value and redacted for non-owners.
  Live-sync feeds turn platform events into graph proposals
  through the governed-change path: a revoked user's membership is
  proposed for deactivation (live today); workflow, Agent Studio
  and connector events are translated and handled the same way,
  ready for the day publishers emit them.
- **Twin UI v2 (slice 8).** A Digital Twin workspace in the app:
  health gauge, multi-source trust card, drift timeline, version
  history, proposal inbox (read-only until approval — Approve /
  Reject act on the exact action shown, Execute only after
  approval), review queue with Acknowledge, and health alerts.

## Safety rules that hold for the whole step

- The twin NEVER writes the graph, grants access, restores data,
  switches a supplier, or pays anything — it only proposes, and a
  human approves.
- Every lookup goes through the platform scope check: unknown
  tenant or non-member → 404; unauthenticated → 401; member
  without the permission → 403.
- Finance amounts are owner-only everywhere (drift reports,
  proposals, conflicts, findings). Everyone else sees the scores,
  counts and the fact that a disagreement exists — never the amount.
- Approvals are bound to the exact action hash, expire, and are
  single-use. Separation of duties is enforced: proposer,
  approver and executor are three different people.
- Side effects go through `execute_once` with read-back
  verification, so a retry never dispatches twice.

## Tests and gates

- Backend pure tests (slices 1–7): drift 38, health 23, versions
  8, what-if 21 + differential 18, reconciliation 21, proposals
  (slice 6: 61 new), trust 10, conflicts 12, feeds 11 — plus
  slice 1–6 pure regressions 167 passed in one run.
- Backend DB/API tests: versions 23, health 19, reconciliation
  12, proposals 57 (slice-6 DB regressions), feeds 18 passed in
  one process (274.49s).
- Frontend: 30 tests passed (22 prior + 8 new twin dashboard),
  `tsc --noEmit` clean, `vite build` clean, twin strings verified
  in the built ModuleWorkspaces chunk.
- Alembic: single head 0110 on this branch; upgrade →
  downgrade → upgrade roundtrip green on a fresh throwaway
  database (114 migrations); `alembic check` clean. No migration
  in slices 7–8. Note: on a shared test database the downgrade is
  blocked by the pre-existing guard in migration 0089 ("Company
  graph contains data") — unrelated to this step; the fresh-DB
  roundtrip is the valid proof. The persistent twin test database
  was upgraded 0106 → 0110 during this slice's gates and now
  reports head 0110 with `alembic check` clean.
- Full backend suite: NOT run — owed on a quiet VM (load control
  while the Step E verification pass runs).

## Bugs found and fixed by this step's tests

- Slice 7: `apply_feed` built the graph change without a tenant
  id (every insert failed); the conflict money comparison never
  ran (wrong field name passed); equal money amounts looked
  different (`"100.00"` vs `"100"` — now canonicalised to 2
  decimals); dedupe compared full payloads but every proposal
  stamps a fresh timestamp, so it could never fire (timestamps
  now excluded from the dedupe identity).
- Slice 6: `proposal_summary` omitted `approver_class`.
- Slice 5: the dedupe fingerprint omitted `money`, merging two
  different money-mismatch findings into one row; the acknowledge
  route returned 200 without persisting.
- Slice 4/5/3 items: see the plan doc.

## What is still owed

- Full backend suite on a quiet VM before merge (load control).
- Per-source drift attribution: the Step S snapshot builder must
  stamp a `source` on each entity before the twin can say which
  source a drifted entity came from.
- Feed publishers for the ready-tier kinds (workflow, Agent
  Studio, connector events): the twin's consumption side is built
  and tested; the publishers live on the Step S branch / future
  work.
- Conflicts-detect UI: the API exists; the workspace has no
  per-source entity views to feed it yet.
- Twin-native engine retirement at merge (gated on the
  differential test).
- Scheduler/CLI entry point for the reconciliation cycle exists
  (`scripts/twin_reconcile.py`); wiring it into the worker
  schedule is a merge-time step.

## Merge notes

- Step S (`agent/digital-twin`) MUST merge before K v2. At merge:
  KEEP the S `__init__.py` and ADD the K names drift + health +
  whatif + reconciliation + proposals + trust + conflicts + feeds.
- Renumber migrations at merge: 0106 (versions), 0107 (health
  alerts), 0108, 0109 (reconciliation findings), 0110 (proposals).
- This branch's `whatif.py` REPLACES the Step S file at rebase;
  retire `simulate_failure` + helpers then, gated on the
  differential test.
- No migration in slices 7–8.

## Owner decisions still open

- Drift trust bands (≤25 / ≤60 / >60 implemented as the default).
- Which twin findings may become proposals, and who approves them
  (safe default implemented: owner approves money / privilege /
  data-restore; graph approver approves coverage / backup).
- Finance amounts owner-only (implemented as the default).
- Authoritative HR / membership source.
- Retention: versions 30 / 90 days; findings 100 / 90 days.
- Where the twin sits in the 12-module sidebar numbering (the
  workspace is reached from Frontend & UX for now; the sidebar was
  not renumbered).
- Dashboard refresh interval for the twin workspace (30 s used,
  matching the Step AJ dashboard).
