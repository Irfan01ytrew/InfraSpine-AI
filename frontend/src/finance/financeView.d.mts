export interface Reason { code: string; message?: string }
export interface InvoiceLike { reasons?: Reason[] }
export interface SpendRow { month: string; department: string; currency: string; amount: string; count: number }
export interface Spend { months: string[]; rows: SpendRow[] }
export interface StepLike { id: string; node_id: string; status: string }
export interface RunLike { status: string; steps: StepLike[] }

export function headline(summary: { verified: number; total: number } | null | undefined): string;
export function reasonCounts(invoices: readonly InvoiceLike[]): { code: string; count: number }[];
export function spendSeries(spend: Spend | null | undefined, currency?: string): {
  months: string[]; departments: string[]; max: number;
  series: { month: string; total: number; parts: { department: string; amount: number }[] }[];
};
export function shortHash(hash: string | null | undefined): string;
export function spendMonthLabel(month: string | null | undefined, language?: string | null): string;
export interface SkippedDocument { filename: string; reason: string }
export function skippedSummary(skipped: readonly SkippedDocument[] | null | undefined): string;
export function actionableStep<T extends StepLike>(run: { status: string; steps: T[] } | null | undefined,
  permissions: readonly string[] | undefined): (T & { permission: string }) | null;
