// Thin API client. Authentication is an HttpOnly session cookie set by the backend:
// this code never sees, stores or persists any token or provider credential.
import type {
  ApprovalChainStep,
  Webhook,
  WebhookDelivery,
  RequestAttachment,
  RequestForm,
  RequestFormField,
  ApprovalDelegation,
  Budget,
  ApprovalLimit,
  AuditEntry,
  JobFamily,
  LibraryPosition,
  OrgChartPerson,
  OrgHistoryEntry,
  Position,
  Claim,
  ClaimDetail,
  ClaimKind,
  ClaimList,
  ChatTurn,
  KnowledgeCategory,
  KnowledgeDetail,
  KnowledgeList,
  KnowledgeVisibility,
  ConversationDetail,
  ConversationMessage,
  ConversationSummary,
  CreateCustomProviderInput,
  MemoryKind,
  MemoryList,
  MemorySaveResult,
  ModelOption,
  ProviderList,
  ProviderMetadata,
  ProviderState,
  RagAnswer,
  RagDocument,
  RagDocumentList,
  ReviewQueueResponse,
  DocumentVersionInfo,
  DocumentVersionDiff,
  DocumentObligation,
  DocumentObligationList,
  FinancialExtraction,
  FinancialFormReviewQueueItem,
  DatabaseAnalysisCatalog,
  DatabaseAnalysisQuery,
  DatabaseAnalysisResult,
  CloudConnector,
  CloudConnectorCreateInput,
  CloudConnectorSyncResult,
  SendMessageResult,
  TraceDetail,
  TraceRow,
  TraceSummary,
  Settings,
  TestConnectionResult,
  EmbeddingModelStatus,
  EmbeddingModelTestResult,
  WorkspaceSummaryReport,
  DocumentParserInfo,
  DocumentParserTestResult,
  ParserFormat,
  OcrEngineStatus,
  OcrEngineTestResult,
  User,
  AgentStudioAgent,
  AgentStudioAgentInput,
  AgentStudioMemory,
  AgentStudioWhatsAppConnection,
  AgentStudioWhatsAppInput,
  AgentStudioRun,
  AgentStudioAnalytics,
  AgentStudioThread,
  AgentStudioTool,
  VectorStoreStatus,
  FolderImportProgress,
  FolderSyncSource,
  FolderSyncRun,
  FolderSyncNotification,
  DepartmentGroup,
  ManagedUser,
  UserInvitation,
  CsvInvitationPreview,
  RoleTemplate,
  RoleTemplateChangeRequest,
  RoleTemplatePermission,
  RoleTemplateAssignment,
  ResearchReport,
  ServiceRequestCategory,
  ServiceRequestItem,
  ServiceRequestNotification,
  ServiceRequestPriority,
  ServiceRequestStatus,
  TrainingPlan,
  TrainingPracticeHistoryItem,
  TrainingPracticeResult,
  TrainingPracticeScenario,
  TrainingProgressListResponse,
  TrainingProgressItem,
  TrainingQuizResult,
  TrainingQuizAttemptListResponse,
  TrainingAssignment,
  TrainingReminder,
  MarketResearchReport,
  MarketResearchStatus,
  GovernanceClaimList,
  GovernanceDecision,
  GovernanceKnowledgeList,
  KnowledgeFreshnessHistory,
  StaleKnowledgeList,
  KnowledgeConflictList,
  PolicyCandidate,
  PolicyException,
  PolicyProcess,
  PolicyProcessAssignee,
  EmployeeHandoff,
  HandoffList,
  InterviewOptions,
  InterviewQuestion,
  InterviewSession,
  InterviewTemplate,
  Meeting,
  MeetingActionTask,
  MeetingDetail,
  MeetingReview,
  MeetingSource,
  MeetingTranscript,
  UsageReport,
  PermissionGrant,
  PermissionCatalogEntry,
  CompanyProfile,
  DeploymentOverview,
  IndustryTemplate,
  IndustryTemplateApply,
  LicenseStatus,
  SignedLicense,
  MediaTranscriptionStatus,
  WorkflowDefinition,
  WorkflowDryRun,
  WorkflowRun,
  WorkflowTask,
  PersonalTask,
  PersonalTaskOptions,
  PersonalTaskReminder,
  WorkflowNotification,
  AgentProfile,
  AgentRunResponse,
  AgentRunRecord,
  AgentRunRetentionPreview,
  AgentRunRetentionPurgeResult,
  AgentDepartment,
  AgentCatalogDefinition,
  AgentCatalogList,
  AgentCatalogManageList,
  AgentCatalogGrant,
  AgentCatalogDraftCreated,
  AgentCatalogVersionLifecycle,
  AgentCatalogRevocation,
  AgentType,
  AgentTool,
  AgentKnowledgeScope,
  ExpertiseProfile,
  ExpertiseSearchResponse,
  ForecastScenario,
  ForecastScenarioSummary,
  ForecastPeriodUnit,
  ProposalCoverageCase,
  ProposalCoverageOptions,
  ProposalCoverageSummary,
  ProposalRequirementExtraction,
  PartnerIdentity,
  PartnerAnswer,
  PartnerWorkspace,
  PartnerInvitation,
  PartnerAdminIdentity,
  PartnerSource,
  LegacyBrowserCapture,
  LegacyBrowserRun,
  LegacyBrowserStatus,
  OrgSettings,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown,
    public requestId?: string,
  ) {
    super(message);
  }
}

let unauthorizedHandler: () => void = () => {};
export function onUnauthorized(handler: () => void) {
  unauthorizedHandler = handler;
}

export async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  signal?: AbortSignal,
  extraHeaders?: Record<string, string>,
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    // Required by the backend's CSRF check on state-changing requests.
    "X-Requested-With": path.startsWith("/api/partner/") ? "InfraSpinePartner" : "InfraSpineAdmin",
  };
  const isForm = body instanceof FormData; // the browser sets the multipart boundary itself
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";
  if (extraHeaders) Object.assign(headers, extraHeaders);

  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    cache: "no-store",
    body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
    signal,
  });
  if (response.status === 204) return undefined as T;

  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const error = data?.error;
    if (response.status === 401 && path !== "/api/auth/login" && !path.startsWith("/api/partner/")) unauthorizedHandler();
    throw new ApiError(
      response.status,
      error?.code ?? "http_error",
      error?.message ?? fallbackMessage(response.status),
      error?.details,
      error?.request_id,
    );
  }
  return data as T;
}

async function streamSendMessage(
  id: string,
  question: string,
  choice: { provider: string; model: string } | undefined,
  documentIds: string[] | undefined,
  onDelta: (delta: string) => void,
): Promise<SendMessageResult> {
  const path = `/api/conversations/${encodeURIComponent(id)}/messages/stream`;
  const response = await fetch(path, {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      "X-Requested-With": "InfraSpineAdmin",
    },
    credentials: "same-origin",
    cache: "no-store",
    body: JSON.stringify({ question, ...choice, ...(documentIds?.length ? { document_ids: documentIds } : {}) }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const error = body?.error;
    if (response.status === 401) unauthorizedHandler();
    throw new ApiError(response.status, error?.code ?? "http_error", error?.message ?? fallbackMessage(response.status), error?.details);
  }
  if (!response.body) throw new ApiError(502, "stream_unavailable", "The server did not open a response stream.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: SendMessageResult | undefined;
  const dispatch = (block: string) => {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    const data = JSON.parse(dataLines.join("\n"));
    if (event === "delta") onDelta(String(data.delta ?? ""));
    if (event === "done") result = data as SendMessageResult;
    if (event === "error") throw new ApiError(Number(data.status ?? 500), String(data.code ?? "stream_error"), String(data.message ?? "The assistant could not complete this reply."));
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() ?? "";
      for (const block of blocks) dispatch(block);
    }
    buffer += decoder.decode();
    if (buffer.trim()) dispatch(buffer);
  } finally {
    reader.releaseLock();
  }
  if (!result) throw new ApiError(502, "stream_incomplete", "The assistant response stream ended before completion.");
  return result;
}

async function download(path: string): Promise<Blob> {
  const response = await fetch(path, {
    method: "GET",
    headers: { Accept: "application/octet-stream", "X-Requested-With": "InfraSpineAdmin" },
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const error = data?.error;
    if (response.status === 401) unauthorizedHandler();
    throw new ApiError(
      response.status,
      error?.code ?? "http_error",
      error?.message ?? fallbackMessage(response.status),
      error?.details,
      error?.request_id,
    );
  }
  return response.blob();
}

async function downloadPost(path: string, body: unknown): Promise<Blob> {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      Accept: "text/csv",
      "Content-Type": "application/json",
      "X-Requested-With": "InfraSpineAdmin",
    },
    credentials: "same-origin",
    cache: "no-store",
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const error = data?.error;
    if (response.status === 401) unauthorizedHandler();
    throw new ApiError(
      response.status,
      error?.code ?? "http_error",
      error?.message ?? fallbackMessage(response.status),
      error?.details,
      error?.request_id,
    );
  }
  return response.blob();
}

/** Human-readable message, including field-level validation details when present. */
/** Message for a response without an API error body, e.g. when the UI server cannot reach the API. */
function fallbackMessage(status: number): string {
  if (status === 502 || status === 503 || status === 504) {
    return "The server could not be reached; it may be restarting. Try again in a few seconds.";
  }
  return `Request failed (${status})`;
}

export function describeError(error: unknown): string {
  if (!(error instanceof ApiError)) return "Network error. Check that the server is running.";
  const details = error.details as
    | { errors?: string[] }
    | Array<{ loc: (string | number)[]; msg: string }>
    | undefined;
  if (Array.isArray(details) && details.length) {
    return `${error.message} ${details.map((d) => `${d.loc.at(-1)}: ${d.msg}`).join("; ")}`;
  }
  if (details && !Array.isArray(details) && details.errors?.length) {
    return `${error.message} ${details.errors.join(" ")}`;
  }
  // An operator finds the traceback by this reference in the server log and the admin Traces page.
  if (error.requestId) return `${error.message} Ref ${error.requestId.slice(0, 8)}`;
  return error.message;
}

const provider = (id: string) => `/api/admin/providers/${encodeURIComponent(id)}`;

type RequestFormInput = {
  name: string; description: string | null; category: ServiceRequestCategory; department_group_id: string | null; fields: RequestFormField[];
  audience_department_ids?: string[]; audience_position_ids?: string[];
};

export const api = {
  me: () => request<User>("GET", "/api/auth/me"),
  login: (email: string, password: string) =>
    request<User | { mfa_required: true; challenge_token: string }>("POST", "/api/auth/login", { email, password }),
  verifyMfaLogin: (challenge_token: string, code: string) =>
    request<User>("POST", "/api/auth/mfa/challenge", { challenge_token, code }),
  mfaStatus: () => request<{ enabled: boolean; recovery_codes_remaining: number; required_by_organization?: boolean }>("GET", "/api/auth/mfa/status"),
  beginMfaEnrollment: (password: string) =>
    request<{ secret: string; provisioning_uri: string }>("POST", "/api/auth/mfa/enrollment", { password }),
  confirmMfaEnrollment: (code: string) =>
    request<{ recovery_codes: string[] }>("POST", "/api/auth/mfa/enrollment/confirm", { code }),
  rotateMfaRecoveryCodes: (password: string, code: string) =>
    request<{ recovery_codes: string[] }>("POST", "/api/auth/mfa/recovery-codes", { password, code }),
  disableMfa: (password: string, code: string) =>
    request<void>("POST", "/api/auth/mfa/disable", { password, code }),
  logout: () => request<void>("POST", "/api/auth/logout"),
  acceptInvitation: (token: string, password: string) =>
    request<{ email: string; role: string; message: string }>("POST", "/api/onboarding/accept", { token, password }),
  partnerMe: () => request<PartnerIdentity>("GET", "/api/partner/auth/me"),
  partnerLogin: (workspace_slug: string, email: string, password: string) =>
    request<PartnerIdentity>("POST", "/api/partner/auth/login", { workspace_slug, email, password }),
  acceptPartnerInvitation: (token: string, password: string) =>
    request<PartnerIdentity>("POST", "/api/partner/auth/accept", { token, password }),
  partnerLogout: () => request<void>("POST", "/api/partner/auth/logout"),
  askPartnerAssistant: (question: string) =>
    request<PartnerAnswer>("POST", "/api/partner/assistant/ask", { question }),
  partnerWorkspaces: () => request<PartnerWorkspace[]>("GET", "/api/admin/partner-workspaces"),
  createPartnerWorkspace: (input: { slug: string; name: string }) =>
    request<PartnerWorkspace>("POST", "/api/admin/partner-workspaces", input),
  setPartnerWorkspaceStatus: (id: string, status: PartnerWorkspace["status"]) =>
    request<PartnerWorkspace>("PATCH", `/api/admin/partner-workspaces/${encodeURIComponent(id)}/status`, { status }),
  partnerInvitations: (id: string) =>
    request<PartnerInvitation[]>("GET", `/api/admin/partner-workspaces/${encodeURIComponent(id)}/invitations`),
  createPartnerInvitation: (id: string, email: string) =>
    request<PartnerInvitation>("POST", `/api/admin/partner-workspaces/${encodeURIComponent(id)}/invitations`, { email }),
  revokePartnerInvitation: (workspaceId: string, invitationId: string) =>
    request<void>("DELETE", `/api/admin/partner-workspaces/${encodeURIComponent(workspaceId)}/invitations/${encodeURIComponent(invitationId)}`),
  partnerIdentities: (id: string) =>
    request<PartnerAdminIdentity[]>("GET", `/api/admin/partner-workspaces/${encodeURIComponent(id)}/identities`),
  setPartnerIdentityActive: (workspaceId: string, identityId: string, active: boolean) =>
    request<PartnerAdminIdentity>("PATCH", `/api/admin/partner-workspaces/${encodeURIComponent(workspaceId)}/identities/${encodeURIComponent(identityId)}/active`, { active }),
  partnerSources: (id: string) =>
    request<PartnerSource[]>("GET", `/api/admin/partner-workspaces/${encodeURIComponent(id)}/sources`),
  createPartnerSource: (id: string, input: { title: string; content: string }) =>
    request<PartnerSource>("POST", `/api/admin/partner-workspaces/${encodeURIComponent(id)}/sources`, input),
  uploadPartnerSource: (id: string, file: File, title?: string) => {
    const form = new FormData();
    form.append("file", file);
    if (title?.trim()) form.append("title", title.trim());
    return request<PartnerSource>("POST", `/api/admin/partner-workspaces/${encodeURIComponent(id)}/sources/upload`, form);
  },
  archivePartnerSource: (workspaceId: string, sourceId: string) =>
    request<void>("DELETE", `/api/admin/partner-workspaces/${encodeURIComponent(workspaceId)}/sources/${encodeURIComponent(sourceId)}`),
  retryPartnerSource: (workspaceId: string, sourceId: string) =>
    request<PartnerSource>("POST", `/api/admin/partner-workspaces/${encodeURIComponent(workspaceId)}/sources/${encodeURIComponent(sourceId)}/retry`),

  catalog: () =>
    request<{ providers: ProviderMetadata[] }>("GET", "/api/admin/providers/catalog"),
  providers: () => request<ProviderList>("GET", "/api/admin/providers"),
  createCustomProvider: (input: CreateCustomProviderInput) =>
    request<ProviderState>("POST", "/api/admin/providers/custom", input),
  updateProvider: (
    id: string,
    model: string,
    settings: Settings,
    expectedVersion: number,
    baseUrl?: string,
    apiKey?: string,
  ) =>
    request<ProviderState>("PUT", provider(id), {
      model,
      settings,
      expected_version: expectedVersion,
      ...(baseUrl !== undefined ? { base_url: baseUrl } : {}),
      // Required with a new API URL: saved keys are bound to the URL they were verified with.
      ...(apiKey !== undefined ? { api_key: apiKey } : {}),
    }),
  testConnection: (id: string, apiKey: string | null, model: string, baseUrl?: string) =>
    request<TestConnectionResult>(
      "POST",
      `${provider(id)}/test`,
      apiKey
        ? { api_key: apiKey, model, ...(baseUrl !== undefined ? { base_url: baseUrl } : {}) }
        : { model, ...(baseUrl !== undefined ? { base_url: baseUrl } : {}) },
    ),
  stageCredential: (id: string, apiKey: string, expectedVersion: number) =>
    request<ProviderState>("POST", `${provider(id)}/credentials`, {
      api_key: apiKey,
      expected_version: expectedVersion,
    }),
  activate: (id: string, expectedVersion: number) =>
    request<ProviderState>("POST", `${provider(id)}/activate`, { expected_version: expectedVersion }),
  deactivate: (id: string, expectedVersion: number) =>
    request<ProviderState>("POST", `${provider(id)}/deactivate`, { expected_version: expectedVersion }),
  setImageSharing: (id: string, authorized: boolean, expectedVersion: number) =>
    request<ProviderState>("PUT", `${provider(id)}/image-sharing`, { authorized, expected_version: expectedVersion }),
  removeProvider: (id: string, expectedVersion: number) =>
    request<void>("DELETE", `${provider(id)}?expected_version=${expectedVersion}`),
  embeddingModelStatus: () => request<EmbeddingModelStatus>("GET", "/api/admin/embedding-models"),
  testEmbeddingModel: () => request<EmbeddingModelTestResult>("POST", "/api/admin/embedding-models/test"),
  documentParsers: () => request<DocumentParserInfo>("GET", "/api/admin/document-parsers"),
  testDocumentParser: (format: ParserFormat) =>
    request<DocumentParserTestResult>("POST", `/api/admin/document-parsers/${encodeURIComponent(format)}/test`),
  ocrEngineStatus: () => request<OcrEngineStatus>("GET", "/api/admin/ocr-engine"),
  orgSettings: () => request<OrgSettings>("GET", "/api/admin/platform/org-settings"),
  orgTimeZones: () => request<{ time_zones: string[] }>("GET", "/api/admin/platform/time-zones"),
  saveOrgTimeZone: (time_zone: string) => request<OrgSettings>("PUT", "/api/admin/platform/org-settings", { time_zone }),
  testOcrEngine: () => request<OcrEngineTestResult>("POST", "/api/admin/ocr-engine/test"),
  documents: () => request<RagDocumentList>("GET", "/api/rag/documents"),
  documentReviewQueue: () => request<ReviewQueueResponse>("GET", "/api/rag/documents/review-queue"),
  setDocumentFreshness: (id: string, input: { review_date?: string | null; expires_at?: string | null; superseded_by?: string | null }) =>
    request<RagDocument>("PATCH", `/api/rag/documents/${encodeURIComponent(id)}/freshness`, input),
  downloadDocumentFile: (documentId: string) => download(`/api/rag/documents/${encodeURIComponent(documentId)}/file`),
  documentVersions: (documentId: string) =>
    request<DocumentVersionInfo[]>("GET", `/api/rag/documents/${encodeURIComponent(documentId)}/versions`),
  extractFinancialFields: (documentId: string, profile: "auto" | "invoice" | "purchase_order" | "form") =>
    request<FinancialExtraction>(
      "POST",
      `/api/rag/documents/${encodeURIComponent(documentId)}/financial-extraction`,
      { profile, date_order: "dmy" },
    ),
  reviewFinancialFields: (documentId: string, input: {
    expected_document_version: number;
    profile: FinancialExtraction["profile"];
    date_order: FinancialExtraction["date_order"];
    fields: Record<string, string | null>;
    line_items: Array<Omit<FinancialExtraction["line_items"][number], "source">>;
    reviewed_exception_indexes: number[];
    rationale: string;
  }) => request<FinancialExtraction>(
    "POST", `/api/rag/documents/${encodeURIComponent(documentId)}/financial-extraction/review`, input,
  ),
  financialReviewQueue: () =>
    request<{ records: FinancialFormReviewQueueItem[] }>("GET", "/api/rag/financial-extractions/review-queue"),
  exportReviewedFinancialRecords: (documentId?: string) => download(
    `/api/rag/financial-extractions/bookkeeping.csv${documentId ? `?document_id=${encodeURIComponent(documentId)}` : ""}`,
  ),
  databaseAnalysisCatalog: () =>
    request<DatabaseAnalysisCatalog>("GET", "/api/rag/database-analysis/catalog"),
  runDatabaseAnalysis: (input: DatabaseAnalysisQuery) =>
    request<DatabaseAnalysisResult>("POST", "/api/rag/database-analysis/query", input),
  cloudConnectors: () => request<CloudConnector[]>("GET", "/api/connectors"),
  createCloudConnector: (input: CloudConnectorCreateInput) =>
    request<CloudConnector>("POST", "/api/connectors", input),
  syncCloudConnector: (id: string) =>
    request<CloudConnectorSyncResult>("POST", `/api/connectors/${encodeURIComponent(id)}/sync`),
  deleteCloudConnector: (id: string) =>
    request<void>("DELETE", `/api/connectors/${encodeURIComponent(id)}`),
  exportDatabaseAnalysis: (input: DatabaseAnalysisQuery) =>
    downloadPost("/api/rag/database-analysis/export", input),
  compareDocumentVersions: (documentId: string, fromVersion: number, toVersion: number) =>
    request<DocumentVersionDiff>(
      "GET",
      `/api/rag/documents/${encodeURIComponent(documentId)}/compare?from_version=${fromVersion}&to_version=${toVersion}`,
    ),
  documentObligations: (documentId: string) =>
    request<DocumentObligationList>("GET", `/api/rag/documents/${encodeURIComponent(documentId)}/obligations`),
  createDocumentObligation: (documentId: string, input: {
    title: string;
    rationale: string;
    source_quote: string;
    assigned_to_email?: string;
    due_at?: string;
  }) => request<DocumentObligation>(
    "POST", `/api/rag/documents/${encodeURIComponent(documentId)}/obligations`, input,
  ),
  updateDocumentObligation: (obligationId: string, input: {
    expected_version: number;
    status?: "open" | "acknowledged" | "completed";
    assigned_to_email?: string | null;
    due_at?: string | null;
    rationale: string;
  }) => request<DocumentObligation>(
    "PATCH", `/api/rag/document-obligations/${encodeURIComponent(obligationId)}`, input,
  ),
  forecastScenarios: () => request<{ items: ForecastScenarioSummary[] }>("GET", "/api/forecasts"),
  createForecastScenario: (input: {
    title: string;
    metric: string;
    unit: string;
    period_unit: ForecastPeriodUnit;
    observations: Array<{ period: string; value: string; document_id: string; excerpt: string }>;
    growth_rate_pct: string;
    additive_adjustment: string;
    horizon_periods: number;
    assumption_notes: string;
  }) => request<ForecastScenario>("POST", "/api/forecasts", input),
  forecastScenario: (id: string) => request<ForecastScenario>("GET", `/api/forecasts/${encodeURIComponent(id)}`),
  uploadDocument: (file: File, visibility: "private" | "shared" | "restricted", groupId?: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("visibility", visibility);
    if (groupId) form.append("department_id", groupId);
    return request<RagDocument>("POST", "/api/rag/documents", form);
  },
  replaceDocument: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<RagDocument>("PUT", `/api/rag/documents/${encodeURIComponent(id)}/file`, form);
  },
  reprocessDocument: (id: string) =>
    request<RagDocument>("POST", `/api/rag/documents/${encodeURIComponent(id)}/reprocess`),
  cancelProcessing: (id: string) =>
    request<RagDocument>("POST", `/api/rag/documents/${encodeURIComponent(id)}/cancel`),
  deleteDocument: (id: string) => request<void>("DELETE", `/api/rag/documents/${encodeURIComponent(id)}`),
  askDocuments: (question: string, history: ChatTurn[], focusDocumentIds: string[]) =>
    request<RagAnswer>("POST", "/api/rag/chat", {
      question,
      history,
      focus_document_ids: focusDocumentIds.length ? focusDocumentIds : null,
    }),
  askSpokenQuestion: (
    file: File,
    focusDocumentId?: string,
    choice?: { provider: string; model: string },
  ) => {
    const form = new FormData();
    form.append("audio", file);
    if (focusDocumentId) form.append("focus_document_id", focusDocumentId);
    if (choice) {
      form.append("provider", choice.provider);
      form.append("model", choice.model);
    }
    return request<{ transcript: string; answer: RagAnswer }>("POST", "/api/rag/voice-question", form);
  },
  researchDocuments: (question: string, documentIds?: string[]) =>
    request<ResearchReport>("POST", "/api/rag/research", {
      question,
      ...(documentIds?.length ? { document_ids: documentIds } : {}),
    }),
  legacyBrowserStatus: () => request<LegacyBrowserStatus>("GET", "/api/legacy-browser/status"),
  legacyBrowserRuns: () => request<{ items: LegacyBrowserRun[] }>("GET", "/api/legacy-browser/runs"),
  requestLegacyBrowserRun: (input: { label: string; reason: string; start_url: string; link_labels: string[] }) =>
    request<LegacyBrowserRun>("POST", "/api/legacy-browser/runs", input),
  approveLegacyBrowserRun: (runId: string) =>
    request<LegacyBrowserCapture>("POST", `/api/legacy-browser/runs/${encodeURIComponent(runId)}/approve`),
  rejectLegacyBrowserRun: (runId: string) =>
    request<LegacyBrowserRun>("POST", `/api/legacy-browser/runs/${encodeURIComponent(runId)}/reject`),
  cancelLegacyBrowserRun: (runId: string) =>
    request<LegacyBrowserRun>("POST", `/api/legacy-browser/runs/${encodeURIComponent(runId)}/cancel`),
  trainingDocuments: (topic: string, documentIds?: string[]) =>
    request<TrainingPlan>("POST", "/api/rag/training", {
      topic,
      ...(documentIds?.length ? { document_ids: documentIds } : {}),
    }),
  trainingHistory: () => request<TrainingProgressListResponse>("GET", "/api/rag/training"),
  trainingPlan: (trainingId: string) => request<TrainingPlan>("GET", `/api/rag/training/${trainingId}`),
  completeTraining: (trainingId: string) =>
    request<TrainingProgressItem>("POST", `/api/rag/training/${trainingId}/complete`),
  submitTrainingQuiz: (trainingId: string, answers: number[]) =>
    request<TrainingQuizResult>("POST", `/api/rag/training/${trainingId}/quiz-attempts`, { answers }),
  trainingQuizAttempts: (trainingId: string) =>
    request<TrainingQuizAttemptListResponse>("GET", `/api/rag/training/${trainingId}/quiz-attempts`),
  trainingPracticeHistory: (trainingId: string) =>
    request<{ scenarios: TrainingPracticeHistoryItem[] }>("GET", `/api/rag/training/${encodeURIComponent(trainingId)}/adaptive-practice`),
  createTrainingPractice: (trainingId: string) =>
    request<TrainingPracticeScenario>("POST", `/api/rag/training/${encodeURIComponent(trainingId)}/adaptive-practice`, {}),
  submitTrainingPractice: (trainingId: string, scenarioId: string, answers: number[]) =>
    request<TrainingPracticeResult>(
      "POST", `/api/rag/training/${encodeURIComponent(trainingId)}/adaptive-practice/${encodeURIComponent(scenarioId)}/attempts`, { answers },
    ),
  trainingReminders: () => request<{ reminders: TrainingReminder[] }>("GET", "/api/rag/training/reminders"),
  readTrainingReminder: (reminderId: string) =>
    request<TrainingReminder>("POST", `/api/rag/training/reminders/${encodeURIComponent(reminderId)}/read`),
  assignTraining: (trainingId: string, departmentId: string, userIds: string[], dueAt?: string) =>
    request<{ created: number; assignments: TrainingAssignment[] }>(
      "POST",
      `/api/rag/training/${trainingId}/assign`,
      { department_id: departmentId, user_ids: userIds, ...(dueAt ? { due_at: dueAt } : {}) },
    ),
  trainingAssignments: (departmentId: string) =>
    request<{ assignments: TrainingAssignment[] }>("GET", `/api/rag/training-assignments?department_id=${encodeURIComponent(departmentId)}`),
  trainingAssignmentCandidates: (departmentId: string) =>
    request<{ employees: Array<{ user_id: string; email: string }> }>("GET", `/api/rag/training-assignment-candidates?department_id=${encodeURIComponent(departmentId)}`),
  marketResearchStatus: () => request<MarketResearchStatus>("GET", "/api/rag/market-research/status"),
  marketResearch: (topic: string) =>
    request<MarketResearchReport>("POST", "/api/rag/market-research", {
      topic,
      confirm_external_search: true,
    }),

  conversations: () => request<{ conversations: ConversationSummary[] }>("GET", "/api/conversations"),
  createConversation: () => request<ConversationSummary>("POST", "/api/conversations", {}),
  getConversation: (id: string) => request<ConversationDetail>("GET", `/api/conversations/${encodeURIComponent(id)}`),
  renameConversation: (id: string, title: string) =>
    request<ConversationSummary>("PATCH", `/api/conversations/${encodeURIComponent(id)}`, { title }),
  deleteConversation: (id: string) => request<void>("DELETE", `/api/conversations/${encodeURIComponent(id)}`),
  sendMessage: (id: string, question: string, choice?: { provider: string; model: string }, documentIds?: string[]) =>
    request<SendMessageResult>("POST", `/api/conversations/${encodeURIComponent(id)}/messages`, {
      question,
      ...choice,
      ...(documentIds?.length ? { document_ids: documentIds } : {}),
    }),
  streamMessage: (
    id: string,
    question: string,
    choice: { provider: string; model: string } | undefined,
    documentIds: string[] | undefined,
    onDelta: (delta: string) => void,
  ) => streamSendMessage(id, question, choice, documentIds, onDelta),
  rateMessage: (conversationId: string, messageId: number, rating: -1 | 1 | null) =>
    request<ConversationMessage>(
      "PUT",
      `/api/conversations/${encodeURIComponent(conversationId)}/messages/${messageId}/feedback`,
      { rating },
    ),
  chatModels: () => request<{ options: ModelOption[] }>("GET", "/api/rag/models"),

  claims: () => request<ClaimList>("GET", "/api/claims"),
  getClaim: (id: string) => request<ClaimDetail>("GET", `/api/claims/${encodeURIComponent(id)}`),
  createClaim: (input: {
    kind: ClaimKind;
    statement: string;
    category: string;
    sources?: Array<{ kind: "document"; document_id: string } | { kind: "url"; url: string } | { kind: "note"; note: string }>;
  }) => request<Claim>("POST", "/api/claims", input),
  withdrawClaim: (id: string) => request<Claim>("POST", `/api/claims/${encodeURIComponent(id)}/withdraw`),
  governanceQueue: () => request<GovernanceClaimList>("GET", "/api/governance/claims"),
  decideClaim: (id: string, stage: "review" | "publish", rationale: string) =>
    request<GovernanceDecision>("POST", `/api/governance/claims/${encodeURIComponent(id)}/decisions`, { stage, rationale }),
  governanceKnowledgeQueue: () => request<GovernanceKnowledgeList>("GET", "/api/governance/knowledge"),
  decideKnowledgeRevision: (id: string, stage: "review" | "publish", rationale: string) =>
    request<GovernanceDecision>("POST", `/api/governance/knowledge/${encodeURIComponent(id)}/decisions`, { stage, rationale }),
  staleKnowledgeQueue: () => request<StaleKnowledgeList>("GET", "/api/governance/knowledge/stale"),
  reviewKnowledgeFreshness: (id: string, rationale: string) =>
    request<KnowledgeFreshnessHistory & { revision_id: string }>("POST", `/api/governance/knowledge/${encodeURIComponent(id)}/freshness-review`, { rationale }),
  knowledgeConflictQueue: (cursor?: string) => request<KnowledgeConflictList>(
    "GET", `/api/governance/knowledge/conflicts${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
  ),
  decideKnowledgeConflict: (id: string, decision: "confirmed" | "dismissed", rationale: string) =>
    request<{ conflict_id: string; status: "confirmed" | "dismissed"; reviewer_email: string; rationale: string; decided_at: string }>(
      "POST", `/api/governance/knowledge/conflicts/${encodeURIComponent(id)}/decisions`, { decision, rationale },
    ),
  policyProcessCandidates: () => request<{ items: PolicyCandidate[] }>("GET", "/api/governance/policy-candidates"),
  policyProcesses: (cursor?: string) => request<{ items: PolicyProcess[]; next_cursor: string | null }>("GET", `/api/governance/processes${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`),
  createPolicyProcess: (input: { name: string; description: string; policy_entry_id: string }) =>
    request<PolicyProcess>("POST", "/api/governance/processes", input),
  linkPolicyToProcess: (processId: string, entryId: string) =>
    request<PolicyProcess>("POST", `/api/governance/processes/${encodeURIComponent(processId)}/policies`, { entry_id: entryId }),
  unlinkPolicyFromProcess: (processId: string, entryId: string) =>
    request<PolicyProcess>("DELETE", `/api/governance/processes/${encodeURIComponent(processId)}/policies/${encodeURIComponent(entryId)}`),
  policyProcessAssignees: (processId: string, entryId: string) =>
    request<{ items: PolicyProcessAssignee[] }>("GET", `/api/governance/processes/${encodeURIComponent(processId)}/assignees?entry_id=${encodeURIComponent(entryId)}`),
  createPolicyException: (processId: string, input: { entry_id: string; assigned_to: string; rationale: string; due_at?: string }) =>
    request<PolicyException>("POST", `/api/governance/processes/${encodeURIComponent(processId)}/exceptions`, input),
  policyExceptions: () => request<{ items: PolicyException[] }>("GET", "/api/governance/policy-exceptions"),
  actOnPolicyException: (id: string, input: { action: "acknowledge" | "resolve" | "reassign"; rationale: string; assigned_to?: string }) =>
    request<PolicyException>("POST", `/api/governance/policy-exceptions/${encodeURIComponent(id)}/actions`, input),

  handoffs: () => request<HandoffList>("GET", "/api/handoffs"),
  createHandoff: (input: { department_id: string; title: string; body: string }) =>
    request<EmployeeHandoff>("POST", "/api/handoffs", input),
  acknowledgeHandoff: (id: string) =>
    request<EmployeeHandoff>("POST", `/api/handoffs/${encodeURIComponent(id)}/acknowledge`),
  meetingSources: () => request<{ sources: MeetingSource[] }>("GET", "/api/meetings/sources"),
  meetings: () => request<{ meetings: Meeting[] }>("GET", "/api/meetings"),
  meeting: (id: string) => request<MeetingDetail>("GET", `/api/meetings/${encodeURIComponent(id)}`),
  createMeeting: (input: { source_document_id: string; title: string }) =>
    request<MeetingDetail>("POST", "/api/meetings", input),
  meetingTranscript: (id: string, afterChunkId = 0) =>
    request<MeetingTranscript>("GET", `/api/meetings/${encodeURIComponent(id)}/transcript?after_chunk_id=${afterChunkId}&limit=100`),
  saveMeetingDraft: (id: string, input: {
    expected_version: number; minutes_markdown: string;
    decisions: Array<{ text: string; evidence_chunk_ids: number[] }>;
    proposed_tasks: Array<{ title: string; body: string; assignee_user_id: string; due_at: string | null; evidence_chunk_ids: number[] }>;
  }) => request<MeetingDetail>("PUT", `/api/meetings/${encodeURIComponent(id)}/draft`, input),
  generateMeetingDraft: (id: string, expectedVersion: number) =>
    request<MeetingDetail>("POST", `/api/meetings/${encodeURIComponent(id)}/generate-draft`, { expected_version: expectedVersion }),
  submitMeeting: (id: string, expectedVersion: number) =>
    request<MeetingDetail>("POST", `/api/meetings/${encodeURIComponent(id)}/submit`, { expected_version: expectedVersion }),
  reviewMeeting: (id: string, decision: MeetingReview["decision"], rationale: string) =>
    request<MeetingDetail>("POST", `/api/meetings/${encodeURIComponent(id)}/review`, { decision, rationale }),
  completeMeetingTask: (id: string) =>
    request<MeetingActionTask>("POST", `/api/meetings/tasks/${encodeURIComponent(id)}/complete`),
  serviceRequests: () => request<{ items: ServiceRequestItem[] }>("GET", "/api/service-requests"),
  serviceRequestNotifications: () =>
    request<{ items: ServiceRequestNotification[] }>("GET", "/api/service-requests/notifications"),
  readServiceRequestNotification: (id: string) =>
    request<ServiceRequestNotification>("POST", `/api/service-requests/notifications/${encodeURIComponent(id)}/read`),
  createServiceRequest: (input: {
    category: ServiceRequestCategory; priority: ServiceRequestPriority; department_group_id: string | null;
    subject: string; description: string; amount?: string | null; currency?: string | null;
    form_id?: string | null; answers?: Record<string, string | boolean | string[]>;
  }) => request<ServiceRequestItem>("POST", "/api/service-requests", input),
  uploadRequestAttachment: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<RequestAttachment>("POST", "/api/service-requests/attachments", form);
  },
  removeRequestAttachment: (id: string) => request<void>("DELETE", `/api/service-requests/attachments/${encodeURIComponent(id)}`),
  requestAttachmentUrl: (requestId: string, attachmentId: string) =>
    `/api/service-requests/${encodeURIComponent(requestId)}/attachments/${encodeURIComponent(attachmentId)}`,
  webhooks: () => request<{ webhooks: Webhook[]; events: string[] }>("GET", "/api/admin/webhooks"),
  createWebhook: (input: { name: string; url: string; events: string[] }) =>
    request<{ webhooks: Webhook[]; events: string[]; secret: string }>("POST", "/api/admin/webhooks", input),
  updateWebhook: (id: string, input: { name?: string; url?: string; events?: string[]; is_active?: boolean }) =>
    request<{ webhooks: Webhook[]; events: string[] }>("PATCH", `/api/admin/webhooks/${encodeURIComponent(id)}`, input),
  rotateWebhookSecret: (id: string) => request<{ webhooks: Webhook[]; events: string[]; secret: string }>("POST", `/api/admin/webhooks/${encodeURIComponent(id)}/rotate-secret`),
  testWebhook: (id: string) => request<{ webhooks: Webhook[]; events: string[] }>("POST", `/api/admin/webhooks/${encodeURIComponent(id)}/test`),
  deleteWebhook: (id: string) => request<{ webhooks: Webhook[]; events: string[] }>("DELETE", `/api/admin/webhooks/${encodeURIComponent(id)}`),
  webhookDeliveries: (id: string) => request<{ deliveries: WebhookDelivery[] }>("GET", `/api/admin/webhooks/${encodeURIComponent(id)}/deliveries`),
  redeliverWebhook: (deliveryId: string) => request<{ webhooks: Webhook[] }>("POST", `/api/admin/webhooks/deliveries/${encodeURIComponent(deliveryId)}/redeliver`),
  requestForms: () => request<{ forms: RequestForm[] }>("GET", "/api/service-requests/forms"),
  adminRequestForms: () => request<{ forms: RequestForm[] }>("GET", "/api/admin/request-forms"),
  createRequestForm: (input: RequestFormInput) => request<{ forms: RequestForm[] }>("POST", "/api/admin/request-forms", input),
  updateRequestForm: (id: string, input: RequestFormInput) => request<{ forms: RequestForm[] }>("PUT", `/api/admin/request-forms/${encodeURIComponent(id)}`, input),
  archiveRequestForm: (id: string) => request<{ forms: RequestForm[] }>("DELETE", `/api/admin/request-forms/${encodeURIComponent(id)}`),
  approvalChain: () => request<{ steps: ApprovalChainStep[] }>("GET", "/api/admin/approval-chain"),
  addApprovalChainStep: (input: { category: "purchase" | "expense"; label: string; approver_kind: "manager" | "position" | "person"; position_id?: string | null; user_id?: string | null; min_amount?: string | null; parallel_with_previous?: boolean; stage_mode?: "all" | "any" }) =>
    request<{ steps: ApprovalChainStep[] }>("POST", "/api/admin/approval-chain", input),
  updateApprovalChainStep: (id: string, input: { parallel_with_previous?: boolean; stage_mode?: "all" | "any" }) =>
    request<{ steps: ApprovalChainStep[] }>("PATCH", `/api/admin/approval-chain/${encodeURIComponent(id)}`, input),
  removeApprovalChainStep: (id: string) => request<{ steps: ApprovalChainStep[] }>("DELETE", `/api/admin/approval-chain/${encodeURIComponent(id)}`),
  moveApprovalChainStep: (id: string, direction: -1 | 1) =>
    request<{ steps: ApprovalChainStep[] }>("POST", `/api/admin/approval-chain/${encodeURIComponent(id)}/move`, { direction }),
  decideApprovalStep: (requestId: string, decision: "approve" | "reject", comment: string) =>
    request<ServiceRequestItem>("POST", `/api/service-requests/${encodeURIComponent(requestId)}/approval-decision`, { decision, comment }),
  approvalDelegations: () => request<{ delegations: ApprovalDelegation[] }>("GET", "/api/org/approval-delegations"),
  createApprovalDelegation: (input: { delegate_user_id: string; starts_on: string; ends_on: string; reason: string | null; delegator_user_id?: string }) =>
    request<{ delegations: ApprovalDelegation[] }>("POST", "/api/org/approval-delegations", input),
  endApprovalDelegation: (id: string) => request<{ delegations: ApprovalDelegation[] }>("POST", `/api/org/approval-delegations/${encodeURIComponent(id)}/end`),
  budgets: () => request<{ budgets: Budget[] }>("GET", "/api/org/budgets"),
  createBudget: (input: { name: string; department_group_id: string; category: Budget["category"]; period_start: string; period_end: string; amount: string; currency: string; enforcement: Budget["enforcement"] }) =>
    request<{ budgets: Budget[] }>("POST", "/api/org/budgets", input),
  updateBudget: (id: string, input: { amount?: string; enforcement?: Budget["enforcement"]; is_active?: boolean }) =>
    request<{ budgets: Budget[] }>("PATCH", `/api/org/budgets/${encodeURIComponent(id)}`, input),
  approvalLimits: () => request<{ limits: ApprovalLimit[] }>("GET", "/api/admin/approval-limits"),
  createApprovalLimit: (input: { category: "purchase" | "expense"; position_id?: string | null; user_id?: string | null; max_amount: string; currency: string }) =>
    request<ApprovalLimit>("POST", "/api/admin/approval-limits", input),
  updateApprovalLimit: (id: string, input: { max_amount?: string; currency?: string; is_active?: boolean }) =>
    request<{ limits: ApprovalLimit[] }>("PATCH", `/api/admin/approval-limits/${encodeURIComponent(id)}`, input),
  commentServiceRequest: (id: string, comment: string) =>
    request<ServiceRequestItem>("POST", `/api/service-requests/${encodeURIComponent(id)}/comments`, { comment }),
  triageServiceRequest: (id: string, input: {
    status: Exclude<ServiceRequestStatus, "submitted" | "cancelled">; assigned_to_user_id: string | null; comment: string | null;
  }) => request<ServiceRequestItem>("POST", `/api/service-requests/${encodeURIComponent(id)}/triage`, input),
  cancelServiceRequest: (id: string) =>
    request<ServiceRequestItem>("POST", `/api/service-requests/${encodeURIComponent(id)}/cancel`),
  agentProfiles: () => request<{ agents: AgentProfile[] }>("GET", "/api/agents"),
  agentStudioTools: () => request<{ tools: AgentStudioTool[] }>("GET", "/api/agent-studio/tools"),
  agentStudioAgents: () => request<{ agents: AgentStudioAgent[] }>("GET", "/api/agent-studio/agents"),
  agentStudioAnalytics: (days = 30) => request<AgentStudioAnalytics>("GET", `/api/agent-studio/analytics?days=${days}`),
  createAgentStudioAgent: (input: AgentStudioAgentInput) =>
    request<AgentStudioAgent>("POST", "/api/agent-studio/agents", input),
  updateAgentStudioAgent: (id: string, input: AgentStudioAgentInput & { expected_version: number }) =>
    request<AgentStudioAgent>("PUT", "/api/agent-studio/agents/" + encodeURIComponent(id), input),
  enableAgentStudioAgent: (id: string) =>
    request<AgentStudioAgent>("POST", "/api/agent-studio/agents/" + encodeURIComponent(id) + "/enable"),
  disableAgentStudioAgent: (id: string) =>
    request<AgentStudioAgent>("POST", "/api/agent-studio/agents/" + encodeURIComponent(id) + "/disable"),
  pauseAgentStudioAgent: (id: string) =>
    request<AgentStudioAgent>("POST", "/api/agent-studio/agents/" + encodeURIComponent(id) + "/pause"),
  resumeAgentStudioAgent: (id: string) =>
    request<AgentStudioAgent>("POST", "/api/agent-studio/agents/" + encodeURIComponent(id) + "/resume"),
  deleteAgentStudioAgent: (id: string) =>
    request<void>("DELETE", "/api/agent-studio/agents/" + encodeURIComponent(id)),
  createAgentStudioThread: (id: string, title = "New agent run") =>
    request<AgentStudioThread>("POST", "/api/agent-studio/agents/" + encodeURIComponent(id) + "/threads", { title }),
  agentStudioThreads: (id: string) =>
    request<{ threads: AgentStudioThread[] }>("GET", "/api/agent-studio/agents/" + encodeURIComponent(id) + "/threads"),
  agentStudioAgentRuns: (id: string) =>
    request<{ runs: AgentStudioRun[] }>("GET", "/api/agent-studio/agents/" + encodeURIComponent(id) + "/runs"),
  agentStudioThreadRuns: (id: string) =>
    request<{ runs: AgentStudioRun[] }>("GET", "/api/agent-studio/threads/" + encodeURIComponent(id) + "/runs"),
  runAgentStudio: (agentId: string, threadId: string, prompt: string, idempotencyKey: string, scheduledAt?: string) =>
    request<AgentStudioRun>(
      "POST",
      "/api/agent-studio/agents/" + encodeURIComponent(agentId) + "/threads/" + encodeURIComponent(threadId) + "/runs",
      { prompt, scheduled_at: scheduledAt },
      undefined,
      { "Idempotency-Key": idempotencyKey },
    ),
  cancelAgentStudioRun: (id: string) =>
    request<AgentStudioRun>("POST", "/api/agent-studio/runs/" + encodeURIComponent(id) + "/cancel"),
  decideAgentStudioRun: (id: string, decision: "approve" | "reject", note = "") =>
    request<AgentStudioRun>("POST", "/api/agent-studio/runs/" + encodeURIComponent(id) + "/decision", { decision, note }),
  agentStudioMemories: (id: string) =>
    request<{ memories: AgentStudioMemory[] }>("GET", "/api/agent-studio/agents/" + encodeURIComponent(id) + "/memories"),
  deleteAgentStudioMemory: (agentId: string, memoryId: string) =>
    request<void>("DELETE", "/api/agent-studio/agents/" + encodeURIComponent(agentId) + "/memories/" + encodeURIComponent(memoryId)),
  agentStudioWhatsApp: () => request<{ connection: AgentStudioWhatsAppConnection | null }>("GET", "/api/agent-studio/whatsapp"),
  agentStudioWhatsAppQr: () => request<{ available: boolean; connection: {
    id: string; agent_id: string; enabled: boolean; status: string; phone_suffix: string | null; qr: string | null; last_error: string | null;
    latest_command?: { id: string; status: string; receipt: "server_acknowledged" | "device_delivered" | "read" | null; received_at: string } | null;
  } | null }>("GET", "/api/agent-studio/whatsapp-qr"),
  connectAgentStudioWhatsAppQr: (agent_id: string) => request<{ started: boolean }>("POST", "/api/agent-studio/whatsapp-qr", { agent_id }),
  disconnectAgentStudioWhatsAppQr: () => request<void>("DELETE", "/api/agent-studio/whatsapp-qr"),
  cancelAgentStudioWhatsAppQrCommand: (id: string) => request<void>("POST", `/api/agent-studio/whatsapp-qr/commands/${id}/cancel`),
  saveAgentStudioWhatsApp: (input: AgentStudioWhatsAppInput) =>
    request<{ connection: AgentStudioWhatsAppConnection }>("PUT", "/api/agent-studio/whatsapp", input),
  verifyAgentStudioWhatsApp: () => request<{ verified_name: string | null; display_phone_number: string | null; subscribed: boolean }>("POST", "/api/agent-studio/whatsapp/verify"),
  disconnectAgentStudioWhatsApp: () => request<void>("DELETE", "/api/agent-studio/whatsapp"),
  agentDepartments: () => request<{ departments: AgentDepartment[] }>("GET", "/api/agents/departments"),
  createAgentProfile: (input: {
    name: string; department_group_id: string; agent_type: AgentType;
    knowledge_scope: AgentKnowledgeScope; tool_policy: AgentTool[];
  }) => request<AgentProfile>("POST", "/api/agents", input),
  versionAgentProfile: (id: string, input: {
    expected_version: number; name: string; agent_type: AgentType;
    knowledge_scope: AgentKnowledgeScope; tool_policy: AgentTool[];
  }) => request<AgentProfile>("PUT", `/api/agents/${encodeURIComponent(id)}`, input),
  disableAgentProfile: (id: string) =>
    request<AgentProfile>("POST", `/api/agents/${encodeURIComponent(id)}/disable`),
  runAgentProfile: (id: string, input: {
    capability: AgentTool; task: string; document_ids?: string[]; confirm_external_search?: boolean;
  }) => request<AgentRunResponse>("POST", `/api/agents/${encodeURIComponent(id)}/runs`, input),
  queueAgentRunProfile: (id: string, input: {
    capability: AgentTool; task: string; document_ids?: string[];
  }) => request<AgentRunRecord>("POST", `/api/agents/${encodeURIComponent(id)}/queue`, input),
  agentRun: (id: string) => request<AgentRunRecord>("GET", `/api/agents/runs/${encodeURIComponent(id)}`),
  agentProfileRuns: (id: string) =>
    request<{ runs: AgentRunRecord[] }>("GET", `/api/agents/${encodeURIComponent(id)}/runs`),
  retryAgentRun: (id: string) =>
    request<AgentRunResponse>("POST", `/api/agents/runs/${encodeURIComponent(id)}/retry`),
  previewAgentRunRetention: (retention_days: number) =>
    request<AgentRunRetentionPreview>("POST", "/api/admin/agent-runs/retention/preview", { retention_days }),
  purgeAgentRunRetention: (input: { retention_days: number; batch_size: number; confirm: true }) =>
    request<AgentRunRetentionPurgeResult>("POST", "/api/admin/agent-runs/retention/purge", input),
  agentCatalog: () => request<AgentCatalogList>("GET", "/api/agent-catalog"),
  managedAgentCatalog: () => request<AgentCatalogManageList>("GET", "/api/agent-catalog/manage"),
  createAgentCatalogTemplate: (input: AgentCatalogDefinition & { slug: string }) =>
    request<AgentCatalogDraftCreated>("POST", "/api/agent-catalog/templates", input),
  versionAgentCatalogTemplate: (id: string, input: AgentCatalogDefinition) =>
    request<AgentCatalogDraftCreated>("POST", `/api/agent-catalog/templates/${encodeURIComponent(id)}/versions`, input),
  submitAgentCatalogVersion: (id: string) =>
    request<AgentCatalogVersionLifecycle>("POST", `/api/agent-catalog/versions/${encodeURIComponent(id)}/submit`),
  reviewAgentCatalogVersion: (id: string, decision: "approve" | "reject", rationale: string) =>
    request<AgentCatalogVersionLifecycle>("POST", `/api/agent-catalog/versions/${encodeURIComponent(id)}/review`, { decision, rationale }),
  revokeAgentCatalogTemplate: (id: string, rationale: string) =>
    request<AgentCatalogRevocation>("POST", `/api/agent-catalog/templates/${encodeURIComponent(id)}/revoke`, { rationale }),
  agentCatalogGrants: () => request<{ items: AgentCatalogGrant[] }>("GET", "/api/agent-catalog/grants"),
  createAgentCatalogGrant: (email: string, permission: "publish" | "review") =>
    request<AgentCatalogGrant>("POST", "/api/agent-catalog/grants", { email, permission }),
  revokeAgentCatalogGrant: (id: string) =>
    request<AgentCatalogGrant>("POST", `/api/agent-catalog/grants/${encodeURIComponent(id)}/revoke`),
  automationWorkflows: () => request<{ workflows: WorkflowDefinition[] }>("GET", "/api/automation/workflows"),
  createAutomationWorkflow: (input: {
    name: string; department_group_id: string; trigger_type: "manual" | "interval" | "service_request" | "request_sla";
    interval_minutes: number | null; action_type: "create_task" | "notify_member" | "assign_personal_task";
    assignee_user_id?: string; task_title?: string; task_body?: string; due_days?: number | null;
    notification_recipient_user_id?: string; notification_title?: string; notification_body?: string;
  }) => request<WorkflowDefinition>("POST", "/api/automation/workflows", input),
  versionAutomationWorkflow: (id: string, input: {
    expected_version: number; name: string; trigger_type: "manual" | "interval" | "service_request" | "request_sla";
    interval_minutes: number | null; action_type: "create_task" | "notify_member" | "assign_personal_task";
    assignee_user_id?: string; task_title?: string; task_body?: string; due_days?: number | null;
    notification_recipient_user_id?: string; notification_title?: string; notification_body?: string;
  }) => request<WorkflowDefinition>("PUT", `/api/automation/workflows/${encodeURIComponent(id)}`, input),
  disableAutomationWorkflow: (id: string) =>
    request<void>("POST", `/api/automation/workflows/${encodeURIComponent(id)}/disable`),
  dryRunAutomationWorkflow: (id: string) =>
    request<WorkflowDryRun>("POST", `/api/automation/workflows/${encodeURIComponent(id)}/dry-run`, {}),
  requestAutomationRun: (id: string) =>
    request<WorkflowRun>("POST", `/api/automation/workflows/${encodeURIComponent(id)}/runs`, { idempotency_key: crypto.randomUUID() }),
  automationRuns: () => request<{ runs: WorkflowRun[] }>("GET", "/api/automation/runs"),
  decideAutomationRun: (id: string, approved: boolean, rationale: string) =>
    request<WorkflowRun>("POST", `/api/automation/runs/${encodeURIComponent(id)}/decision`, { approved, rationale }),
  cancelAutomationRun: (id: string) =>
    request<WorkflowRun>("POST", `/api/automation/runs/${encodeURIComponent(id)}/cancel`),
  retryAutomationRun: (id: string) =>
    request<WorkflowRun>("POST", `/api/automation/runs/${encodeURIComponent(id)}/retry`),
  automationTasks: () => request<{ tasks: WorkflowTask[] }>("GET", "/api/automation/tasks"),
  personalTaskOptions: () => request<PersonalTaskOptions>("GET", "/api/personal-tasks/options"),
  personalTasks: () => request<{ tasks: PersonalTask[] }>("GET", "/api/personal-tasks"),
  personalTaskReminders: () => request<{ reminders: PersonalTaskReminder[] }>("GET", "/api/personal-tasks/reminders"),
  readPersonalTaskReminder: (id: string) =>
    request<PersonalTaskReminder>("POST", `/api/personal-tasks/reminders/${encodeURIComponent(id)}/read`),
  createPersonalTask: (input: {
    title: string; body: string; assignee_user_id: string; department_group_id: string | null; due_at: string | null;
  }) => request<PersonalTask>("POST", "/api/personal-tasks", input),
  completePersonalTask: (id: string) =>
    request<PersonalTask>("POST", `/api/personal-tasks/${encodeURIComponent(id)}/complete`),
  cancelPersonalTask: (id: string) =>
    request<PersonalTask>("POST", `/api/personal-tasks/${encodeURIComponent(id)}/cancel`),
  automationNotifications: () =>
    request<{ items: WorkflowNotification[] }>("GET", "/api/automation/notifications"),
  readAutomationNotification: (id: string) =>
    request<WorkflowNotification>("POST", "/api/automation/notifications/" + encodeURIComponent(id) + "/read"),
  completeAutomationTask: (id: string) =>
    request<WorkflowTask>("POST", `/api/automation/tasks/${encodeURIComponent(id)}/complete`),
  managedDepartments: () => request<{ groups: DepartmentGroup[] }>("GET", "/api/departments/managed"),
  myDepartments: () => request<{ groups: DepartmentGroup[] }>("GET", "/api/departments/mine"),
  departmentMembers: (id: string) => request<{ members: Array<{ user_id: string; email: string }> }>("GET", `/api/departments/${encodeURIComponent(id)}/members`),
  invitableDepartments: () => request<{ groups: DepartmentGroup[] }>("GET", "/api/departments/invitable"),
  trainingDepartments: () => request<{ departments: AgentDepartment[] }>("GET", "/api/rag/training-departments"),
  createDepartmentInvitation: (id: string, email: string) =>
    request<UserInvitation>("POST", `/api/departments/${encodeURIComponent(id)}/invitations`, { email }),
  departmentInvitations: (id: string) => request<{ invitations: UserInvitation[] }>("GET", `/api/departments/${encodeURIComponent(id)}/invitations`),
  resendDepartmentInvitation: (groupId: string, invitationId: string) =>
    request<UserInvitation>("POST", `/api/departments/${encodeURIComponent(groupId)}/invitations/${encodeURIComponent(invitationId)}/resend`),
  revokeDepartmentInvitation: (groupId: string, invitationId: string) =>
    request<UserInvitation>("POST", `/api/departments/${encodeURIComponent(groupId)}/invitations/${encodeURIComponent(invitationId)}/revoke`),
  addDepartmentMember: (id: string, email: string) =>
    request<void>("POST", `/api/departments/${encodeURIComponent(id)}/members`, { email }),
  removeDepartmentMember: (id: string, userId: string) =>
    request<void>("DELETE", `/api/departments/${encodeURIComponent(id)}/members/${encodeURIComponent(userId)}`),

  adminUsers: () => request<{ users: ManagedUser[] }>("GET", "/api/admin/users"),
  updateAdminUser: (id: string, role: "admin" | "manager" | "employee", departmentIds: string[]) =>
    request<ManagedUser>("PATCH", `/api/admin/users/${encodeURIComponent(id)}`, { role, department_ids: departmentIds }),
  setUserActive: (id: string, active: boolean, knowledgeTransferTo?: string) =>
    request<ManagedUser>("POST", `/api/admin/users/${encodeURIComponent(id)}/active`, {
      active,
      ...(active || !knowledgeTransferTo ? {} : { knowledge_transfer_to: knowledgeTransferTo }),
    }),
  transferUserKnowledge: (id: string, successorUserId: string) =>
    request<{ transferred_entries: number }>("POST", `/api/admin/users/${encodeURIComponent(id)}/knowledge-transfer`, {
      successor_user_id: successorUserId,
    }),
  invitations: () => request<{ invitations: UserInvitation[] }>("GET", "/api/admin/users/invitations"),
  createInvitation: (input: { email: string; role: "manager" | "employee"; department_ids: string[]; role_template_id?: string | null; position_id?: string | null; reports_to_id?: string | null }) =>
    request<UserInvitation>("POST", "/api/admin/users/invitations", input),
  previewInvitationCsv: (csvText: string) =>
    request<CsvInvitationPreview>("POST", "/api/admin/users/invitations/csv-preview", { csv_text: csvText }),
  revokeInvitation: (id: string) =>
    request<UserInvitation>("POST", `/api/admin/users/invitations/${encodeURIComponent(id)}/revoke`),
  resendInvitation: (id: string) =>
    request<UserInvitation>("POST", `/api/admin/users/invitations/${encodeURIComponent(id)}/resend`),
  adminGroups: () => request<{ groups: DepartmentGroup[] }>("GET", "/api/admin/access/groups"),
  createAdminGroup: (
    name: string,
    kind: "department" | "project" | "client" | "custom",
    options: { parent_id?: string | null; owner_id?: string | null } = {},
  ) => request<DepartmentGroup>("POST", "/api/admin/access/groups", { name, kind, ...options }),
  updateAdminGroup: (
    id: string,
    input: {
      name?: string;
      parent_id?: string | null;
      owner_id?: string | null;
      lifecycle_status?: "active" | "archived";
    },
  ) => request<DepartmentGroup>("PATCH", `/api/admin/access/groups/${encodeURIComponent(id)}`, input),
  adminWorkspaceSummary: (id: string) =>
    request<WorkspaceSummaryReport>("GET", `/api/admin/access/groups/${encodeURIComponent(id)}/summary`),
  adminGroupMembers: (id: string) =>
    request<{ members: Array<{ user_id: string; email: string }> }>("GET", `/api/admin/access/groups/${encodeURIComponent(id)}/members`),
  addAdminGroupMember: (id: string, email: string) =>
    request<void>("POST", `/api/admin/access/groups/${encodeURIComponent(id)}/members`, { email }),
  removeAdminGroupMember: (id: string, userId: string) =>
    request<void>("DELETE", `/api/admin/access/groups/${encodeURIComponent(id)}/members/${encodeURIComponent(userId)}`),
  permissionGrants: () => request<{ grants: PermissionGrant[] }>("GET", "/api/admin/access/grants"),
  permissionCatalog: () => request<{ permissions: PermissionCatalogEntry[] }>("GET", "/api/admin/access/permission-catalog"),
  createPermissionGrant: (input: {
    email: string;
    permission: PermissionGrant["permission"];
    category?: string | null;
    scope_group_id?: string | null;
  }) => request<PermissionGrant>("POST", "/api/admin/access/grants", input),
  revokePermissionGrant: (id: string) =>
    request<PermissionGrant>("POST", `/api/admin/access/grants/${encodeURIComponent(id)}/revoke`),
  roleTemplates: () => request<{ templates: RoleTemplate[] }>("GET", "/api/admin/role-templates"),
  roleLibrary: () => request<{ roles: Array<{ key: string; name: string; base_role: "employee" | "manager"; description: string; permissions: RoleTemplatePermission[] }> }>("GET", "/api/admin/role-library"),
  createRoleTemplate: (input: {
    name: string;
    description: string;
    base_role: "manager" | "employee";
    permissions: RoleTemplatePermission[];
  }) => request<RoleTemplate>("POST", "/api/admin/role-templates", input),
  roleTemplateChanges: () => request<{ requests: RoleTemplateChangeRequest[] }>("GET", "/api/admin/role-template-change-requests"),
  proposeRoleTemplateChange: (id: string, input: {
    description: string;
    permissions: RoleTemplatePermission[];
  }) => request<RoleTemplateChangeRequest>("POST", `/api/admin/role-templates/${encodeURIComponent(id)}/change-requests`, input),
  decideRoleTemplateChange: (id: string, decision: "approve" | "reject", rationale: string) =>
    request<RoleTemplateChangeRequest>("POST", `/api/admin/role-template-change-requests/${encodeURIComponent(id)}/decision`, { decision, rationale }),
  setRoleTemplateActive: (id: string, active: boolean) =>
    request<RoleTemplate>("PATCH", `/api/admin/role-templates/${encodeURIComponent(id)}/active`, { active }),
  roleTemplateAssignments: () => request<{ assignments: RoleTemplateAssignment[] }>("GET", "/api/admin/role-template-assignments"),
  assignRoleTemplate: (id: string, email: string, departmentIds: string[], expiresAt: string) =>
    request<RoleTemplateAssignment>("POST", `/api/admin/role-templates/${encodeURIComponent(id)}/assignments`, {
      email, department_ids: departmentIds, expires_at: expiresAt,
    }),
  revokeRoleTemplateAssignment: (id: string) =>
    request<RoleTemplateAssignment>("POST", `/api/admin/role-template-assignments/${encodeURIComponent(id)}/revoke`),
  usageReport: (days: number, signal?: AbortSignal) => request<UsageReport>("GET", `/api/admin/reports/usage?days=${days}`, undefined, signal),
  usageReportDownload: (days: number, format: "csv" | "pdf" | "docx" | "xlsx" | "pptx") =>
    download(`/api/admin/reports/usage/export?days=${days}&format=${format}`),
  deployment: () => request<DeploymentOverview>("GET", "/api/admin/deployment"),
  industryTemplates: () => request<IndustryTemplate[]>("GET", "/api/admin/deployment/templates"),
  applyIndustryTemplate: (templateId: string) =>
    request<IndustryTemplateApply>("POST", `/api/admin/deployment/templates/${encodeURIComponent(templateId)}/apply`),
  companyProfile: () => request<CompanyProfile>("GET", "/api/deployment/profile"),
  updateCompanyProfile: (profile: CompanyProfile) =>
    request<CompanyProfile>("PUT", "/api/admin/deployment/company", profile),
  installLicense: (license: SignedLicense) =>
    request<LicenseStatus>("PUT", "/api/admin/deployment/license", license),
  mediaTranscriptionStatus: () => request<MediaTranscriptionStatus>("GET", "/api/admin/media-transcription"),
  myExpertise: () => request<ExpertiseProfile>("GET", "/api/expertise/me"),
  updateMyExpertise: (tags: string[]) => request<ExpertiseProfile>("PUT", "/api/expertise/me", { tags }),
  searchExpertise: (skill: string, after?: string) =>
    request<ExpertiseSearchResponse>("GET", `/api/expertise/search?skill=${encodeURIComponent(skill)}${after ? `&after=${encodeURIComponent(after)}` : ""}`),

  knowledge: () => request<KnowledgeList>("GET", "/api/knowledge"),
  getKnowledge: (id: string) => request<KnowledgeDetail>("GET", `/api/knowledge/${encodeURIComponent(id)}`),
  createKnowledge: (input: {
    statement: string;
    category: KnowledgeCategory;
    visibility?: KnowledgeVisibility;
    scope_group_id?: string;
    evidence: Array<{ document_id: string; quote: string }>;
  }) => request<KnowledgeDetail>("POST", "/api/knowledge", input),
  withdrawKnowledge: (id: string) => request<KnowledgeDetail>("POST", `/api/knowledge/${encodeURIComponent(id)}/withdraw`),
  submitKnowledge: (id: string) => request<KnowledgeDetail>("POST", `/api/knowledge/${encodeURIComponent(id)}/submit`),

  knowledgeInterviewOptions: () => request<InterviewOptions>("GET", "/api/knowledge-interviews/options"),
  knowledgeInterviewSessions: () => request<{ sessions: InterviewSession[] }>("GET", "/api/knowledge-interviews/sessions"),
  knowledgeInterviewSession: (id: string) =>
    request<InterviewSession>("GET", `/api/knowledge-interviews/sessions/${encodeURIComponent(id)}`),
  startKnowledgeInterview: (template_id: string, department_group_id: string | null) =>
    request<InterviewSession>("POST", "/api/knowledge-interviews/sessions", { template_id, department_group_id }),
  saveKnowledgeInterview: (id: string, answers: Record<string, string>, sop_title?: string) =>
    request<InterviewSession>("PATCH", `/api/knowledge-interviews/sessions/${encodeURIComponent(id)}`, { answers, sop_title }),
  submitKnowledgeInterview: (id: string) =>
    request<InterviewSession>("POST", `/api/knowledge-interviews/sessions/${encodeURIComponent(id)}/submit`),
  reviewKnowledgeInterview: (id: string, decision: "approved" | "changes_requested", rationale: string) =>
    request<InterviewSession>("POST", `/api/knowledge-interviews/sessions/${encodeURIComponent(id)}/review`, { decision, rationale }),
  withdrawKnowledgeInterview: (id: string) =>
    request<InterviewSession>("POST", `/api/knowledge-interviews/sessions/${encodeURIComponent(id)}/withdraw`),
  createKnowledgeInterviewTemplate: (input: {
    slug: string; title: string; description: string; questions: InterviewQuestion[];
  }) => request<InterviewTemplate>("POST", "/api/knowledge-interviews/templates", input),
  updateKnowledgeInterviewTemplate: (id: string, changes: Partial<Pick<InterviewTemplate, "title" | "description" | "questions" | "is_active">>) =>
    request<InterviewTemplate>("PATCH", `/api/knowledge-interviews/templates/${encodeURIComponent(id)}`, changes),

  proposalCoverageOptions: () => request<ProposalCoverageOptions>("GET", "/api/proposal-coverage/options"),
  proposalCoverageCases: () => request<{ cases: ProposalCoverageSummary[] }>("GET", "/api/proposal-coverage"),
  proposalCoverageCase: (id: string) => request<ProposalCoverageCase>("GET", `/api/proposal-coverage/${encodeURIComponent(id)}`),
  proposalCoverageExport: (id: string, format: "pdf" | "docx" | "xlsx") =>
    download(`/api/proposal-coverage/${encodeURIComponent(id)}/export?format=${format}`),
  extractProposalRequirements: (document_ids: string[]) =>
    request<ProposalRequirementExtraction>("POST", "/api/proposal-coverage/extract-requirements", { document_ids }),
  createProposalCoverageCase: (input: { title: string; department_group_id: string | null; requirements: Array<{ requirement: string; source_chunk_ids: number[] }> }) =>
    request<ProposalCoverageCase>("POST", "/api/proposal-coverage", input),
  updateProposalCoverageCase: (id: string, input: { title: string; department_group_id: string | null; requirements: Array<{ requirement: string; source_chunk_ids: number[] }> }) =>
    request<ProposalCoverageCase>("PUT", `/api/proposal-coverage/${encodeURIComponent(id)}`, input),
  analyzeProposalRequirement: (caseId: string, requirementId: string) =>
    request<ProposalCoverageCase>("POST", `/api/proposal-coverage/${encodeURIComponent(caseId)}/requirements/${encodeURIComponent(requirementId)}/analyze`, {}),
  assessProposalRequirement: (caseId: string, requirementId: string, assessment: "meets" | "partial" | "gap", rationale: string) =>
    request<ProposalCoverageCase>("POST", `/api/proposal-coverage/${encodeURIComponent(caseId)}/requirements/${encodeURIComponent(requirementId)}/assessment`, { assessment, rationale }),
  submitProposalCoverageCase: (id: string) => request<ProposalCoverageCase>("POST", `/api/proposal-coverage/${encodeURIComponent(id)}/submit`),
  reviewProposalCoverageCase: (id: string, decision: "approved" | "changes_requested", rationale: string) =>
    request<ProposalCoverageCase>("POST", `/api/proposal-coverage/${encodeURIComponent(id)}/review`, { decision, rationale }),
  withdrawProposalCoverageCase: (id: string) => request<ProposalCoverageCase>("POST", `/api/proposal-coverage/${encodeURIComponent(id)}/withdraw`),

  memories: () => request<MemoryList>("GET", "/api/memory"),
  createMemory: (content: string, kind?: MemoryKind) =>
    request<MemorySaveResult>("POST", "/api/memory", kind ? { content, kind } : { content }),
  updateMemory: (id: string, changes: { content?: string; kind?: MemoryKind }) =>
    request<MemorySaveResult>("PATCH", `/api/memory/${encodeURIComponent(id)}`, changes),
  deleteMemory: (id: string) => request<void>("DELETE", `/api/memory/${encodeURIComponent(id)}`),
  clearMemories: () => request<{ deleted: number }>("DELETE", "/api/memory?all=true"),

  traces: (filters: { days: number; status: string; outcome: string }) =>
    request<{ summary: TraceSummary; traces: TraceRow[] }>(
      "GET",
      `/api/admin/traces?${new URLSearchParams({ ...filters, days: String(filters.days), limit: "200" })}`,
    ),
  trace: (id: number) => request<TraceDetail>("GET", `/api/admin/traces/${id}`),
  vectorStore: () => request<VectorStoreStatus>("GET", "/api/rag/vector-store"),
  folderImportProgress: (sourceId: string) =>
    request<FolderImportProgress>("GET", `/api/rag/imports/folders/${encodeURIComponent(sourceId)}`),
  folderSyncSources: () => request<{ items: FolderSyncSource[] }>("GET", "/api/folder-sync/sources"),
  createFolderSyncSource: (input: {
    name: string; relative_path: string; visibility: "private" | "shared" | "restricted";
    department_group_id?: string | null; interval_minutes: number;
  }) => request<FolderSyncSource>("POST", "/api/folder-sync/sources", input),
  updateFolderSyncSource: (id: string, changes: { interval_minutes?: number; enabled?: boolean }) =>
    request<FolderSyncSource>("PATCH", `/api/folder-sync/sources/${encodeURIComponent(id)}`, changes),
  folderSyncRuns: (id: string) => request<{ items: FolderSyncRun[] }>("GET", `/api/folder-sync/sources/${encodeURIComponent(id)}/runs`),
  folderSyncNotifications: () => request<{ items: FolderSyncNotification[] }>("GET", "/api/folder-sync/notifications"),
  readFolderSyncNotification: (id: string) =>
    request<FolderSyncNotification>("POST", `/api/folder-sync/notifications/${encodeURIComponent(id)}/read`),
  folderSyncPreference: () => request<{ change_alerts_enabled: boolean }>("GET", "/api/folder-sync/preferences"),
  updateFolderSyncPreference: (changeAlertsEnabled: boolean) =>
    request<{ change_alerts_enabled: boolean }>("PUT", "/api/folder-sync/preferences", { change_alerts_enabled: changeAlertsEnabled }),
  positions: () => request<{ positions: Position[] }>("GET", "/api/admin/positions"),
  createPosition: (input: { title: string; job_family: JobFamily; level: number | null; description: string }) =>
    request<Position>("POST", "/api/admin/positions", input),
  updatePosition: (id: string, input: { title?: string; job_family?: JobFamily; level?: number | null; clear_level?: boolean; description?: string; is_active?: boolean }) =>
    request<Position>("PATCH", `/api/admin/positions/${encodeURIComponent(id)}`, input),
  positionLibrary: () => request<{ positions: LibraryPosition[] }>("GET", "/api/admin/positions/library"),
  applyPositionLibrary: (titles: string[] | null) =>
    request<{ created: string[]; skipped: string[] }>("POST", "/api/admin/positions/library/apply", { titles }),
  assignOrg: (userId: string, input: { position_id: string | null; reports_to_id: string | null }) =>
    request<OrgChartPerson>("PUT", `/api/admin/users/${encodeURIComponent(userId)}/org`, input),
  orgHistory: (userId: string) =>
    request<{ entries: OrgHistoryEntry[] }>("GET", `/api/admin/users/${encodeURIComponent(userId)}/org-history`),
  orgChart: () => request<{ people: OrgChartPerson[] }>("GET", "/api/org/chart"),
  auditLogs: (limit = 25) =>
    request<{ entries: AuditEntry[] }>("GET", `/api/admin/audit-logs?limit=${limit}`),
};
