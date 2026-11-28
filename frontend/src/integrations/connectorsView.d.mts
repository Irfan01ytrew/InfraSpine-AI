export interface IntegrationJobSummary {
  id: string;
  status: string;
  operation: string;
}

export interface BankStatus {
  active: string | null;
  available: boolean;
  source: string;
  admin_mode: string | null;
  environment_mode: string;
  http_configured: boolean;
  http_host: string | null;
  is_mock: boolean;
  real_bank: boolean;
  problem: string | null;
}

export interface BankLabel {
  title: string;
  detail: string;
  tone: "success" | "danger";
  state: "available" | "unavailable";
}

export function pendingApprovals<T extends IntegrationJobSummary>(jobs: readonly T[] | null | undefined): T[];
export function needsAttentionCount(jobs: readonly IntegrationJobSummary[] | null | undefined): number;
export function bankLabel(bank: BankStatus | null | undefined): BankLabel | null;
