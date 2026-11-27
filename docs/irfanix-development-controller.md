# IRFANIX development controller

Run from the primary checkout. Keep the current working tree intact. Choose a reviewed baseline and create a separate linked Git worktree outside the primary checkout. For tasks that depend on uncommitted Agent Studio code, first review and capture that code on a development branch. The currently tracked baseline alone is missing the uncommitted Agent Studio additions.

```powershell
git worktree add --detach 'C:\path\irfanix-engineering' <reviewed-commit>
python scripts/irfanix_cycle.py status
python scripts/irfanix_cycle.py run --workspace 'C:\path\irfanix-engineering' --cycles 2 --seconds 300
```

The controller uses Codex CLI JSON events and an output schema. It checkpoints the exact task fingerprint, revision, worktree, child PID and Codex session ID in `.irfanix-cycle/state.sqlite3`. Structured events record lifecycle transitions and evidence. It saves no raw tool output, never selects a manual gate and moves to the next eligible task within the cycle limit. A write result waits for review before dependent tasks are released. No commits, pushes or deployments are automatic. The state folder is local and ignored by Git. Back it up with SQLite's backup facility if it must survive machine loss.

If a process stops, inspect the checkpoint, confirm the recorded child and descendants have exited, and review the worktree and any side effects. Use `resume TASK --evidence "reconciliation reference"` when a session ID exists, followed by the same run command and worktree. When the process stopped before a session ID was recorded, use `retry TASK --evidence "reconciliation reference"` only after confirming replay is safe. Hard termination can leave a child running until it exits or is stopped. The controller blocks new cycles while an attempt is unresolved.

Use `accept TASK --evidence "test or approval reference"` after independently reviewing output or completing a manual gate. For a task blocked by the execution environment and independently completed outside Codex, add `--external-review` and record how it was reviewed. A blocked result remains in the event history. Never use this flag to override required approval or claim unverified work.

On the current host, the PostgreSQL audit resumed from its original Codex session and the controller automatically selected the runbook task. Nested shell and worktree writes were rejected under the host policy. The runbook was completed and reviewed in the primary checkout. A later controller run completed the workflow-worker audit. The audit tasks consume bounded snapshots of explicitly selected tracked source. Run future write tasks from an authorized workspace with supported shell access, or review and record work completed outside the controller. See the release checklist for live test and WhatsApp evidence.

The reviewed source baseline is commit `e55fdac` on `codex/irfanix-development` in the separate `irfanix-development` worktree. The primary checkout retains the original changes and excluded generated documents, presentations and backup copies. The controller's baseline gate is recorded in local SQLite.

On the baseline worktree, Codex CLI 0.169.1 started and authenticated, but the workspace-write probe received `CreateProcess ... rejected: blocked by policy` for a PowerShell echo, Git status and file readback. No file was created. The direct Windows sandbox probe independently returned `registered Core launch requires an installed app`. These errors occurred before the proposed development command ran. A worktree ACL change alone has no verified effect on either error.

To enable unattended write cycles, have the authorized Windows/Codex administrator verify the Codex Core installation and registration for the account running the controller and authorize the Codex shell tool for this one development worktree under the applicable policy. Keep approval mode `never`, workspace-write isolation, and shell network access disabled. Re-run the harmless file-write/readback probe after authorization, then run two real source tasks through validation, independent review and recorded commits before calling autonomous writes operational.
