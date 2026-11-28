/** Types for the Step AK agent controls view helpers (agentControlsView.mjs). */

export type SpendState = "ok" | "near_limit" | "at_limit" | "over_limit";
export type ControlStatus = "active" | "paused" | "disabled" | "killed";

export interface AgentControlLike {
  status: ControlStatus | string;
  spend_state: SpendState | string;
  spend_used_usd: string | null;
  spend_limit_usd: string | null;
}

export declare function spendPercent(
  used: string | number | null,
  limit: string | number | null,
): number | null;
export declare function spendStateLabel(state: string): string;
export declare function statusLabel(status: string): string;
export declare function formatUsd(value: string | null | undefined): string;
export declare function needsAttention(control: AgentControlLike | null | undefined): boolean;
export declare function attentionCount(controls: AgentControlLike[] | null | undefined): number;
export declare function sortControls<T extends AgentControlLike>(controls: T[] | null | undefined): T[];
