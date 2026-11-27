# Module 04 — Adaptive Process Intelligence

Checkpoint: 2026-11-23. Development branch `codex/module04-process-intelligence`, based on `157166b`. The primary checkout and live services are untouched. No production migration is authorized.

## Architecture audit and acceptance matrix

| Area | Existing implementation | Module 04 acceptance |
| --- | --- | --- |
| Authentication | HttpOnly session, CSRF, active users, MFA | Reuse on every API; no API keys in this module |
| Tenant isolation | User directory is deployment-wide; no shared tenant identity | Explicit module organizations and membership; no admin read bypass |
| Source permissions | PostgreSQL document ACL, quarantine, live revocation | Source allowlists intersect membership and current document access |
| Governance | KnowledgeProcess and policy links; reviewed Knowledge | Read evidence only; proposed baselines require a separate human reviewer |
| Mining | No event-log miner | Immutable source events, case namespace, variants, direct-follows graph, evidence |
| Intelligence | No event-based analysis | Cycle/wait/throughput, conformance, SLA, rework, handoffs, anomalies, drift |
| Adaptation | No versioned discovery snapshots | Durable jobs, revision fencing, per-case recalculation, bounded retries |
| UI | React/TypeScript enterprise shell and theme tokens | Authenticated dashboard, graph, filters, timelines, review, simulation, exports |
| Integration | Knowledge, Agents, governance and workflow foundations; no Company Graph API located | Versioned scoped contracts; no speculative cross-module table writes |
| Verification | Disposable PostgreSQL runner and regression suites | Real PostgreSQL, API security tests, browser acceptance, measured benchmarks |

## Release gates

- [ ] Purchase approval dataset ingested with multiple departments and variants.
- [ ] Discovery reconstructed from persisted events; evidence resolves under current ACL.
- [ ] Approved SOP baseline comparison detects extra approvals and order changes.
- [ ] Bottlenecks and SLA breaches match labeled expected results.
- [ ] Manager absence simulation reports assumptions and estimated impact.
- [ ] New events update models; drift is detected without policy mutation.
- [ ] Tenant isolation, source revocation, out-of-order, duplicate and conflicting input tests pass.
- [ ] Authenticated browser verifies graph, filters, variants, timeline, review and export.
- [ ] Benchmarks report latency, quality metrics and dataset limits.
- [ ] Relevant Knowledge/auth/governance regressions and frontend build pass.
- [ ] External connector credentials, Company Graph and Workflow telemetry adapters accepted by their owning modules.
- [ ] Real approved LLM extraction quality, deployment scale and independent production review complete.

Keep unchecked gates open until actual evidence is recorded. Never equate a synthetic benchmark with real company discovery quality.
