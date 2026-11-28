# Step L — Predictive Continuity: what changed

Plain-language summary of the whole step (slices 1–8) for
the owner. Predictive Continuity is the prediction layer on top
of the company twin: it does not wait for a disruption, it says
in advance "if this employee, system or supplier is unavailable
for 7 days, these processes are affected" — and it never acts on
its own predictions. Every forecast is advisory only. A forecast
may become a proposal; a human approves it.

## What the software does now

- **Time-aware process forecasts (slice 1).** "Unavailable for
  N days" for people, teams, systems and suppliers returns a
  stable, numbered list of affected processes with owners,
  dependencies, live work in flight, and money at risk. The
  horizon changes the answer: a 1-day, 7-day and 30-day forecast
  for the same absence differ, because deadlines and SLAs inside
  the window decide what is "due". A process the caller may not
  see is omitted from the list, from every count, and from every
  money total. Backup coverage follows the recorded `backup_for`
  rule (a backup counts only when present and holding every
  required skill).
- **Standing forecast / always-on watch (slice 2).** Every
  critical entity is scanned at the standard horizons (1 / 7 /
  14 / 30 days) whenever the twin changes; an unchanged twin
  never re-fires. Rules as data (critical / due / uncovered /
  supplier single point) raise alerts with fingerprint dedupe,
  cooldown, and quiet hours 22:00–07:00 PKT (critical alerts
  break through).
- **Forecast records + staleness (slice 3).** Forecasts are
  stored (fingerprint, horizon, unavailable set, process list,
  `as_of`) with a read-only API. A new scan supersedes older
  records for the same root and horizon and stamps each with a
  drift score (trustworthy ≤25 / review ≤60 / stale >60), so
  nobody acts on a prediction the twin has outgrown. Retention:
  30 records / 90 days; current records are never pruned.
- **Calendar-aware forecasting (slice 4).** Planned absences
  from HR leave ("Ayesha is on leave 5–12 Dec") become forecasts
  for that window, with lead-time warnings when a backup
  holder's leave overlaps a critical process window (critical
  when every primary holder is also away for the whole overlap).
  Only `approved` leave counts as planned; pending, rejected and
  cancelled requests are skipped. The loader reads the real
  `hr_leave_requests` table, so it works before Step AG merges.
- **Live-work wiring (slice 5).** Real workflow runs, approvals
  and invoice batches in flight feed the forecast explanation:
  the scan refreshes the snapshot's live-work statuses from the
  live tables before it scans, so a forecast refreshes when live
  work changes, not only when the graph changes. Loaders read the
  real tables by reflection and never insert snapshot entities.
  `forecast-scan --live-work` runs the path end to end.
- **Forecast-driven proposals (slice 6).** An uncovered critical
  process or a supplier single point of failure becomes an
  approval-gated proposal — never auto-executed. Three action
  kinds only: request backup coverage (a graph approver
  decides), request supplier review (the owner decides), request
  backup review for a leave overlap (a graph approver decides).
  Approvals are bound to the exact action hash, expire after 24
  hours, are single-use, and follow separation of duties
  (proposer, approver and executor are three different people).
  An approved proposal is dispatched exactly once through
  `execute_once` with read-back verification, as a durable
  outbox event — the physical follow-up (assigning a backup,
  reviewing a supplier) is a human/operator step the event
  triggers. Money at risk stays on the finding summary and is
  shown only to the tenant owner. Proposals are idempotent per
  finding (re-proposing returns the existing row); rejected,
  expired or failed proposals may be re-proposed, cancelled ones
  stay cancelled. Retention: newest 100 terminal proposals per
  tenant / 90 days; pending or approved proposals are never
  pruned.
- **Drill reconciliation (slice 7).** A stored forecast is
  compared with a real continuity drill: did the drill hit the
  processes the forecast named? Every forecast process the drill
  did not recover and every drilled process the forecast never
  named goes to a review queue, with the drill's own timing
  differences (missed recovery targets, data loss over the
  limit) alongside. Nothing is auto-applied and no drill record
  is ever rewritten — a reviewer reads the queue and
  acknowledges each report as seen. When the drill proves a
  forecast wrong, the forecast's trust score drops (its drift
  score is raised) so nobody keeps acting on a prediction the
  real world disagreed with; a drill that matches the forecast
  leaves the score alone. Money at risk on a finding is shown
  only to the tenant owner.
- **Forecast dashboard (slice 8).** The forecasts, proposals
  and drill comparisons now have a page of their own
  ("Predictive continuity" in the sidebar, next to What-if
  forecasts — that page is unchanged). It shows the standing
  forecast (the newest prediction per unavailable person,
  system or supplier), a horizon switcher (1 / 7 / 14 / 30
  days — the horizon changes the answer), the numbered process
  list with the processes due inside the horizon highlighted,
  staleness badges (trustworthy / review / stale), the live
  work in flight behind each process, and the money at risk
  (owner only). An uncovered process can propose backup
  coverage and a failing supplier can propose a supplier
  review — but proposing only asks: the proposal waits for a
  human approval, the approval binds to the exact action
  shown, and dispatching runs it exactly once. The drill
  reconciliation review queue is on the same page: differences
  the drill proved, with a "mark as seen" button that changes
  nothing else. The page never executes anything on its own
  and never shows money the server hid from the viewer.

## Safety rules that hold for the whole step

- A forecast never approves, dispatches, grants access, restores
  data, switches a supplier, or pays. Money figures are exposure
  ("money at risk"), never payment instructions.
- Snapshot text is untrusted data: names and attributes are
  compared as data only and can never grant a permission, change
  a payee, or change an amount.
- Finance amounts stay owner-only in forecasts and proposals
  until the owner decides otherwise.
- Bounded inputs: forecasts refuse snapshots over 50,000
  entities / 100,000 relationships instead of grinding the
  machine. Test data is synthetic (fake people, systems,
  amounts). No secrets in forecasts, proposals, logs, or tests.

## Still owed (merge-time, not blockers for this slice)

- Step S must merge before L (or L rebases onto it): the
  `--from-twin` snapshot mode that builds the twin snapshot
  with `build_twin_snapshot` is owed then. Until then the
  snapshot is supplied as JSON. The snapshot builder must use
  DB row ids as snapshot entity ids, or live-work matching
  silently reports everything stale/added.
- When Step K merges, `staleness.py` is swapped for a thin
  import of `app.digital_twin.drift` (a cross-check test pins
  the equivalence until then).
- When Step AL merges, a proposal may attach a scenario
  reference from the AL what-if engine. Slice 6 builds no
  second engine and no proposal carries a scenario today.
- When Step G merges, the reconciliation scheduler can call the
  drill comparison directly instead of taking the comparison
  dict as input (CLI file / API body today).
- The full backend suite and the frontend gates are owed before
  any L slice merges (same quiet-VM gate as Step E).
- Owner decisions still open (safe defaults in place): standard
  horizons 1/7/14/30 with 7 the default; what counts as
  critical; finance amounts owner-only; who receives forecast
  alerts; forecast retention 30 records / 90 days; proposal
  authority (owner for money/supplier actions, department
  managers for coverage/backup actions).
