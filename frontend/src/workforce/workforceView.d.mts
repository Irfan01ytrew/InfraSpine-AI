export function statusTone(status: string | null | undefined): "success" | "warning" | "danger" | "neutral";
export function statusLabel(status: string | null | undefined): string;
export function filterAgents<T extends { agent_kind: string; agent_id: string; display_name: string; effective_status: string }>(
  agents: T[],
  query: string,
  status: string,
): T[];
export function sortAgents<T extends { display_name: string; effective_status: string }>(
  agents: T[],
  key: "name" | "status",
): T[];
export function formatMoneyUsd(amount: number | string | null | undefined): string;
export function formatDurationMs(ms: number | null | undefined): string;
export function summarizeOrg(report: {
  agents?: { effective_status: string; can_start: boolean }[];
  total_agents?: number;
  total_runs?: number;
  total_cost_usd?: number;
  success_rate?: number | null;
}): {
  totalAgents: number;
  totalRuns: number;
  totalCostUsd: number;
  successRate: number | null;
  pausedAgents: number;
  cannotStartAgents: number;
};
