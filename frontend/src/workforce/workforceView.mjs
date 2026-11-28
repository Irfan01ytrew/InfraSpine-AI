// Pure view helpers for the AI Workforce admin page. No DOM, no network:
// the page calls these and the node tests pin them down.

export function statusTone(status) {
  switch ((status ?? "").toLowerCase()) {
    case "active": return "success";
    case "paused": return "warning";
    case "retired": return "neutral";
    case "dead": return "danger";
    default: return "neutral";
  }
}

export function statusLabel(status) {
  switch ((status ?? "").toLowerCase()) {
    case "active": return "Active";
    case "paused": return "Paused";
    case "retired": return "Retired";
    case "dead": return "Dead";
    default: return status ? String(status) : "Unknown";
  }
}

export function filterAgents(agents, query, status) {
  const needle = (query ?? "").trim().toLowerCase();
  return agents.filter((agent) => {
    if (status && agent.effective_status !== status) return false;
    if (!needle) return true;
    return [agent.display_name, agent.agent_kind, agent.agent_id]
      .some((value) => String(value ?? "").toLowerCase().includes(needle));
  });
}

export function sortAgents(agents, key) {
  const rows = [...agents];
  if (key === "status") {
    return rows.sort((a, b) =>
      a.effective_status.localeCompare(b.effective_status)
      || a.display_name.localeCompare(b.display_name));
  }
  return rows.sort((a, b) => a.display_name.localeCompare(b.display_name));
}

export function formatMoneyUsd(amount) {
  if (amount === null || amount === undefined) return "—";
  const value = typeof amount === "string" ? Number(amount) : amount;
  if (!Number.isFinite(value)) return "—";
  return `$${value.toFixed(2)}`;
}

export function formatDurationMs(ms) {
  if (ms === null || ms === undefined) return "—";
  if (!Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

export function summarizeOrg(report) {
  const agents = report?.agents ?? [];
  return {
    totalAgents: report?.total_agents ?? agents.length,
    totalRuns: report?.total_runs ?? 0,
    totalCostUsd: report?.total_cost_usd ?? 0,
    successRate: report?.success_rate ?? null,
    pausedAgents: agents.filter((a) => a.effective_status === "paused").length,
    cannotStartAgents: agents.filter((a) => !a.can_start).length,
  };
}
