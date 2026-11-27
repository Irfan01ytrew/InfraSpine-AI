# Module 04 — Adaptive Process Intelligence

Checkpoint: 2026-11-25. Module 04 source was reconciled into the canonical shared checkout alongside active Module 05–12 drafts. Alembic merge head `0090_merge_module_heads` includes Module 04. No production database or live API was changed.

## Architecture and acceptance

| Area | Existing foundation | Module 04 implementation |
| --- | --- | --- |
| Authentication | HttpOnly session, CSRF, active-user and MFA checks | Reuses signed-in session and audit context for all API operations |
| Tenant isolation | No shared tenant identity for this module | Explicit organizations and membership roles; user and tenant scoped lookups |
| Source permissions | Live Knowledge document ACL and quarantine | Source grants intersect membership and current document authorization |
| Governance | Reviewed Knowledge policies and process records | Independent steward review for comparison baselines; discovery cannot execute workflows |
| Event mining | No persisted event-log miner | Immutable events, namespaced cases, variants, transitions, evidence links |
| Analysis | No event based process analytics | Cycle and waiting intervals, throughput, rework, handoffs, missing ownership, conformance, SLA, anomaly and drift signals |
| Adaptation | No versioned process models | Per-case recalculation, immutable scoped snapshots, revision hash, queued fenced analysis |
| UI | Shared React/TypeScript shell | Responsive map, variants, timelines, evidence, filters, comparisons, recommendations, what-if, source controls, CSV/JSON export |
| Integrations | Knowledge, Workflow and Company Graph are separate modules | Read-only document extraction and versioned module API; owning-module adapters remain open |
| Security | Existing app audit and source ACL | 4 MiB request bound, 2,000-event batch limit, explicit user/tenant scope, live permission recheck |

### Verified on 2026-11-25

- [x] Synthetic purchase approvals: 27 events, 6 cases, 4 departments, 3 variants.
- [x] Persisted-event reconstruction and evidence endpoint verified against PostgreSQL.
- [x] Separate steward approved the baseline; its creator's self-review was denied.
- [x] Labeled results: 2/2 SLA breaches; 3/6 cases conformant; 25h manager-to-director interval identified.
- [x] Hypothetical manager absence reported affected cases, added case-hours, threshold crossings and assumptions.
- [x] Late event batches update affected case traces; drift appears in a new snapshot while reviewed SOP stays intact.
- [x] PostgreSQL API suite: 13 tests passed, including tenant isolation, source revocation, duplicate/conflicting imports, event ordering, immutable events/models, stale worker claims, CSRF and body bounds.
- [x] React/TypeScript production build passed after shared-shell integration.
- [x] `git diff --check`; Python compile check; `alembic heads` reports shared `0090_merge_module_heads`.
- [ ] Authentication, document-ACL and governance regression suite currently running; record final result when completed.
- [ ] Authenticated browser walkthrough of maps, filters, variants, timelines, review, simulation and exports.
- [ ] Larger benchmark with processing latency and labeled precision/recall across several event-log sizes.

### Open acceptance gates

- [ ] Authorized production-like ERP/CRM/HRMS/accounting/email/ticket/API connectors and incremental synchronization.
- [ ] Source permission creation and revocation browser acceptance with real authorized users and documents.
- [ ] AI extraction provider acceptance, evidence precision and prompt injection test corpus.
- [ ] Integration with Module 02 Agents, Module 03 Company Graph, Module 05 Workflow telemetry, Module 06 Governance, Module 07 Exceptions and Module 08 Continuity through their owned contracts.
- [ ] Bounded memory/partitioning and measured scale tests above 100,000 events.
- [ ] Full keyboard/screen-reader and responsive authenticated browser review.
- [ ] Independent production architecture/security review, retention policy approval and migration/recovery drill.

All metrics above are from synthetic labeled test records. They describe only the test implementation; they do not establish real-company discovery quality or production throughput.
