# Knowledge AI continuous development

Mission started 2026-11-23 (Asia/Karachi). The full mission remains **in progress**. No ChatGPT parity or production-readiness claim is supported yet.

## Baseline and isolation

- Existing checkout: `feature/enterprise-platform-ui`, starting HEAD `157166b`.
- The initial checkout contains 33 modified tracked files plus untracked Agent Studio, controller, migration and documentation work. Preserve it. Commit only reviewed mission increments; do not stage unrelated baseline changes.
- Read `irfanix-release-checklist.md`, `irfanix-backlog.json`, and controller status before work. Interrupted controller tasks remain gated; this mission does not start a controller or nested Codex process.
- Use a disposable PostgreSQL cluster, synthetic accounts and a separate local API/browser candidate. Do not migrate, restart, deploy to, or write test data into the existing application instance.

## Acceptance backlog

| ID | Requirement | Status / required evidence |
| --- | --- | --- |
| K01 | General knowledge, reasoning, coding, mathematics, multilingual answers and dates | Open: representative real-model answers, scored against a fixed baseline and holdout |
| K02 | Contextual follow-ups and long-term memory without topic contamination | Open: isolated context/ACL regression and long real-model conversations |
| K03 | Live search, deep research, truthful citations and tool selection | Open: dated live-provider and authenticated UI evidence, source verification and outages |
| K04 | Permission-aware hybrid retrieval, decomposition, chunking and reranking | Open: retrieval/answer benchmark, negative authorization cases, index recovery |
| K05 | PDF, Office, images/OCR, audio/video and structured ingestion | Open: actual fixtures for every format, workers, retry/recovery and source previews |
| K06 | Rich chat answers | In progress: safe Markdown, highlighted fenced code, copy controls, tables, citations and downloadable artifacts |
| K07 | Professional responsive chat workspace | In progress: usable chat/sidebar/library navigation, themes, streaming, mobile and keyboard acceptance |
| K08 | Persistent chat search, history, folders and pinning | Open: real API persistence and owner isolation, reload and browser verification |
| K09 | Uploads, drag/drop, voice, image search/generation and visual responses | Open: authenticated browser/provider/media acceptance |
| K10 | Models, settings, memory and research controls | Open: functional API-connected controls and per-role authorization |
| K11 | Knowledge dashboard | In progress: actual document/index state, workers, usage, latency, errors and quality outcomes; no invented metrics |
| K12 | Local/cloud routing, fallback, cache, streaming, latency and cost | Open: real-model online/offline failure and recovery evidence |
| K13 | Recurring QA, monitoring, root-cause analysis and safe recovery | Open: reproducible regression/evaluation/browser harness and durable report history |
| K14 | Complete Admin/Manager/Employee integration and release acceptance | Open: authenticated browser at desktop/mobile sizes, RBAC, regressions, independent competitive evaluation |

## Current findings

- `Markdown.tsx` supports headings, flat lists, inline code, bounded links and citations, but lacks fenced code blocks and Markdown tables.
- Knowledge currently reserves three columns for history, chat and the full document library; on mobile it stacks them. A dedicated workspace navigation and dashboard are missing.
- Existing conversation SSE emits provisional tokens and an authoritative saved response. Browser cancellation, navigation races and duplicate-submit behavior need acceptance coverage.
- Previous benchmark reports are historical evidence only. The fixed 61-case answer set and its human-review cases must remain unchanged when measuring a new baseline.

## Evidence log

- Repository status, existing release checklist, backlog and `python scripts/irfanix_cycle.py status` inspected. No controller write cycle started.
- First isolated PostgreSQL baseline attempt started and stopped its disposable cluster, but `psql` setup exited 2 before pytest. No test result claimed; diagnostic/retry pending.

## Resume

Inspect current Git status and this evidence log. Continue K06/K07/K11 while preserving all other acceptance gates. Run focused checks followed by relevant regressions, build UI, inspect authenticated browser behavior, record actual results and commit scoped increments. Do not mark this mission complete until every acceptance row has end-to-end evidence.
