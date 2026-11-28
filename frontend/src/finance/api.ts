import { request } from "../api";

export interface FinanceScope { id: string; name: string; permissions: string[] }
export interface Reason { code: string; message: string; fields?: string[]; details?: Record<string, string | number | null> }
export interface Invoice {
  id: string; batch_id: string; group_id: string; seq: number; revision: number; invoice_number: string | null;
  vendor_name: string | null; vendor_id: string | null; po_number: string | null; department: string | null;
  amount: string | null; currency: string | null; invoice_date: string | null; status: string; reasons: Reason[];
  source_kind: string; source_ref: string | null; payment_id: string | null; verified_at: string | null;
  resolution_note: string | null; extraction_status: string | null;
}
export interface RunStep { id: string; node_id: string; status: string; phase: string; acting_user_id: string | null; verified_at: string | null }
export interface BatchRun { id: string; status: string; phase: string; blocked_reason: string | null; last_verified_node: string | null; failure_code: string | null; steps: RunStep[] }
export interface BatchSummary { total: number; verified: number; by_status: Record<string, number>; headline: string; remaining_ids: string[]; run: BatchRun | null }
export interface Batch { id: string; group_id: string; name: string; status: string; currency: string; tolerance_pct: string; tolerance_abs: string; round: number; wf_run_id: string | null; created_at: string | null }
export interface Payment {
  id: string; batch_id: string; round: number; vendor_id: string; vendor_name: string | null; amount: string; currency: string;
  payee_account: string; invoice_ids: string[]; status: string; approval_id: string | null; action_hash: string | null;
  execution_id: string | null; connector: string | null; external_ref: string | null; failure_code: string | null; verified_at: string | null;
}
export interface PaymentApproval {
  approval_id: string; action_hash: string; action: { type: string; params: Record<string, unknown>; amount: string | null; currency: string | null; subject: string | null };
  amount: string; currency: string; expires_at: string; payment_id: string; batch_id: string; batch_name: string;
  vendor_name: string; payee_account: string; invoice_numbers: (string | null)[]; requested_by: string; own_request: boolean;
}
export interface SpendRow { month: string; department: string; currency: string; amount: string; count: number }
export type ConnectorMode = "mock" | "http" | "http+mock";
export interface ConnectorStatus {
  source: string; admin_mode: string | null; environment_mode: string; modes: string[];
  http_configured: boolean; http_host: string | null; http_token_set: boolean;
  mock_allowed: boolean; production: boolean; test_override: boolean;
  active: string | null; available: boolean; problem: string | null; is_mock: boolean;
  real_bank: boolean; reachable?: boolean; probe_error?: string;
}
export interface Spend { months: string[]; from: string; departments: string[]; currencies: string[]; rows: SpendRow[] }
export interface Vendor { id: string; name: string; bank_account: string; currency: string; tax_id: string | null; status: string }
export interface VendorHistoryInvoice extends Invoice { bank_account_masked: string | null }
export interface VendorHistory { vendor: Vendor; invoices: VendorHistoryInvoice[] }
export interface AnomalySettings {
  group_id: string;
  thresholds: { min_history: number; outlier_mad_multiplier: string; outlier_ratio_when_flat: string; near_duplicate_days: number; velocity_window_days: number; velocity_max_prior: number };
  customized: string[];
}
export interface KnowledgeDocument { id: string; filename: string; folder: string | null; indexed: boolean; updated_at: string | null }
export interface SkippedDocument { document_id: string; filename: string; reason: string }
export interface InvoiceInput {
  invoice_number?: string; vendor_name?: string; po_number?: string; amount?: string; currency?: string;
  invoice_date?: string; department?: string; bank_account_on_invoice?: string;
}

const root = "/api/finance";
const call = <T,>(method: string, path: string, body?: unknown) => request<T>(method, root + path, body);

export const financeApi = {
  scopes: () => call<{ scopes: FinanceScope[]; reasons: Record<string, string>; connector: string }>("GET", "/scopes"),
  vendors: (group: string) => call<{ vendors: Vendor[] }>("GET", `/scopes/${group}/vendors`),
  batches: (group?: string) => call<{ batches: (Batch & { summary: BatchSummary })[] }>("GET", `/batches${group ? `?group_id=${group}` : ""}`),
  createBatch: (group: string, name: string, currency: string) => call<Batch>("POST", `/scopes/${group}/batches`, { name, currency }),
  batch: (id: string) => call<{ batch: Batch; summary: BatchSummary; invoices: Invoice[]; payments: Payment[] }>("GET", `/batches/${id}`),
  addInvoices: (id: string, invoices: InvoiceInput[], texts: string[]) => call<{ invoices: Invoice[] }>("POST", `/batches/${id}/invoices`, { invoices, texts }),
  upload: (id: string, file: File) => { const form = new FormData(); form.append("file", file); return call<{ invoices: Invoice[] }>("POST", `/batches/${id}/invoices/upload`, form); },
  knowledgeDocuments: (folder?: string, q?: string) => {
    const params = new URLSearchParams();
    if (folder) params.set("folder", folder);
    if (q) params.set("q", q);
    const query = params.toString();
    return call<{ documents: KnowledgeDocument[]; folders: string[] }>("GET", `/knowledge-documents${query ? `?${query}` : ""}`);
  },
  addInvoicesFromDocuments: (id: string, documentIds: string[], folder?: string | null) =>
    call<{ invoices: Invoice[]; skipped: SkippedDocument[] }>("POST", `/batches/${id}/invoices/from-documents`, { document_ids: documentIds, folder: folder ?? null }),
  start: (id: string) => call<{ batch: Batch; run_id: string }>("POST", `/batches/${id}/start`),
  exceptions: (group?: string) => call<{ invoices: Invoice[] }>("GET", `/exceptions${group ? `?group_id=${group}` : ""}`),
  vendorHistory: (group: string, vendorId: string) =>
    call<VendorHistory>("GET", `/scopes/${group}/vendors/${vendorId}/history`),
  anomalySettings: (group: string) => call<AnomalySettings>("GET", `/scopes/${group}/anomaly-settings`),
  setAnomalySettings: (group: string, thresholds: Record<string, number>) =>
    call<AnomalySettings>("PUT", `/scopes/${group}/anomaly-settings`, { thresholds }),
  resolve: (id: string, action: "correct" | "reject" | "approve", fields: InvoiceInput | null, note: string) =>
    call<Invoice>("POST", `/invoices/${id}/resolve`, { action, fields, note }),
  approvals: () => call<{ approvals: PaymentApproval[] }>("GET", "/approvals"),
  decide: (id: string, decision: "approve" | "reject", presented_hash: string, note = "") =>
    call<{ approval_id: string; status: string }>("POST", `/approvals/${id}/decide`, { decision, presented_hash, note }),
  spend: (months = 12, group?: string) => call<Spend>("GET", `/spend?months=${months}${group ? `&group_id=${group}` : ""}`),
  connector: () => call<ConnectorStatus>("GET", "/connector"),
  setConnector: (mode: ConnectorMode | null) => call<ConnectorStatus>("PUT", "/connector", { mode }),
  perform: (group: string, run: string, step: string) =>
    request<unknown>("POST", `/api/workflow-engine/scopes/${group}/runs/${run}/steps/${step}/perform`),
};
