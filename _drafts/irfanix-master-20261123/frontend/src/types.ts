// Mirrors the backend response schemas (app/schemas). No type here holds a secret.

export interface User {
  id: string;
  email: string;
  role: "admin" | "manager" | "employee" | "user";
  mfa_enrollment_required?: boolean;
  mfa_reauthentication_required?: boolean;
  /** Delegated business permissions, e.g. "reports.view" (administrators hold all). */
  capabilities?: string[];
}

export interface PartnerIdentity {
  id: string;
  email: string;
  workspace_id: string;
  workspace_slug: string;
  workspace_name: string;
}

export interface PartnerCitation {
  source_id: string;
  source_record_id: string;
  title: string;
  excerpt: string;
  location_label: string | null;
  download_available: boolean;
}

export interface PartnerAnswer {
  answer: string;
  insufficient_evidence: boolean;
  citations: PartnerCitation[];
  retrieved_sources: number;
}

export interface PartnerWorkspace {
  id: string;
  slug: string;
  name: string;
  status: "active" | "suspended";
  created_at: string;
}

export interface PartnerInvitation {
  id: string;
  workspace_id: string;
  email: string;
  created_at: string;
  expires_at: string;
  accepted_at: string | null;
  revoked_at: string | null;
  token: string | null;
}

export interface PartnerAdminIdentity {
  id: string;
  workspace_id: string;
  email: string;
  is_active: boolean;
  created_at: string;
}

export interface PartnerSource {
  id: string;
  workspace_id: string;
  title: string;
  content_sha256: string;
  status: "active" | "archived";
  created_at: string;
  archived_at: string | null;
  source_format: "text" | "pdf" | "docx" | "xlsx" | "pptx";
  index_status: "pending" | "processing" | "ready" | "failed";
  index_attempts: number;
  index_error_code: string | null;
}

export interface DepartmentGroup {
  id: string;
  name: string;
  kind: "department" | "project" | "client" | "custom";
  source: "local" | "directory";
  external_id: string | null;
  member_count: number;
  created_at: string;
  parent_id?: string | null;
  owner_id?: string | null;
  lifecycle_status?: "active" | "archived";
}

export interface WorkspaceSummaryReport {
  group_id: string;
  name: string;
  kind: string;
  parent_id: string | null;
  owner_id: string | null;
  lifecycle_status: "active" | "archived";
  member_count: number;
  child_project_count: number;
  total_documents: number;
  total_bytes: number;
  total_chunks: number;
}

export interface InterviewQuestion {
  key: string;
  prompt: string;
  required: boolean;
}

export interface InterviewTemplate {
  id: string;
  slug: string;
  title: string;
  description: string;
  questions: InterviewQuestion[];
  is_active: boolean;
  built_in: boolean;
}

export interface InterviewOptions {
  templates: InterviewTemplate[];
  managed_templates: InterviewTemplate[];
  departments: Array<{ id: string; name: string }>;
  can_manage_templates: boolean;
}

export interface InterviewReview {
  id: number;
  revision_number: number;
  reviewer_user_id: string;
  reviewer_email: string;
  decision: "approved" | "changes_requested";
  rationale: string;
  reviewed_at: string;
}

export interface InterviewSopRevision {
  id: number;
  revision_number: number;
  title: string;
  body: string;
  answers_snapshot: Record<string, string>;
  submitted_by_user_id: string;
  created_at: string;
}

export interface InterviewSession {
  id: string;
  template_id: string;
  template_title: string;
  template_questions: InterviewQuestion[];
  owner_user_id: string;
  owner_email: string;
  department_group_id: string | null;
  department_name: string | null;
  status: "in_progress" | "submitted" | "changes_requested" | "approved" | "withdrawn";
  answers: Record<string, string>;
  sop_title: string;
  sop_body: string;
  revision_number: number;
  submitted_revision: number | null;
  submitted_at: string | null;
  approved_at: string | null;
  created_at: string;
  updated_at: string;
  can_edit: boolean;
  can_review: boolean;
  reviews: InterviewReview[];
  sop_revisions: InterviewSopRevision[];
}

export interface DepartmentAssignment {
  id: string;
  name: string;
  manager: boolean;
}

export interface ManagedUser {
  id: string;
  email: string;
  role: string;
  is_active: boolean;
  created_at: string;
  departments: DepartmentAssignment[];
}

export interface UserInvitation {
  id: string;
  email: string;
  role: "manager" | "employee";
  department_ids: string[];
  role_template_id?: string | null;
  created_at: string;
  expires_at: string;
  accepted_at: string | null;
  revoked_at: string | null;
  token?: string | null;
}

export interface CsvInvitationPreviewRow {
  row_number: number;
  email: string | null;
  role: "manager" | "employee" | null;
  department_ids: string[];
  departments: string[];
  position_id?: string | null;
  position_title?: string | null;
  reports_to_id?: string | null;
  reports_to_email?: string | null;
  valid: boolean;
  errors: string[];
}

export interface CsvInvitationPreview {
  total_rows: number;
  valid_rows: number;
  invalid_rows: number;
  invitations_sent: false;
  rows: CsvInvitationPreviewRow[];
}

export interface PermissionGrant {
  id: string;
  user_id: string;
  email: string;
  permission: "knowledge.review" | "knowledge.publish" | "data.steward" | "reports.view" | "people.invite" | "training.manage" | "agents.manage";
  category: KnowledgeCategory | null;
  scope_group_id: string | null;
  granted_by: string;
  granted_at: string;
  revoked_at: string | null;
}

export interface PermissionCatalogEntry {
  permission: PermissionGrant["permission"];
  resource: string;
  action: string;
  scope_dimensions: Array<"category" | "access_group">;
}

export interface RoleTemplatePermission {
  permission: PermissionGrant["permission"];
  categories: KnowledgeCategory[];
}

export interface RoleTemplate {
  id: string;
  name: string;
  description: string;
  base_role: "manager" | "employee";
  version: number;
  permissions: RoleTemplatePermission[];
  is_active: boolean;
  supersedes_id: string | null;
  created_at: string;
}

export interface RoleTemplateChangeRequest {
  id: string;
  role_template_id: string;
  template_name: string;
  current_version: number;
  requested_by: string;
  requested_by_email: string;
  proposed_description: string;
  proposed_permissions: RoleTemplatePermission[];
  status: "pending" | "approved" | "rejected";
  decided_by: string | null;
  decided_by_email: string | null;
  decision_rationale: string | null;
  new_template_id: string | null;
  created_at: string;
  decided_at: string | null;
}

export interface RoleTemplateAssignment {
  id: string;
  role_template_id: string;
  template_name: string;
  template_version: number;
  user_id: string;
  email: string;
  base_role: "manager" | "employee";
  department_ids: string[];
  grant_count: number;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
}

export interface ModelOption {
  id: string;
  label: string;
  description: string;
  context_window: number;
  max_output_tokens: number;
  default_output_tokens: number;
}

export interface SelectOption {
  value: string;
  label: string;
}

export interface SettingField {
  key: string;
  label: string;
  type: "number" | "integer" | "select";
  default: number | string;
  description: string;
  minimum: number | null;
  maximum: number | null;
  step: number | null;
  options: SelectOption[];
  /** Maximum/default come from the selected model's output-token limits. */
  per_model_limit: boolean;
}

export type SettingValue = number | string;
export type Settings = Record<string, SettingValue>;

export interface ProviderMetadata {
  id: string;
  display_name: string;
  description: string;
  docs_url: string;
  models: ModelOption[];
  default_model: string;
  settings: SettingField[];
}

export interface CreateCustomProviderInput {
  display_name: string;
  base_url: string;
  model: string;
  api_key: string;
}

export interface CredentialSummary {
  id: string;
  key_hint: string;
  verified_at: string;
  activated_at: string | null;
  created_at: string;
}

export interface ProviderState {
  provider: string;
  display_name: string;
  base_url: string | null;
  configured: boolean;
  model: string;
  settings: Settings;
  is_active: boolean;
  version: number;
  active_credential: CredentialSummary | null;
  pending_credential: CredentialSummary | null;
  updated_at: string | null;
  image_sharing_authorized: boolean;
  image_sharing_authorized_at: string | null;
}

export interface ProviderList {
  mode: "database";
  providers: ProviderState[];
}

export interface TestConnectionResult {
  ok: boolean;
  reason: string | null;
  message: string;
  detail: string | null;
  tested: "provided_key" | "pending_key" | "active_key";
  model: string;
  latency_ms: number;
}

export interface EmbeddingModelStatus {
  model_name: string;
  dimensions: number;
  device: "cpu";
  available_locally: boolean;
  loaded: boolean;
  status: "ready" | "not_loaded" | "unavailable";
}

export interface EmbeddingModelTestResult {
  ok: boolean;
  model_name: string;
  dimensions: number;
  latency_ms: number;
  message: string;
}

export type ParserFormat = "pdf" | "docx" | "xlsx" | "pptx" | "txt" | "csv";

export interface DocumentParserFormatInfo {
  file_format: ParserFormat;
  extension: string;
  active_parser: string;
  fallback_parser: string | null;
  active_available: boolean;
  fallback_available: boolean | null;
}

export interface DocumentParserInfo {
  docling_enabled: boolean;
  formats: DocumentParserFormatInfo[];
}

export interface DocumentParserTestResult {
  file_format: ParserFormat;
  ok: boolean;
  parser_used: string;
  latency_ms: number;
  page_count: number | null;
  chunk_count: number | null;
  error: string | null;
}

export interface OcrEngineStatus {
  engine_name: string;
  enabled: boolean;
  runtime_available: boolean;
  status: "ready" | "disabled" | "unavailable";
  docling_available: boolean;
  rapidocr_available: boolean;
  onnxruntime_available: boolean;
  pytorch_available: boolean;
  backend: string;
  device: string;
  full_page_ocr_enabled: boolean;
  orientation_classifier_enabled: boolean;
  configured_languages: string[];
  capabilities: string[];
}

export interface OcrEngineTestResult {
  ok: boolean;
  engine_name: string;
  parser_used: string;
  latency_ms: number;
  page_count: number | null;
  chunk_count: number | null;
  recognized_characters: number;
  message: string;
  error: string | null;
}

export interface AuditEntry {
  id: number;
  created_at: string;
  actor_email: string | null;
  action: string;
  target: string | null;
  outcome: "success" | "failure";
  details: Record<string, unknown>;
  ip_address: string | null;
}

export interface TraceRow {
  id: number;
  created_at: string;
  request_id: string | null;
  user_email: string | null;
  conversation_id: string | null;
  message_id: number | null;
  route: string;
  status: "ok" | "error";
  strategy: string | null;
  decision: string | null;
  provider: string | null;
  model: string | null;
  insufficient_evidence: boolean | null;
  general_knowledge: boolean | null;
  question_chars: number;
  history_turns: number;
  retrieved_count: number;
  cited_count: number;
  timings: Record<string, number>;
  llm_calls: number;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  latency_ms: number;
  error_type: string | null;
  feedback: -1 | 1 | null;
}

export interface TraceDetail extends TraceRow {
  retrieved: Array<{ rank: number; chunk_id: number; document_id: string; filename: string | null; score: number; cited: boolean }>;
  error_stack: string | null;
}

export interface TraceSummary {
  daily?: { day: string; questions: number; errors: number; refused: number; citation_refusals: number }[];
  days: number;
  questions: number;
  errors: number;
  refused: number;
  general: number;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  llm_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  helpful: number;
  not_helpful: number;
  decisions: Array<{ decision: string; count: number }>;
}

export interface UsageReport {
  generated_at: string;
  period_start: string;
  period_end: string;
  summary: TraceSummary;
  daily: Array<{
    day: string;
    questions: number;
    errors: number;
    refused: number;
    helpful: number;
    not_helpful: number;
  }>;
  providers: Array<{
    provider: string;
    model: string | null;
    calls: number;
    latency_p50_ms: number | null;
    latency_p95_ms: number | null;
  }>;
}

export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

export type FileFormat = "pdf" | "docx" | "xlsx" | "pptx" | "txt" | "csv" | "md" | "json" | "html" | "png" | "jpeg" | "webp" | "audio" | "video" | "doc" | "xls" | "ppt";

export interface RagDocument {
  id: string;
  filename: string;
  file_format: FileFormat;
  visibility: "private" | "shared" | "restricted";
  source_system: "upload" | "file_server";
  status: DocumentStatus;
  version: number;
  size_bytes: number;
  page_count: number | null;
  chunk_count: number;
  attempts: number;
  error_code: string | null;
  error_message: string | null;
  searchable: boolean;
  indexed_version: number | null;
  indexed_at: string | null;
  is_owner: boolean;
  can_modify: boolean;
  created_at: string;
  updated_at: string;
}

/** Whether queued documents will be processed: in "worker" mode a separate worker process must run. */
export interface IngestionStatus {
  mode: "worker" | "embedded";
  workers_online: number;
}

export interface RagDocumentList {
  documents: RagDocument[];
  max_upload_mb: number;
  embedding_model: string;
  ingestion?: IngestionStatus | null;
}

export interface DocumentVersionInfo {
  version: number;
  filename: string;
  file_format: FileFormat;
  sha256: string;
  size_bytes: number;
  archived_at: string;
  reason: "replacement" | "folder_sync";
}

export interface DocumentDiffChange {
  kind: "added" | "removed";
  text: string;
}

export interface DocumentObligationImpact {
  id: string;
  title: string;
  status: "open" | "acknowledged" | "completed";
  source_version: number;
  source_quote: string;
  quote_found_in_to_version: boolean;
}

export interface DocumentVersionDiff {
  document_id: string;
  filename: string;
  from_version: number;
  to_version: number;
  from_sha256: string;
  to_sha256: string;
  method: "deterministic_text_diff";
  added_lines: number;
  removed_lines: number;
  changes: DocumentDiffChange[];
  truncated: boolean;
  obligation_impacts: DocumentObligationImpact[];
}

export interface FinancialExtractionField {
  value: string | null;
  raw: string | null;
  valid: boolean;
  source: Record<string, unknown> | null;
}

export interface FinancialLineItem {
  description: string | null;
  quantity: string | null;
  unit_price: string | null;
  amount: string | null;
  currency: string | null;
  source: Record<string, unknown>;
}

export interface FinancialExtraction {
  document_id: string;
  filename: string;
  document_version: number;
  profile: "invoice" | "purchase_order" | "form";
  date_order: "dmy" | "mdy" | "ymd";
  status: "ready" | "needs_review";
  fields: Record<string, FinancialExtractionField>;
  line_items: FinancialLineItem[];
  exceptions: Array<{
    code: string;
    field: string;
    message: string;
    severity: "error" | "warning";
    source: Record<string, unknown> | null;
  }>;
  note: string;
  record_id: string | null;
  review_status: "not_saved" | "pending" | "reviewed";
  review: FinancialFormReviewRevision | null;
  review_history: FinancialFormReviewRevision[];
}

export interface FinancialFormReviewRevision {
  revision: number;
  reviewed_by_email: string;
  reviewed_at: string;
  rationale: string;
  fields: Record<string, string | null>;
  line_items: FinancialLineItem[];
  reviewed_exception_indexes: number[];
}

export interface FinancialFormReviewQueueItem {
  record_id: string;
  document_id: string;
  filename: string;
  document_version: number;
  profile: "invoice" | "purchase_order" | "form";
  extraction_status: "ready" | "needs_review";
  exception_count: number;
  created_at: string;
  can_review: boolean;
}

export interface DatabaseAnalysisColumn {
  name: string;
  type: "text" | "integer" | "numeric" | "date" | "timestamp" | "boolean";
}

export interface DatabaseAnalysisTable {
  name: string;
  columns: DatabaseAnalysisColumn[];
}

export interface DatabaseAnalysisCatalog {
  enabled: boolean;
  source_label: string | null;
  tables: DatabaseAnalysisTable[];
  message: string | null;
}

export interface DatabaseAnalysisQuery {
  table: string;
  operation: "rows" | "count" | "sum" | "mean" | "min" | "max" | "count_distinct";
  value?: string;
  group_by: string[];
  filters: Array<{ column: string; operation: "eq" | "ne" | "contains" | "gt" | "gte" | "lt" | "lte"; value: string }>;
  limit: number;
}

export interface DatabaseAnalysisResult {
  source_label: string;
  table: string;
  retrieved_at: string;
  operation: DatabaseAnalysisQuery["operation"];
  columns: string[];
  rows: string[][];
  returned_rows: number;
  row_limit: number;
  truncated: boolean;
  provenance: string;
}

export type CloudConnectorType = "google_drive" | "onedrive" | "sharepoint";

export interface CloudConnector {
  id: string;
  name: string;
  connector_type: CloudConnectorType;
  visibility: "private" | "shared" | "restricted";
  department_group_id: string | null;
  sync_interval_minutes: number;
  is_active: boolean;
  sync_in_progress: boolean;
  last_sync_status: "success" | "partial" | "failed" | null;
  last_sync_error_count: number;
  last_synced_at: string | null;
  created_at: string;
}

export interface CloudConnectorCreateInput {
  name: string;
  connector_type: CloudConnectorType;
  client_id: string;
  client_secret: string;
  tenant_id?: string;
  refresh_token?: string;
  drive_id?: string;
  folder_id?: string;
  department_group_id: string;
  visibility: "restricted";
  sync_interval_minutes: number;
}

export interface CloudConnectorSyncResult {
  connector_id: string;
  status: "success" | "partial" | "failed";
  files_discovered: number;
  files_synced: number;
  files_queued: number;
  files_unchanged: number;
  files_skipped: number;
  files_deleted: number;
  permission_reconciliation_scanned: number;
  permission_reconciliation_updated: number;
  permission_reconciliation_quarantined: number;
  permission_reconciliation_failed: number;
  permission_reconciliation_complete: boolean | null;
  errors: string[];
}

export type DocumentObligationStatus = "open" | "acknowledged" | "completed";

export interface DocumentObligationEvent {
  id: number;
  actor_user_id: string | null;
  action: string;
  from_status: string | null;
  to_status: string | null;
  rationale: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

export interface DocumentObligation {
  id: string;
  document_id: string;
  document_filename: string;
  source_version: number;
  source_sha256: string;
  source_quote: string;
  source_page_number: number;
  source_locator: Record<string, unknown>;
  source_current: boolean;
  title: string;
  rationale: string;
  assigned_to_user_id: string;
  assigned_to_email: string;
  created_by_user_id: string;
  due_at: string | null;
  status: DocumentObligationStatus;
  version: number;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  can_manage: boolean;
  can_update_status: boolean;
  history: DocumentObligationEvent[];
}

export interface DocumentObligationList {
  obligations: DocumentObligation[];
}

/** Where a cited passage is: a PDF page, Word section, sheet range, slide, or line/row range. */
export interface CitationLocator {
  kind: "page" | "section" | "sheet" | "slide" | "lines" | "rows" | string;
  label?: string;
  [field: string]: unknown;
}

export interface Citation {
  source_id: string;
  document_id: string;
  filename: string;
  page_number: number;
  /** Absent on answers saved before structured locators existed (those are PDF pages). */
  locator?: CitationLocator | null;
  location?: string | null;
  chunk_index: number;
  score: number;
  snippet: string;
  available: boolean;
}

export interface RagAnswer {
  memories_applied?: number; // personal memories that shaped this answer (never cited)
  general_knowledge?: boolean; // answered from the model's general knowledge, not from the user's documents
  answer: string;
  insufficient_evidence: boolean;
  citations: Citation[];
  retrieved_chunks: number;
  strategy:
    | "none"
    | "small_talk"
    | "whole_document"
    | "semantic"
    | "semantic_with_context"
    | "focused_documents"
    | "table_analysis"
    | "library_overview"
    | "page_images"
    | "chart"
    | "memory"
    | "general"
    | "web_research"
    | "web_export"
    | "hybrid_research"
    | "image_generation"
    | "image_edit"
    | "web_image_search"
    | "identity"
    | "library_inventory"
    | "upload_capability";
  page_images: PageImage[];
  chart: Chart | null;
  model: string | null;
  provider: string | null;
}

export type ResearchSpecialist = "evidence_finder" | "policy_analyst" | "exception_checker";

export interface ResearchFinding {
  specialist?: ResearchSpecialist;
  question: string;
  answer: string;
  insufficient_evidence: boolean;
  citations: Citation[];
  retrieved_chunks: number;
}

export interface ResearchReport {
  question: string;
  findings: ResearchFinding[];
  model: string | null;
  provider: string | null;
  grounded_only: boolean;
}

export interface TrainingModule {
  title: string;
  question: string;
  answer: string;
  insufficient_evidence: boolean;
  citations: Citation[];
  retrieved_chunks: number;
}

export interface TrainingQuizQuestion {
  question: string;
  choices: string[];
  citations: Citation[];
}

export interface TrainingPlan {
  id: string | null;
  topic: string;
  modules: TrainingModule[];
  quiz: TrainingQuizQuestion[];
  quiz_required: boolean;
  model: string | null;
  provider: string | null;
  grounded_only: boolean;
  status: "in_progress" | "completed";
  created_at: string | null;
  completed_at: string | null;
}

export interface LegacyBrowserRun {
  id: string;
  requester_user_id: string | null;
  approver_user_id: string | null;
  label: string;
  reason: string;
  start_url: string;
  link_labels: string[];
  status: "pending_approval" | "running" | "completed" | "failed" | "rejected" | "cancelled";
  attempts: number;
  failure_code: string | null;
  requested_at: string;
  approved_at: string | null;
  completed_at: string | null;
}

export interface LegacyBrowserStatus {
  enabled: boolean;
  maximum_pages: number;
  maximum_requests: number;
  maximum_response_bytes: number;
  maximum_result_characters: number;
}

export interface LegacyBrowserCapture {
  run: LegacyBrowserRun;
  pages: Array<{ label: string; url: string; title: string; text: string }>;
  request_count: number;
  response_bytes: number;
}

export interface TrainingProgressItem {
  id: string;
  topic: string;
  status: "in_progress" | "completed";
  module_count: number;
  created_at: string;
  completed_at: string | null;
  assigned: boolean;
  assignment_id: string | null;
  due_at: string | null;
}

export interface TrainingProgressListResponse {
  items: TrainingProgressItem[];
}

export interface TrainingQuizAttemptSummary {
  attempt_id: number;
  score: number;
  max_score: number;
  percentage: number;
  passed: boolean;
  created_at: string;
}

export interface TrainingQuizAttemptListResponse {
  attempts: TrainingQuizAttemptSummary[];
}

export interface TrainingQuizResult extends TrainingQuizAttemptSummary {
  feedback: Array<{ correct: boolean; correct_choice: number; explanation: string }>;
}

export interface TrainingPracticeQuestion {
  question_index: number;
  question: TrainingQuizQuestion;
}

export interface TrainingPracticeScenario {
  scenario_id: string;
  course_id: string;
  questions: TrainingPracticeQuestion[];
  created_at: string;
}

export interface TrainingPracticeResult {
  scenario_id: string;
  attempt_id: number;
  score: number;
  max_score: number;
  percentage: number;
  passed: boolean;
  feedback: Array<{ correct: boolean; correct_choice: number; explanation: string }>;
  created_at: string;
}

export interface TrainingPracticeHistoryItem {
  scenario_id: string;
  question_indexes: number[];
  created_at: string;
  completed_at: string | null;
  score: number | null;
  max_score: number | null;
  percentage: number | null;
}

export interface TrainingAssignment {
  assignment_id: string;
  course_id: string;
  topic: string;
  assignee_user_id: string;
  assignee_email: string;
  department_id: string;
  status: "assigned" | "completed";
  due_at: string | null;
  created_at: string;
  completed_at: string | null;
  latest_score: number | null;
  latest_max_score: number | null;
}

export interface TrainingReminder {
  id: string;
  course_id: string;
  topic: string;
  reminder_type: "due_soon" | "overdue";
  due_at: string;
  created_at: string;
  read_at: string | null;
}

export interface WorkflowDefinition {
  id: string;
  definition_key: string;
  version: number;
  name: string;
  department_group_id: string;
  department_name: string;
  trigger_type: "manual" | "interval" | "service_request" | "request_sla";
  interval_minutes: number | null;
    action_type: "create_task" | "notify_member" | "assign_personal_task";
  assignee_user_id: string | null;
  task_title: string | null;
  task_body: string | null;
  due_days: number | null;
  notification_recipient_user_id: string | null;
  notification_title: string | null;
  notification_body: string | null;
  enabled: boolean;
  next_run_at: string | null;
  created_at: string;
  can_manage: boolean;
}

export interface WorkflowDryRun {
  workflow_id: string;
  workflow_version: number;
  valid: boolean;
  side_effects_performed: false;
  nodes: Array<{
    id: string;
    node_type: "trigger" | "approval" | "action" | "completion";
    title: string;
    description: string;
  }>;
  edges: Array<{ source: string; target: string }>;
  checks: Array<{
    code: "department_access" | "active_assignee" | "approval_gate" | "allowlisted_action";
    passed: boolean;
    summary: string;
  }>;
  task_preview: {
    title: string;
    body: string;
    assignee_email: string;
    due_days: number | null;
  } | null;
  notification_preview: {
    title: string;
    body: string;
    recipient_email: string;
  } | null;
}

export interface WorkflowRun {
  id: string;
  workflow_id: string;
  workflow_name: string;
  requested_by_user_id: string | null;
  department_group_id: string;
  status: "pending_approval" | "queued" | "processing" | "completed" | "rejected" | "cancelled" | "failed";
  attempts: number;
  last_error_code: string | null;
  created_at: string;
  completed_at: string | null;
  approval_decision: "approved" | "rejected" | null;
  approver_user_id: string | null;
  approval_rationale: string | null;
  approval_decided_at: string | null;
  can_approve: boolean;
  can_cancel: boolean;
  can_retry: boolean;
}

export interface WorkflowTask {
  id: string;
  run_id: string;
  assignee_user_id: string;
  department_group_id: string;
  title: string;
  body: string;
  due_at: string | null;
  status: "open" | "completed";
  created_at: string;
  completed_at: string | null;
}

export interface PersonalTask {
  id: string;
  created_by_user_id: string;
  assignee_user_id: string;
  department_group_id: string | null;
  title: string;
  body: string;
  due_at: string | null;
  status: "open" | "completed" | "cancelled";
  created_at: string;
  completed_at: string | null;
  cancelled_at: string | null;
  created_by_email: string;
  assignee_email: string;
}

export interface PersonalTaskOptions {
  departments: Array<{
    id: string;
    name: string;
    members: Array<{ user_id: string; email: string }>;
  }>;
}

export interface PersonalTaskReminder {
  id: string;
  task_id: string;
  title: string;
  reminder_type: "due_soon" | "overdue";
  due_at: string;
  created_at: string;
  read_at: string | null;
  email_status: "disabled" | "pending" | "sending" | "sent" | "retry" | "failed" | "cancelled";
}

export interface WorkflowNotification {
  id: string;
  run_id: string;
  task_id: string | null;
  notification_type: "run_approved" | "run_rejected" | "task_assigned" | "approval_required" | "workflow_message";
  message_title: string | null;
  message_body: string | null;
  created_at: string;
  read_at: string | null;
}

export type AgentType = "knowledge_research" | "market_research" | "company_analysis" | "employee_training";
export type AgentTool = "knowledge_search" | "public_web_research" | "company_data_analysis" | "training_plan";
export type AgentKnowledgeScope = "requester_acl" | "department_only";

export interface AgentProfile {
  id: string;
  agent_key: string;
  version: number;
  name: string;
  agent_type: AgentType;
  department_group_id: string;
  department_name: string;
  knowledge_scope: AgentKnowledgeScope;
  tool_policy: AgentTool[];
  enabled: boolean;
  created_at: string;
  can_manage: boolean;
}

export type AgentStudioToolName =
  | "web_search" | "browse_url" | "browser_capture" | "file_list" | "file_read"
  | "create_file" | "generate_image" | "rag_search" | "my_work" | "remember" | "recall" | "delegate_agent";

export interface AgentStudioTool {
  name: AgentStudioToolName;
  description: string;
  requires_approval_by_default: boolean;
  available: boolean;
  availability_note: string | null;
}

export interface AgentStudioAgent {
  id: string;
  version: number;
  name: string;
  description: string;
  instructions: string;
  tool_permissions: AgentStudioToolName[];
  approval_tools: AgentStudioToolName[];
  delegate_agent_ids: string[];
  rag_enabled: boolean;
  max_steps: number;
  provider_id: string | null;
  model: string | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export type AgentStudioAgentInput = Omit<AgentStudioAgent, "id" | "version" | "enabled" | "created_at" | "updated_at">;

export interface AgentStudioThread {
  id: string;
  agent_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  last_run_status: string | null;
  last_run_at: string | null;
}

export interface AgentStudioStep {
  tool: string;
  status: string;
  summary: string;
  result?: unknown;
}

export interface AgentStudioRun {
  id: string;
  agent_id: string;
  thread_id: string;
  status: "queued" | "running" | "awaiting_approval" | "completed" | "failed" | "cancelled" | "recovery_required";
  prompt: string;
  answer: string | null;
  steps: AgentStudioStep[];
  pending_approval: { id: string; tool_name: string; summary: string; arguments: Record<string, unknown>; action_digest: string; expires_at: string | null } | null;
  failure_code: string | null;
  durable: boolean;
  attempts: number;
  cancel_requested: boolean;
  available_at: string | null;
  created_at: string;
  started_at: string;
  finished_at: string | null;
}

export interface AgentStudioMemory {
  id: string;
  memory_key: string;
  value: string;
  updated_at: string;
}

export interface AgentStudioWhatsAppConnection {
  id: string;
  agent_id: string;
  agent_name: string;
  phone_number_id: string;
  business_account_id: string;
  allowed_senders: string[];
  api_version: string;
  enabled: boolean;
  verified_name: string | null;
  display_phone_number: string | null;
  last_verified_at: string | null;
  access_token_configured: boolean;
  app_secret_configured: boolean;
  verify_token_configured: boolean;
  webhook_url: string;
  created_at: string;
  updated_at: string;
}

export interface AgentStudioWhatsAppInput {
  agent_id: string;
  phone_number_id: string;
  business_account_id: string;
  allowed_senders: string[];
  api_version: string;
  access_token?: string;
  app_secret?: string;
  verify_token?: string;
}

export interface AgentRunResponse {
  run_id: string;
  profile_id: string;
  profile_version: number;
  capability: AgentTool;
  result: RagAnswer | TrainingPlan | MarketResearchReport;
}

export interface AgentRunRecord {
  run_id: string;
  profile_id: string;
  profile_version: number;
  capability: AgentTool;
  status: "queued" | "running" | "completed" | "failed";
  attempts: number;
  failure_code: string | null;
  retry_available: boolean;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  content_purged_at?: string | null;
  result: RagAnswer | TrainingPlan | MarketResearchReport | null;
}

export interface AgentRunRetentionPreview {
  dry_run: true;
  retention_days: number;
  cutoff_at: string;
  eligible_runs: number;
  preserved_fields: string[];
}

export interface AgentRunRetentionPurgeResult {
  result: "purged";
  retention_days: number;
  cutoff_at: string;
  purged_runs: number;
  remaining_eligible_runs: number;
  batch_size: number;
  audit_event: "agent.profile_run_content_purged";
}

export interface AgentDepartment {
  id: string;
  name: string;
}

export interface AgentCatalogDefinition {
  name: string;
  summary: string;
  agent_type: AgentType;
  knowledge_scope: AgentKnowledgeScope;
  tool_policy: AgentTool[];
}

export interface AgentCatalogTemplate extends AgentCatalogDefinition {
  id: string;
  slug: string;
  version: number;
  published_at: string;
  can_instantiate: boolean;
}

export interface AgentCatalogList {
  items: AgentCatalogTemplate[];
  can_publish: boolean;
  can_review: boolean;
}

export interface AgentCatalogVersion extends AgentCatalogDefinition {
  id: string;
  version: number;
  status: "draft" | "pending_review" | "approved" | "rejected" | "superseded";
  author_email: string;
  created_at: string;
  submitted_by_email: string | null;
  submitted_at: string | null;
  reviewed_by_email: string | null;
  reviewed_at: string | null;
  review_rationale: string | null;
}

export interface AgentCatalogManagedTemplate {
  id: string;
  slug: string;
  latest_version: number;
  published_version: number | null;
  created_by_email: string;
  created_at: string;
  revoked_at: string | null;
  revoked_by_email: string | null;
  revoke_rationale: string | null;
  versions: AgentCatalogVersion[];
}

export interface AgentCatalogManageList {
  items: AgentCatalogManagedTemplate[];
  can_publish: boolean;
  can_review: boolean;
}

export interface AgentCatalogGrant {
  id: string;
  user_id: string;
  email: string;
  permission: "publish" | "review";
  granted_by_email: string;
  created_at: string;
  revoked_at: string | null;
  revoked_by_email: string | null;
}

export interface AgentCatalogDraftCreated {
  template_id: string;
  version_id: string;
  slug: string;
  version: number;
  status: "draft";
}

export interface AgentCatalogVersionLifecycle {
  id: string;
  template_id: string;
  version: number;
  status: "pending_review" | "approved" | "rejected";
  submitted_at: string;
  reviewed_by_user_id: string | null;
  reviewed_at: string | null;
  review_rationale: string | null;
}

export interface AgentCatalogRevocation {
  id: string;
  slug: string;
  revoked_at: string;
  revoked_by_user_id: string;
  revoke_rationale: string;
}

export interface MarketResearchStatus {
  enabled: boolean;
  web_search_available: boolean;
  gemini_available: boolean;
  message: string;
}

export interface MarketResearchSource {
  title: string;
  url: string;
}

export interface MarketResearchReport {
  topic: string;
  answer: string;
  sources: MarketResearchSource[];
  generated_at: string;
  model: string;
  provider: string;
}

export interface ChatTurn {
  question: string;
  answer: string;
}

export interface PageImage {
  document_id: string;
  filename: string;
  page_number: number;
  url: string;
}

export interface ChartSeries {
  name: string;
  values: (number | null)[];
  sources: (string | null)[];
}

export interface Chart {
  title: string;
  unit: string;
  layout: "grouped" | "stacked";
  kind?: "bar" | "line" | "pie" | "combo";
  line_series?: string[]; // combo only: series drawn as a line on the right axis
  categories: string[];
  series: ChartSeries[];
}

export interface ConversationSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

export interface ConversationMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  response: RagAnswer | null;
  sources_unavailable: number;
  withheld: boolean; // the answer used a document the user can no longer read; its text is replaced
  feedback?: -1 | 1 | null; // assistant only: the owner's rating
}

export interface ConversationDetail extends ConversationSummary {
  messages: ConversationMessage[];
}

export interface SendMessageResult {
  conversation: ConversationSummary;
  user_message: ConversationMessage;
  assistant_message: ConversationMessage;
}

export type MemoryKind = "preference" | "terminology" | "fact";

export interface MemoryItem {
  id: string;
  kind: MemoryKind;
  subject: string | null;
  content: string;
  status: "active" | "superseded";
  source: "chat" | "manual";
  supersedes_id: string | null;
  created_at: string;
  superseded_at: string | null;
}

export interface MemoryList {
  items: MemoryItem[];
  active_count: number;
  max_items: number;
}

export interface MemorySaveResult {
  item: MemoryItem;
  outcome: "created" | "duplicate" | "superseded" | "updated";
  replaced_id: string | null;
}

export type ClaimKind = "proposal" | "correction";
export type ClaimStatus = "submitted" | "withdrawn";
export type KnowledgeCategory = "policy" | "financial" | "legal" | "client" | "operational" | "general";

export type ClaimSource =
  | { kind: "document"; document_id: string; filename: string }
  | { kind: "url"; url: string }
  | { kind: "note"; note: string };

export interface ClaimEvent {
  id: number;
  action: "submitted" | "withdrawn";
  from_status: ClaimStatus | null;
  to_status: ClaimStatus;
  created_at: string;
}

export interface Claim {
  id: string;
  kind: ClaimKind;
  statement: string;
  category: KnowledgeCategory;
  scope_group_id: string | null;
  sources: ClaimSource[];
  status: ClaimStatus;
  created_at: string;
  withdrawn_at: string | null;
  verified: false;
}

export interface ClaimDetail extends Claim {
  history: ClaimEvent[];
}

export interface ClaimList {
  items: Claim[];
}

export interface GovernanceDecision {
  stage: "review" | "publish";
  approver_email: string;
  rationale: string;
  decided_at: string;
}

export interface GovernanceClaim {
  id: string;
  kind: ClaimKind;
  statement: string;
  claimant_email: string;
  category: KnowledgeCategory;
  scope_group_id: string | null;
  sources: ClaimSource[];
  created_at: string;
  decisions: GovernanceDecision[];
  allowed_stages: Array<"review" | "publish">;
}

export interface GovernanceClaimList {
  items: GovernanceClaim[];
}

export interface GovernanceKnowledge {
  id: string;
  entry_id: string;
  statement: string;
  author_email: string;
  category: KnowledgeCategory;
  visibility: KnowledgeVisibility;
  scope_group_id: string | null;
  created_at: string;
  sources: Array<{ filename: string; page_number: number; quote: string }>;
  decisions: GovernanceDecision[];
  allowed_stages: Array<"review" | "publish">;
}

export interface GovernanceKnowledgeList {
  items: GovernanceKnowledge[];
}

export interface KnowledgeFreshnessHistory {
  reviewer_email: string;
  rationale: string;
  reviewed_at: string;
  next_review_due_at: string;
}

export interface StaleKnowledge {
  id: string;
  entry_id: string;
  statement: string;
  author_email: string;
  category: KnowledgeCategory;
  visibility: KnowledgeVisibility;
  scope_group_id: string | null;
  created_at: string;
  last_reviewed_at: string | null;
  review_due_at: string;
  sources: Array<{ filename: string; page_number: number; quote: string }>;
  last_freshness_review: KnowledgeFreshnessHistory | null;
}

export interface StaleKnowledgeList {
  items: StaleKnowledge[];
}

export interface KnowledgeConflictReview {
  reviewer_email: string;
  decision: "confirmed" | "dismissed";
  rationale: string;
  decided_at: string;
}

export interface KnowledgeConflictStatement {
  revision_id: string;
  statement: string;
  author_email: string;
  visibility: KnowledgeVisibility;
  scope_group_id: string | null;
  sources: Array<{ filename: string; page_number: number; quote: string }>;
}

export interface KnowledgeConflict {
  id: string;
  category: KnowledgeCategory;
  status: "open" | "confirmed" | "dismissed";
  detected_at: string;
  submitted: KnowledgeConflictStatement;
  existing: KnowledgeConflictStatement;
  review: KnowledgeConflictReview | null;
}

export interface KnowledgeConflictList {
  items: KnowledgeConflict[];
  next_cursor: string | null;
}

export interface PolicyProcessSource {
  filename: string;
  page_number: number;
  quote: string;
}

export interface PolicyImpactPolicy {
  entry_id: string;
  category: "policy" | "legal" | "operational";
  scope_group_id: string | null;
  published_revision_id: string;
  published_revision_number: number;
  published_statement: string;
  published_sources: PolicyProcessSource[];
  pending_revision_id: string | null;
  pending_revision_number: number | null;
  pending_statement: string | null;
  pending_sources: PolicyProcessSource[];
  can_manage: boolean;
}

export interface PolicyProcess {
  id: string;
  name: string;
  description: string;
  scope_group_id: string | null;
  owner_email: string;
  created_at: string;
  policies: PolicyImpactPolicy[];
}

export interface PolicyCandidate {
  entry_id: string;
  revision_id: string;
  revision_number: number;
  category: "policy" | "legal" | "operational";
  statement: string;
  scope_group_id: string | null;
  sources: PolicyProcessSource[];
}

export interface PolicyProcessAssignee {
  user_id: string;
  email: string;
  role: string;
}

export interface PolicyExceptionEvent {
  action: "assigned" | "reassigned" | "acknowledged" | "resolved";
  actor_email: string;
  from_assignee_email: string | null;
  to_assignee_email: string;
  from_status: "open" | "acknowledged" | "resolved" | null;
  to_status: "open" | "acknowledged" | "resolved";
  rationale: string;
  created_at: string;
}

export interface PolicyException {
  id: string;
  process_id: string;
  process_name: string;
  entry_id: string;
  revision_id: string;
  policy_statement: string;
  status: "open" | "acknowledged" | "resolved";
  assigned_to: PolicyProcessAssignee;
  created_by_email: string;
  rationale: string;
  due_at: string | null;
  created_at: string;
  events: PolicyExceptionEvent[];
  can_acknowledge: boolean;
  can_resolve: boolean;
  can_reassign: boolean;
}

export interface EmployeeHandoff {
  id: string;
  author_email: string;
  department_id: string;
  department_name: string;
  title: string;
  body: string;
  status: "submitted" | "acknowledged";
  created_at: string;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
}

export interface HandoffList {
  items: EmployeeHandoff[];
}

export type MeetingStatus = "draft" | "submitted" | "changes_requested" | "approved";
export interface MeetingSource {
  id: string;
  filename: string;
  file_format: "audio" | "video";
  version: number;
  chunk_count: number;
  indexed_at: string | null;
}
export interface MeetingCitationDraft {
  text: string;
  evidence_chunk_ids: number[];
}
export interface MeetingTaskDraft {
  title: string;
  body: string;
  assignee_user_id: string;
  due_at: string | null;
  evidence_chunk_ids: number[];
}
export interface MeetingRevision {
  id: number;
  version: number;
  editor_email: string;
  minutes_markdown: string;
  decisions: MeetingCitationDraft[];
  proposed_tasks: MeetingTaskDraft[];
  created_at: string;
}
export interface MeetingReview {
  decision: "approved" | "changes_requested";
  rationale: string;
  reviewer_email: string;
  reviewed_at: string;
  revision_id: number;
}
export interface MeetingActionTask {
  id: string;
  title: string;
  body: string;
  assignee_user_id: string;
  assignee_email: string;
  evidence_chunk_ids: number[];
  due_at: string | null;
  status: "open" | "completed";
  created_at: string;
  completed_at: string | null;
}
export interface MeetingAssignee {
  id: string;
  email: string;
}
export interface Meeting {
  id: string;
  source_document_id: string;
  source_filename: string;
  source_document_version: number;
  source_is_current: boolean;
  owner_email: string;
  title: string;
  status: MeetingStatus;
  current_revision: number;
  submitted_revision_id: number | null;
  created_at: string;
  updated_at: string;
  can_edit: boolean;
  can_review: boolean;
}
export interface MeetingDetail extends Meeting {
  revision: MeetingRevision;
  reviews: MeetingReview[];
  action_tasks: MeetingActionTask[];
  eligible_assignees: MeetingAssignee[];
}
export interface MeetingTranscriptChunk {
  id: number;
  page_number: number;
  locator: Record<string, unknown> | null;
  content: string;
}
export interface MeetingTranscript {
  document_id: string;
  document_version: number;
  chunks: MeetingTranscriptChunk[];
  next_after_chunk_id: number | null;
}

export type ServiceRequestCategory = "it" | "hr" | "access" | "facilities" | "other" | "purchase" | "expense";

/** What the current reviewer may do with a purchase request or expense claim. */
export interface ServiceRequestApproval {
  can_approve: boolean;
  reason: string | null;
  your_limit: string | null;
  limit_currency: string | null;
  suggested_approver_email: string | null;
}

export interface ApprovalStep {
  order: number;
  label: string;
  approver_kind: "manager" | "position" | "person";
  approver: string;
  stage?: number;
  stage_mode?: "all" | "any";
  status: "waiting" | "pending" | "approved" | "rejected" | "cancelled";
  final: boolean;
  decided_by_email: string | null;
  decided_at: string | null;
  comment: string | null;
  on_behalf_of_email?: string | null;
  pending_since?: string | null;
  reminder_count?: number;
  escalated_at?: string | null;
}

export interface ApprovalDelegation {
  id: string;
  delegator_user_id: string;
  delegator_email: string;
  delegate_user_id: string;
  delegate_email: string;
  starts_on: string;
  ends_on: string;
  reason: string | null;
  current: boolean;
}

export interface ApprovalChainStep {
  id: string;
  category: "purchase" | "expense";
  step_order: number;
  label: string;
  approver_kind: "manager" | "position" | "person";
  position_id: string | null;
  position_title: string | null;
  user_id: string | null;
  user_email: string | null;
  min_amount: string | null;
  parallel_with_previous: boolean;
  stage_mode: "all" | "any";
}

export interface Budget {
  id: string;
  name: string;
  department_group_id: string;
  department_name: string;
  category: "purchase" | "expense" | "all";
  period_start: string;
  period_end: string;
  amount: string;
  currency: string;
  enforcement: "warn" | "block";
  approved: string;
  committed: string;
  remaining: string;
}

export interface RequestBudgetLine {
  name: string;
  amount: string;
  currency: string;
  approved: string;
  committed: string;
  after_approval: string;
  would_exceed: boolean;
  enforcement: "warn" | "block";
}

export interface ApprovalLimit {
  id: string;
  category: "purchase" | "expense";
  position_id: string | null;
  position_title: string | null;
  user_id: string | null;
  user_email: string | null;
  max_amount: string;
  currency: string;
}
export type ServiceRequestPriority = "low" | "normal" | "high" | "urgent";
export type ServiceRequestStatus = "submitted" | "acknowledged" | "in_progress" | "waiting_requester" | "resolved" | "rejected" | "cancelled";

export interface ServiceRequestEvent {
  id: number;
  actor_email: string;
  action: "submitted" | "status_changed" | "commented" | "cancelled" | "sla_escalated";
  from_status: ServiceRequestStatus | null;
  to_status: ServiceRequestStatus;
  comment: string | null;
  created_at: string;
}

export interface RequestFormField {
  key: string | null;
  label: string;
  type: "text" | "long_text" | "number" | "date" | "choice" | "yes_no" | "file";
  required: boolean;
  options: string[];
  help: string | null;
  // Shown only when an earlier choice or yes/no field (by key, or label before first save) has this answer.
  show_if?: { field: string; equals: string } | null;
}

export interface WebhookDelivery {
  id: string;
  event: string;
  status: "pending" | "sending" | "retry" | "sent" | "failed" | "cancelled";
  attempts: number;
  response_status: number | null;
  last_error: string | null;
  duration_ms: number | null;
  created_at: string;
  sent_at: string | null;
  next_attempt_at: string | null;
}

export interface Webhook {
  id: string;
  name: string;
  url: string;
  events: string[];
  is_active: boolean;
  consecutive_failures: number;
  disabled_reason: string | null;
  created_at: string;
  last_delivery: WebhookDelivery | null;
}

export interface RequestAttachment {
  id: string;
  filename: string;
  size_bytes: number;
  content_type: string;
}

export interface RequestForm {
  id: string;
  name: string;
  description: string | null;
  category: ServiceRequestCategory;
  department_group_id: string | null;
  department_name: string | null;
  fields: RequestFormField[];
  version: number;
  updated_at: string;
  audience_departments?: Array<{ id: string; name: string }>;
  audience_positions?: Array<{ id: string; name: string }>;
}

export interface ServiceRequestItem {
  form_name?: string | null;
  form_answers?: Array<{ label: string; value: string; attachments?: RequestAttachment[] }>;
  id: string;
  requester_user_id: string;
  requester_email: string;
  department_group_id: string | null;
  department_name: string | null;
  assigned_to_user_id: string | null;
  assigned_to_email: string | null;
  category: ServiceRequestCategory;
  priority: ServiceRequestPriority;
  subject: string;
  description: string;
  status: ServiceRequestStatus;
  /** Purchase requests and expense claims only (decimal string). */
  amount?: string | null;
  currency?: string | null;
  approval?: ServiceRequestApproval | null;
  approval_steps?: ApprovalStep[];
  can_decide_step?: boolean;
  step_blocker?: string | null;
  budgets?: RequestBudgetLine[];
  created_at: string;
  updated_at: string;
  closed_at: string | null;
  response_due_at: string;
  response_sla_met_at: string | null;
  sla_escalated_at: string | null;
  response_sla_overdue: boolean;
  can_manage: boolean;
  can_cancel: boolean;
  events: ServiceRequestEvent[];
}

export interface ServiceRequestNotification {
  id: string;
  request_id: string;
  notification_type: "response_sla_escalated" | "approval_step_pending" | "approval_step_reminder" | "approval_step_escalated";
  created_at: string;
  read_at: string | null;
}

export interface ServiceRequestList {
  items: ServiceRequestItem[];
}

export type KnowledgeVisibility = "private" | "restricted" | "shared";
export type KnowledgeStatus = "draft" | "submitted" | "published" | "superseded" | "withdrawn";

export interface KnowledgeEvidence {
  id: number;
  document_id: string | null;
  document_key: string;
  document_version: number;
  filename: string;
  chunk_key: number;
  page_number: number;
  start_offset: number;
  end_offset: number;
  quote: string;
  prefix: string;
  suffix: string;
  orphan_reason: "document_revision_changed" | "source_deleted" | "passage_removed" | null;
  orphaned_at: string | null;
}

export interface KnowledgeRevision {
  id: string;
  revision_number: number;
  statement: string;
  category: KnowledgeCategory;
  visibility: KnowledgeVisibility;
  scope_group_id: string | null;
  status: KnowledgeStatus;
  created_at: string;
  published: boolean;
  evidence: KnowledgeEvidence[];
}

export interface KnowledgeDetail {
  id: string;
  created_at: string;
  revisions: KnowledgeRevision[];
}

export interface KnowledgeList {
  items: Array<{ id: string; created_at: string; current: KnowledgeRevision }>;
}

/** A model a user may pick for their own chat (configured provider with a key). */
export interface ModelOption {
  provider: string;
  provider_label: string;
  model: string;
  model_label: string;
  is_default: boolean;
}

export interface VectorIndex {
  name: "documents" | "knowledge";
  status: "healthy" | "out_of_sync" | "needs_rebuild" | "rebuilding";
  vectors: number;
  expected_vectors: number;
  needs_rebuild: boolean;
  rebuilding: boolean;
  persisted: boolean;
  snapshot_bytes: number | null;
  change_log_bytes: number | null;
  memory_bytes: number | null;
}

export interface VectorStoreStatus {
  backend: string;
  supported_backends: string[];
  index_type: string;
  dimension: number;
  embedding_model: string;
  embedding_dimension: number;
  status: "healthy" | "degraded";
  issues: string[];
  indexes: VectorIndex[];
}

export interface FolderImportProgress {
  source_id: string;
  total_documents: number;
  queued: number;
  processing: number;
  ready: number;
  failed: number;
  total_bytes: number;
  last_updated: string | null;
}

export interface FolderSyncSource {
  id: string;
  source_id: string;
  name: string;
  relative_path: string;
  owner_user_id: string;
  visibility: "private" | "shared" | "restricted";
  department_group_id: string | null;
  interval_minutes: number;
  enabled: boolean;
  next_sync_at: string;
  last_run_at: string | null;
  consecutive_failures: number;
  last_error_code: string | null;
}

export interface FolderSyncRun {
  id: string;
  status: "running" | "completed" | "partial" | "failed";
  attempt: number;
  scanned: number;
  created: number;
  updated: number;
  unchanged: number;
  removed: number;
  duplicates: number;
  failed: number;
  error_code: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface FolderSyncNotification {
  id: string;
  source_id: string;
  source_name: string;
  run_id: string;
  status: "completed" | "partial" | "failed";
  created: number;
  updated: number;
  removed: number;
  failed: number;
  error_code: string | null;
  created_at: string;
  read_at: string | null;
}

export interface CompanyProfile {
  company_name: string;
  industry: string;
  brand_name: string;
  primary_color: string;
  setup_complete: boolean;
  template_id: string | null;
  template_version: string | null;
  /** Regional formatting; null keeps each viewer's browser setting. */
  currency?: string | null;
  time_zone?: string | null;
  locale?: string | null;
}

export interface IndustryTemplate {
  template_id: string;
  version: string;
  name: string;
  industry: string;
  description: string;
  default_departments: string[];
  knowledge_areas: string[];
  default_roles: string[];
}

export interface IndustryTemplateApply {
  template_id: string;
  version: string;
  created_departments: string[];
  existing_departments: string[];
  created_roles: string[];
  existing_roles: string[];
}

export interface SignedLicense {
  payload: {
    product: "infraspine-ai";
    license_id: string;
    customer: string;
    deployment_id: string;
    issued_at: string;
    expires_at: string;
    seat_limit: number;
    features: string[];
  };
  signature: string;
}

export interface LicenseStatus {
  status: "not_configured" | "missing" | "invalid" | "expired" | "valid";
  enforcement_enabled: boolean;
  deployment_id: string;
  license_id: string | null;
  customer: string | null;
  expires_at: string | null;
  seat_limit: number | null;
  features: string[];
}

export interface DeploymentOverview extends CompanyProfile {
  license: LicenseStatus;
}

export interface MediaTranscriptionStatus {
  enabled: boolean;
  runtime_available: boolean;
  model_available: boolean;
  status: "ready" | "disabled" | "unavailable";
  device: "cpu" | "cuda";
  max_file_mb: number;
  max_duration_seconds: number;
  segment_seconds: number;
  video_frame_ocr_enabled: boolean;
  video_frame_ocr_available: boolean;
  video_frame_interval_seconds: number;
  video_max_frames: number;
}

export interface ExpertiseProfile {
  user_id: string;
  tags: string[];
}

export interface ExpertiseMatch {
  user_id: string;
  email: string;
  tags: string[];
  shared_departments: string[];
}

export interface ExpertiseSearchResponse {
  skill: string;
  matches: ExpertiseMatch[];
  next_cursor: string | null;
}

export type ForecastDecimal = string | number;
export type ForecastPeriodUnit = "period" | "month" | "quarter" | "year";

export interface ForecastObservationInput {
  period: string;
  value: string;
  document_id: string;
  excerpt: string;
}

export interface ForecastCitation {
  document_id: string;
  filename: string;
  document_version: number;
  chunk_id: number;
  page_number: number;
  locator: Record<string, unknown> | null;
  excerpt: string;
}

export interface ForecastObservation {
  period: string;
  value: ForecastDecimal;
  citation: ForecastCitation;
}

export interface ForecastProjection {
  period: string;
  value: ForecastDecimal;
}

export interface ForecastAssumptions {
  growth_rate_pct: ForecastDecimal;
  additive_adjustment: ForecastDecimal;
  horizon_periods: number;
  period_unit: ForecastPeriodUnit;
  notes: string;
}

export interface ForecastScenarioSummary {
  id: string;
  title: string;
  metric: string;
  unit: string;
  model_version: string;
  input_digest: string;
  observation_count: number;
  citation_count: number;
  assumptions: ForecastAssumptions;
  projections: ForecastProjection[];
  created_at: string;
}

export interface ForecastScenario extends Omit<ForecastScenarioSummary, "observation_count" | "citation_count"> {
  observations: ForecastObservation[];
}

export type ProposalCoverageStatus = "draft" | "submitted" | "changes_requested" | "approved" | "withdrawn";
export type ProposalAssessment = "not_assessed" | "meets" | "partial" | "gap";

export interface ProposalCitation {
  source_id: string;
  document_id: string;
  filename: string;
  page_number: number;
  locator: Record<string, unknown> | null;
  location: string | null;
  chunk_index: number;
  score: number;
  snippet: string;
  available: boolean;
}

export interface ProposalRequirement {
  id: string;
  position: number;
  requirement: string;
  answer: string;
  insufficient_evidence: boolean;
  retrieved_chunks: number;
  analyzed_at: string | null;
  evidence_current: boolean;
  citations: ProposalCitation[];
  origin_evidence: ProposalRequirementEvidence[];
  origin_evidence_current: boolean;
  assessment: ProposalAssessment;
  assessment_rationale: string;
  assessed_by_user_id: string | null;
  assessed_at: string | null;
}

export interface ProposalCoverageReview {
  id: number;
  revision_number: number;
  reviewer_user_id: string;
  reviewer_email: string;
  decision: "approved" | "changes_requested";
  rationale: string;
  reviewed_at: string;
}

export interface ProposalCoverageSummary {
  id: string;
  title: string;
  status: ProposalCoverageStatus;
  revision_number: number;
  owner_user_id: string;
  owner_email: string;
  department_group_id: string | null;
  department_name: string | null;
  requirement_count: number;
  analyzed_count: number;
  assessment_count: number;
  created_at: string;
  updated_at: string;
}

export interface ProposalCoverageCase extends ProposalCoverageSummary {
  submitted_at: string | null;
  decided_at: string | null;
  requirements: ProposalRequirement[];
  reviews: ProposalCoverageReview[];
  can_edit: boolean;
  can_submit: boolean;
  can_review: boolean;
}

export interface ProposalCoverageOptions {
  departments: Array<{ id: string; name: string }>;
}

export interface ProposalRequirementEvidence {
  chunk_id: number;
  document_id: string;
  document_version: number;
  document_sha256: string;
  filename: string;
  location: string;
  excerpt: string;
}

export interface ProposalRequirementCandidate {
  requirement: string;
  evidence: ProposalRequirementEvidence[];
}

export interface ProposalRequirementExtraction {
  source_document_count: number;
  candidates: ProposalRequirementCandidate[];
}

export type JobFamily =
  | "executive" | "management" | "professional" | "technical" | "operations"
  | "administration" | "governance" | "sales" | "frontline" | "external" | "other";

/** A job title. It never grants software access. */
export interface Position {
  id: string;
  title: string;
  job_family: JobFamily;
  level: number | null;
  description: string;
  is_active: boolean;
  holders: number;
  created_at: string;
  updated_at: string;
}

export interface LibraryPosition {
  title: string;
  job_family: JobFamily;
  level: number | null;
  description: string;
  added: boolean;
}

export interface OrgChartPerson {
  user_id: string;
  email: string;
  role: string;
  position_id: string | null;
  position_title: string | null;
  job_family: JobFamily | null;
  reports_to_id: string | null;
  departments: string[];
}

export interface OrgHistoryEntry {
  position_title: string | null;
  reports_to_email: string | null;
  started_at: string;
  ended_at: string | null;
}
