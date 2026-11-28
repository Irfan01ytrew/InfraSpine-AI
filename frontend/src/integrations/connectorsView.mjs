// Pure view helpers for the admin integrations connectors page
// (ModuleWorkspaces.tsx — IntegrationsWorkspace). Kept dependency-free so
// `node --test` can cover them without a DOM.
//
// Nothing here talks to the network or decides an approval: the page only
// displays status, and Approve/Reject posts the job digest back to the
// server, which re-checks it before changing anything.

export function pendingApprovals(jobs) {
  if (!Array.isArray(jobs)) return [];
  return jobs.filter((job) => job?.status === "pending_approval");
}

export function needsAttentionCount(jobs) {
  if (!Array.isArray(jobs)) return 0;
  return jobs.filter((job) =>
    ["dead_letter", "uncertain", "pending_approval"].includes(job?.status),
  ).length;
}

export function bankLabel(bank) {
  if (!bank) return null;
  const title = bank.active ?? "Not configured";
  const mode = bank.is_mock
    ? "mock (no real bank)"
    : bank.real_bank
      ? "real bank"
      : "http";
  const host =
    bank.http_configured && bank.http_host ? ` · ${bank.http_host}` : "";
  const detail =
    `Source: ${bank.source}` +
    (bank.admin_mode
      ? ` · admin mode ${bank.admin_mode}`
      : ` · environment ${bank.environment_mode}`) +
    ` · ${mode}${host}`;
  return {
    title,
    detail,
    tone: bank.available ? "success" : "danger",
    state: bank.available ? "available" : "unavailable",
  };
}
