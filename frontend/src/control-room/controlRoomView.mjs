// Pure view helpers for the Step Q control-room page (registry, roll-up,
// blockers first, one-click controls). No imports, no DOM, no fetch — the
// page in ControlRoomRegistryPage.tsx renders what these functions decide,
// and the backend stays the only authority on whether a control may run
// (every control still needs workflow.manage + an exact-action approval).
//
// The status sets mirror the backend exactly: run pause/resume states come
// from _RUN_COMMAND_STATES in app/control_room/controls.py (pause from
// queued/running, resume from paused/failed); blocker kinds come from
// app/control_room/service.py (run_paused / run_failed /
// run_awaiting_approval).

const AGENT_ATTENTION = {
  paused: 0,
  failed: 1,
  disabled: 2,
  running: 3,
  queued: 4,
  ready: 5,
  idle: 6,
};

/** One-word status for an agent row: attention first, healthy last. */
export function agentStatus(agent) {
  if (!agent) return "idle";
  if (!agent.enabled) return "disabled";
  if (agent.paused) return "paused";
  const run = agent.latest_run;
  if (!run) return "idle";
  if (run.status === "failed") return "failed";
  if (run.status === "running") return "running";
  if (run.status === "queued") return "queued";
  return "ready";
}

/** Agents needing attention first (paused, failed, disabled), then by name. */
export function sortAgents(agents) {
  return [...(agents ?? [])].sort((a, b) => {
    const rank = (agent) => AGENT_ATTENTION[agentStatus(agent)] ?? 6;
    const diff = rank(a) - rank(b);
    if (diff !== 0) return diff;
    return String(a?.name ?? "").localeCompare(String(b?.name ?? ""));
  });
}

/** Roll-up counts for the run summary cards. Unknown statuses still count. */
export function runRollup(runsByStatus) {
  const counts = runsByStatus ?? {};
  const n = (key) => Number(counts[key] ?? 0) || 0;
  const total = Object.values(counts).reduce((sum, value) => sum + (Number(value) || 0), 0);
  return {
    total,
    failed: n("failed"),
    paused: n("paused"),
    active: n("queued") + n("running") + n("compensating"),
    completed: n("completed"),
  };
}

/** Plain-language title for a blocker row. */
export function blockerTitle(blocker) {
  switch (blocker?.kind) {
    case "run_paused":
      return "Paused run";
    case "run_awaiting_approval":
      return "Waiting for approval";
    case "run_failed":
      return "Failed run";
    default:
      return "Needs attention";
  }
}

/** Whether the page should offer this run control (backend re-checks). */
export function canControlRun(run, command) {
  if (command === "pause") return ["queued", "running"].includes(run?.status);
  if (command === "resume") return ["paused", "failed"].includes(run?.status);
  return false;
}

/** Whether the page should offer this agent control (backend re-checks). */
export function canControlAgent(agent, command) {
  if (!agent) return false;
  if (command === "pause") return agent.enabled && !agent.paused;
  if (command === "resume") return Boolean(agent.paused);
  // Assign is offered for any listed agent; the backend refuses the Master
  // Agent (400) and an assignee outside the scope (403).
  if (command === "assign") return true;
  return false;
}

/** Button label for a control. */
export function controlLabel(command, kind) {
  const target = kind === "agent" ? "agent" : "run";
  if (command === "pause") return `Pause ${target}`;
  if (command === "resume") return `Resume ${target}`;
  if (command === "assign") return "Reassign agent";
  return command;
}

/** Key for the pending-approval map (one entry per controlled run/agent). */
export function pendingKey(kind, id) {
  return `${kind}:${id}`;
}

export function formatTokens(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "0";
  return n.toLocaleString("en-US");
}
