export function agentStatus(agent: {
  enabled: boolean;
  paused: boolean;
  latest_run: { status: string } | null;
} | null): "paused" | "failed" | "disabled" | "running" | "queued" | "ready" | "idle";
export function sortAgents<T extends {
  name: string;
  enabled: boolean;
  paused: boolean;
  latest_run: { status: string } | null;
}>(agents: T[] | null | undefined): T[];
export function runRollup(runsByStatus: Record<string, number> | null | undefined): {
  total: number;
  failed: number;
  paused: number;
  active: number;
  completed: number;
};
export function blockerTitle(blocker: { kind: string } | null | undefined): string;
export function canControlRun(run: { status: string } | null | undefined, command: string): boolean;
export function canControlAgent(
  agent: { enabled: boolean; paused: boolean } | null | undefined,
  command: string,
): boolean;
export function controlLabel(command: string, kind: string): string;
export function pendingKey(kind: string, id: string): string;
export function formatTokens(value: unknown): string;
