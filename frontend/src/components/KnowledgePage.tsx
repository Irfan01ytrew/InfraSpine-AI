import { type ChangeEvent, type FormEvent, type KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, describeError } from "../api";
import { citationPlace, formatDate, isPageCitation, mediaPreviewHref, timeAgo } from "../format";
import {
  Spinner,
} from "../icons";
import type {
  Citation,
  ConversationMessage,
  ConversationSummary,
  FileFormat,
  IngestionStatus,
  ModelOption,
  RagAnswer,
  RagDocument,
  DepartmentGroup,
  FinancialExtraction,
  FinancialFormReviewQueueItem,
  User,
} from "../types";

const SUPPORTED_EXTENSIONS = [
  ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".csv", ".md", ".markdown", ".json", ".html", ".htm", ".png", ".jpg", ".jpeg", ".webp",
  ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
];
const ACCEPT = SUPPORTED_EXTENSIONS.join(",");
/** What `page_count` counts for each format (flowing text has no pages). */
const UNIT: Partial<Record<FileFormat, string>> = { pdf: "page", pptx: "slide", ppt: "slide", xlsx: "sheet", xls: "sheet" };
import { ChartView } from "./ChartView";
import { ConversationSidebar } from "./ConversationSidebar";
import { DocumentLifecyclePanel } from "./DocumentLifecyclePanel";
import { DatabaseAnalysisPanel } from "./DatabaseAnalysisPanel";
import { Markdown } from "./Markdown";
import { CopyButton } from "./CopyButton";
import { KnowledgeDashboard } from "./KnowledgeDashboard";
import { useFeedback } from "./Feedback";
import { AlertTriangle, ArrowUp, ArrowUpRight, ChevronDown, Download, ExternalLink, FileAudio, FileText, FolderOpen, Info, LayoutDashboard, ListChecks, Loader2, Lock, MessageSquare, Mic, MoreHorizontal, PanelLeft, Paperclip, RotateCw, ScanText, Search, Sparkles, Square, Target, ThumbsDown, ThumbsUp, Trash2, UploadCloud, X } from "lucide-react";
import { cn } from "../lib/utils";
import { Alert, Badge, Button, DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger, EmptyState, Field, Input, Select, Skeleton, Tooltip } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

const STATUS_LABEL: Record<RagDocument["status"], string> = {
  pending: "Queued",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
};

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function formatSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function extractionLocation(source: Record<string, unknown> | null): string | null {
  if (!source) return null;
  if (typeof source.label === "string") return source.label;
  const kind = typeof source.kind === "string" ? source.kind : "source";
  const position = typeof source.position === "number" ? ` ${source.position}` : "";
  const row = typeof source.row_number === "number" ? `, row ${source.row_number}` : "";
  return `${kind}${position}${row}`;
}

type StatusFilter = "all" | "ready" | "working" | "failed";

const STATUS_FILTERS: { value: StatusFilter; label: string; matches: (d: RagDocument) => boolean }[] = [
  { value: "all", label: "All statuses", matches: () => true },
  { value: "ready", label: "Ready", matches: (d) => d.status === "ready" },
  { value: "working", label: "In progress", matches: (d) => d.status === "pending" || d.status === "processing" },
  { value: "failed", label: "Failed", matches: (d) => d.status === "failed" },
];

/** A library document the next questions are limited to. */
interface FocusDocument {
  id: string;
  filename: string;
}

const VISIBILITY: Record<RagDocument["visibility"], { label: string; title: string }> = {
  private: { label: "Private", title: "Only the owner can see it" },
  shared: { label: "Shared", title: "Visible to all users" },
  restricted: { label: "Restricted", title: "Visible only to the people and groups it is shared with" },
};

export function KnowledgePage({ user }: { user: User }) {
  const { toast, confirm } = useFeedback();
  const [docs, setDocs] = useState<RagDocument[] | null>(null);
  const [maxMb, setMaxMb] = useState(20);
  const [ingestion, setIngestion] = useState<IngestionStatus | null>(null);
  const isAdmin = user.role === "admin";
  const isManager = user.role === "manager";
  const canUpload = isAdmin || isManager;
  const [view, setView] = useState<"chat" | "library" | "dashboard">("chat");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);
  const [uploadGroups, setUploadGroups] = useState<DepartmentGroup[]>([]);
  const [scopeGroupId, setScopeGroupId] = useState("");
  const [uploadVisibility, setUploadVisibility] = useState<"shared" | "restricted">("shared");
  const [uploading, setUploading] = useState(false);
  const [financialProfile, setFinancialProfile] = useState<"auto" | "invoice" | "purchase_order" | "form">("auto");
  const [financialReview, setFinancialReview] = useState<{ documentId: string; result: FinancialExtraction } | null>(null);
  const [financialBusy, setFinancialBusy] = useState<string | null>(null);
  const [financialQueue, setFinancialQueue] = useState<FinancialFormReviewQueueItem[] | null>(null);
  const [financialReviewedExceptions, setFinancialReviewedExceptions] = useState<number[]>([]);
  const [financialRationale, setFinancialRationale] = useState("");
  const [financialExportBusy, setFinancialExportBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const replaceInput = useRef<HTMLInputElement>(null);
  const replaceTarget = useRef<string | null>(null);
  const documentsLoading = useRef(false);
  const documentErrorAt = useRef(0);

  const load = useCallback(async () => {
    if (documentsLoading.current) return;
    documentsLoading.current = true;
    try {
      const list = await api.documents();
      setDocs(list.documents);
      setMaxMb(list.max_upload_mb);
      setIngestion(list.ingestion ?? null);
      void api.financialReviewQueue().then((queue) => setFinancialQueue(queue.records)).catch(() => setFinancialQueue(null));
    } catch (err) {
      if (Date.now() - documentErrorAt.current > 60_000) {
        documentErrorAt.current = Date.now();
        toast("error", "Document library reconnecting", describeError(err));
      }
    } finally {
      documentsLoading.current = false;
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);
  useLiveRefresh(load);

  useEffect(() => {
    if (!isManager && !isAdmin) return;
    const scope = isManager ? api.managedDepartments() : api.adminGroups();
    scope.then((result) => {
      const allowedKinds = isAdmin ? ["department", "client", "project", "custom"] : ["department"];
      const localGroups = result.groups.filter((group) => allowedKinds.includes(group.kind) && group.source === "local");
      setUploadGroups(localGroups);
      setScopeGroupId((current) => localGroups.some((group) => group.id === current) ? current : localGroups[0]?.id || "");
    }).catch((err) => toast("error", "Could not load available document scopes", describeError(err)));
  }, [isAdmin, isManager, toast]);

  // Poll while anything is being processed in the background.
  const busy = docs?.some((d) => d.status === "pending" || d.status === "processing");
  useEffect(() => {
    if (!busy) return;
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [busy, load]);

  function validate(file: File): string | null {
    if (!SUPPORTED_EXTENSIONS.some((ext) => file.name.toLowerCase().endsWith(ext)))
      return "Supported files: PDF, Word, Excel, PowerPoint, TXT, CSV, PNG/JPG, audio and video.";
    if (file.size > maxMb * 1024 * 1024) return `The file is larger than ${maxMb} MB.`;
    return null;
  }

  async function onUpload(files: FileList | null) {
    const selected = Array.from(files ?? []);
    if (fileInput.current) fileInput.current.value = "";
    if (!selected.length) return;
    if ((isManager || (isAdmin && uploadVisibility === "restricted")) && !scopeGroupId) {
      toast("error", "Choose an access group", isManager ? "Restricted documents must belong to a department you manage." : "Choose the department or workspace that should be added to this document's ACL.");
      return;
    }
    setUploading(true);
    const received: string[] = [];
    try {
      // One request per file, in order: a rejected file is reported and the others still upload.
      for (const file of selected) {
        const problem = validate(file);
        if (problem) {
          toast("error", `${file.name} not accepted`, problem);
          continue;
        }
        try {
          const visibility = isAdmin ? uploadVisibility : "restricted";
          await api.uploadDocument(file, visibility, visibility === "restricted" ? scopeGroupId : undefined);
          received.push(file.name);
        } catch (err) {
          toast("error", `${file.name} failed`, describeError(err));
        }
      }
      if (received.length === 1) toast("success", "Upload received", `${received[0]} is being processed.`);
      else if (received.length > 1)
        toast("success", "Uploads received", `${received.length} documents are queued and will be processed one by one.`);
    } finally {
      setUploading(false);
      await load();
    }
  }

  async function onReplace(files: FileList | null) {
    const file = files?.[0];
    const id = replaceTarget.current;
    if (replaceInput.current) replaceInput.current.value = "";
    if (!file || !id) return;
    const problem = validate(file);
    if (problem) return toast("error", "File not accepted", problem);
    try {
      await api.replaceDocument(id, file);
      toast("success", "Replacement received", "The current version stays searchable until the new one is ready.");
    } catch (err) {
      toast("error", "Replace failed", describeError(err));
    }
    await load();
  }

  async function onDownloadOriginal(doc: RagDocument) {
    try {
      const blob = await api.downloadDocumentFile(doc.id);
      const href = URL.createObjectURL(blob);
      const link = window.document.createElement("a");
      link.href = href;
      link.download = doc.filename;
      link.click();
      URL.revokeObjectURL(href);
    } catch (err) {
      toast("error", "Could not download original", describeError(err));
    }
  }

  async function onExtractFinancialFields(
    documentId: string,
    profile: "auto" | "invoice" | "purchase_order" | "form" = financialProfile,
  ) {
    setFinancialBusy(documentId);
    try {
      const result = await api.extractFinancialFields(documentId, profile);
      setFinancialReview({ documentId, result });
      setFinancialReviewedExceptions(result.review?.reviewed_exception_indexes ?? []);
      setFinancialRationale(result.review?.rationale ?? "Verified against the current source document");
    } catch (err) {
      toast("error", "Could not extract fields", describeError(err));
    } finally {
      setFinancialBusy(null);
    }
  }

  function setFinancialField(name: string, value: string) {
    setFinancialReview((current) => {
      if (!current) return current;
      const field = current.result.fields[name];
      return {
        ...current,
        result: { ...current.result, fields: {
          ...current.result.fields,
          [name]: { ...field, value: value || null, raw: value || null },
        } },
      };
    });
  }

  function setFinancialLineItem(index: number, name: "description" | "quantity" | "unit_price" | "amount" | "currency", value: string) {
    setFinancialReview((current) => {
      if (!current) return current;
      const line_items = current.result.line_items.map((item, itemIndex) => itemIndex === index
        ? { ...item, [name]: value || null }
        : item);
      return { ...current, result: { ...current.result, line_items } };
    });
  }

  async function onSaveFinancialReview(documentId: string) {
    if (!financialReview || financialReview.documentId !== documentId) return;
    const { result } = financialReview;
    if (financialReviewedExceptions.length !== result.exceptions.length || financialRationale.trim().length < 3) return;
    setFinancialBusy(documentId);
    try {
      const reviewed = await api.reviewFinancialFields(documentId, {
        expected_document_version: result.document_version,
        profile: result.profile,
        date_order: result.date_order,
        fields: Object.fromEntries(Object.entries(result.fields).map(([name, field]) => [name, field.value])),
        line_items: result.line_items.map(({ description, quantity, unit_price, amount, currency }) => ({
          description, quantity, unit_price, amount, currency,
        })),
        reviewed_exception_indexes: [...financialReviewedExceptions].sort((a, b) => a - b),
        rationale: financialRationale,
      });
      setFinancialReview({ documentId, result: reviewed });
      setFinancialReviewedExceptions(reviewed.review?.reviewed_exception_indexes ?? []);
      setFinancialRationale(reviewed.review?.rationale ?? financialRationale);
      toast("success", "Review saved", `Revision ${reviewed.review?.revision ?? ""} is recorded in the review history.`);
      void api.financialReviewQueue().then((queue) => setFinancialQueue(queue.records)).catch(() => setFinancialQueue(null));
    } catch (err) {
      toast("error", "Could not save finance review", describeError(err));
    } finally {
      setFinancialBusy(null);
    }
  }

  async function onExportReviewedFinances() {
    setFinancialExportBusy(true);
    try {
      const blob = await api.exportReviewedFinancialRecords();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "infraspine-bookkeeping-reviewed.csv";
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      toast("error", "Could not export reviewed records", describeError(err));
    } finally {
      setFinancialExportBusy(false);
    }
  }

  async function onDelete(doc: RagDocument) {
    const ok = await confirm({
      title: `Delete ${doc.filename}?`,
      body: "The file, its extracted text and its search index entries are permanently removed.",
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await api.deleteDocument(doc.id);
      if (focusDoc?.id === doc.id) setFocusDoc(null);
      toast("success", "Document deleted");
    } catch (err) {
      toast("error", "Delete failed", describeError(err));
    }
    await load();
  }

  async function onCancel(doc: RagDocument) {
    try {
      await api.cancelProcessing(doc.id);
      toast("info", "Processing cancelled", "Use Retry to process the file again.");
    } catch (err) {
      toast("error", "Could not cancel", describeError(err));
    }
    await load();
  }

  async function onRetry(doc: RagDocument) {
    try {
      await api.reprocessDocument(doc.id);
      toast("info", "Queued for processing");
    } catch (err) {
      toast("error", "Retry failed", describeError(err));
    }
    await load();
  }

  const readyCount = docs?.filter((d) => d.searchable).length ?? 0;

  // ---- library search and filter (client-side over the loaded list)
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const needle = query.trim().toLowerCase();
  const statusMatch = STATUS_FILTERS.find((f) => f.value === statusFilter)!.matches;
  const shownDocs = docs?.filter((d) => statusMatch(d) && (!needle || d.filename.toLowerCase().includes(needle))) ?? [];

  // ---- "Ask about this document": the next questions in the open chat search this document only
  const [focusDoc, setFocusDoc] = useState<FocusDocument | null>(null);

  // ---- conversations (persisted server-side; the open one is kept in the URL: #/c/<id>)
  const [conversations, setConversations] = useState<ConversationSummary[] | null>(null);
  const [activeId, setActiveId] = useState<string | null>(
    () => window.location.hash.match(/^#\/c\/([0-9a-f-]{36})$/)?.[1] ?? null,
  );

  useEffect(() => {
    const syncConversationHash = () => {
      const id = window.location.hash.match(/^#\/c\/([0-9a-f-]{36})$/)?.[1] ?? null;
      setActiveId(id);
      if (!id) setFocusDoc(null);
    };
    window.addEventListener("hashchange", syncConversationHash);
    return () => window.removeEventListener("hashchange", syncConversationHash);
  }, []);

  const loadConversations = useCallback(async () => {
    try {
      setConversations((await api.conversations()).conversations);
    } catch (err) {
      setConversations([]);
      toast("error", "Could not load chats", describeError(err));
    }
  }, [toast]);

  useEffect(() => {
    void loadConversations();
  }, [loadConversations]);

  const selectConversation = useCallback((id: string | null, keepFocus = false) => {
    setView("chat");
    setHistoryOpen(false);
    setActiveId(id);
    if (!keepFocus) setFocusDoc(null); // a document focus belongs to the chat it was set in
    window.history.replaceState(null, "", id ? `#/c/${id}` : "#/knowledge");
  }, []);

  const upsertConversation = useCallback((c: ConversationSummary) => {
    setConversations((all) => [c, ...(all ?? []).filter((x) => x.id !== c.id)]); // most recent first
  }, []);

  async function renameConversation(id: string, title: string) {
    try {
      const updated = await api.renameConversation(id, title);
      setConversations((all) => (all ?? []).map((c) => (c.id === id ? updated : c)));
    } catch (err) {
      toast("error", "Rename failed", describeError(err));
    }
  }

  async function deleteConversation(c: ConversationSummary) {
    const ok = await confirm({
      title: `Delete "${c.title}"?`,
      body: "This conversation and all of its messages are permanently deleted.",
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await api.deleteConversation(c.id);
      setConversations((all) => (all ?? []).filter((x) => x.id !== c.id));
      if (activeId === c.id) selectConversation(null);
      toast("success", "Chat deleted");
    } catch (err) {
      toast("error", "Delete failed", describeError(err));
    }
  }

  const activeTitle = conversations?.find((c) => c.id === activeId)?.title ?? "New chat";

  return (
    <div className="relative flex min-h-0 min-w-0 flex-1 flex-col bg-surface"
      onDragEnter={(event) => { if (canUpload && event.dataTransfer.types.includes("Files")) { event.preventDefault(); dragDepth.current += 1; setDragging(true); } }}
      onDragOver={(event) => { if (canUpload && event.dataTransfer.types.includes("Files")) { event.preventDefault(); event.dataTransfer.dropEffect = uploading ? "none" : "copy"; } }}
      onDragLeave={() => { dragDepth.current = Math.max(0, dragDepth.current - 1); if (!dragDepth.current) setDragging(false); }}
      onDrop={(event) => { dragDepth.current = 0; setDragging(false); if (canUpload && event.dataTransfer.files.length) { event.preventDefault(); if (!uploading) { setView("library"); void onUpload(event.dataTransfer.files); } } }}
    >
      <header className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-3 py-2 sm:px-5">
        <div className="flex items-center gap-2">
          {view === "chat" && <Button variant="ghost" size="icon-sm" className="lg:hidden" aria-label="Toggle chat history" aria-expanded={historyOpen} onClick={() => setHistoryOpen((open) => !open)}><PanelLeft /></Button>}
          <span className="hidden items-center gap-2 text-sm font-semibold sm:flex"><Sparkles className="size-4 text-primary" /> Knowledge</span>
        </div>
        <nav className="flex items-center gap-1 rounded-lg bg-surface-2 p-1" aria-label="Knowledge workspace">
          {([{ id: "chat", label: "Chat", icon: MessageSquare }, { id: "library", label: "Library", icon: FolderOpen }, { id: "dashboard", label: "Overview", icon: LayoutDashboard }] as const).map((item) => <Button key={item.id} size="sm" variant={view === item.id ? "secondary" : "ghost"} aria-pressed={view === item.id} onClick={() => setView(item.id)}><item.icon className="size-3.5" />{item.label}</Button>)}
        </nav>
      </header>
      {dragging && <div className="pointer-events-none absolute inset-0 z-20 grid place-items-center rounded-xl border-2 border-dashed border-primary bg-surface/95" role="status"><div className="text-center"><UploadCloud className="mx-auto mb-3 size-9 text-primary" /><p className="font-semibold">Drop files to add to your library</p><p className="mt-2 text-sm text-muted">Your selected library sharing settings apply.</p></div></div>}
      <div className={cn("min-h-0 min-w-0 flex-1", view === "chat" ? "grid grid-cols-1 lg:grid-cols-[248px_minmax(0,1fr)] lg:overflow-hidden" : "flex flex-col overflow-hidden")}>
      <div className={cn("min-h-0 flex-col [&>aside]:flex-1", view !== "chat" ? "hidden" : historyOpen ? "flex" : "hidden lg:flex")}>
      <ConversationSidebar
        conversations={conversations}
        activeId={activeId}
        onSelect={selectConversation}
        onNew={() => selectConversation(null)}
        onRename={renameConversation}
        onDelete={(c) => void deleteConversation(c)}
      />
      </div>
      <div className={cn("min-h-0 min-w-0 flex-col [&>section]:flex-1", view === "chat" ? "flex" : "hidden")}>
      <KnowledgeChat
        conversationId={activeId}
        title={activeTitle}
        readyCount={readyCount}
        canManageLibrary={isAdmin}
        onAttach={canUpload ? () => fileInput.current?.click() : undefined}
        uploading={uploading}
        onOpenLibrary={() => setView("library")}
        focus={focusDoc}
        onClearFocus={() => setFocusDoc(null)}
        onCreated={(c) => {
          upsertConversation(c);
          selectConversation(c.id, true); // the chat was created by its first question: keep its focus
        }}
        onUpdated={upsertConversation}
        onMissing={() => {
          selectConversation(null);
          void loadConversations();
        }}
      />
      </div>
      {view === "dashboard" && <KnowledgeDashboard documents={docs} ingestion={ingestion} isAdmin={isAdmin} onRefresh={load} onOpenLibrary={(filter) => { setStatusFilter(filter); setQuery(""); setView("library"); }} />}
      <section className={cn("mx-auto min-h-0 w-full max-w-6xl flex-1 flex-col bg-surface", view === "library" ? "flex" : "hidden")} aria-label="Document library">
        <div className="shrink-0 space-y-3 border-b border-border p-4">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-foreground">Library</h3>
              <Badge>{docs?.length ?? 0}</Badge>
            </div>
            {canUpload && (
              <div className="flex items-center gap-1">
                <Button size="sm" variant="primary" onClick={() => fileInput.current?.click()} loading={uploading}>
                  {!uploading && <UploadCloud />} Upload
                </Button>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button size="icon-sm" variant="ghost" aria-label="More library actions"><MoreHorizontal /></Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent>
                    <DropdownMenuItem disabled={financialExportBusy} onSelect={() => void onExportReviewedFinances()}>
                      <Download /> Export reviewed bookkeeping CSV
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            )}
          </div>
          {canUpload && (
            <>
              {isAdmin && (
                <Field label="Upload sharing">
                  <Select
                    size="sm"
                    aria-label="Upload sharing"
                    value={uploadVisibility}
                    onValueChange={(v) => setUploadVisibility(v as "shared" | "restricted")}
                    options={[{ value: "shared", label: "Shared with everyone" }, { value: "restricted", label: "Restricted to a group" }]}
                  />
                </Field>
              )}
              {(isManager || (isAdmin && uploadVisibility === "restricted")) && (
                <Field label={isAdmin ? "Department or workspace" : "Department"}>
                  <Select
                    size="sm"
                    aria-label={isAdmin ? "Department or workspace" : "Department"}
                    value={scopeGroupId}
                    onValueChange={setScopeGroupId}
                    placeholder="Choose access group"
                    options={uploadGroups.map((group) => ({
                      value: group.id,
                      label: `${group.kind === "department" ? "Department" : group.kind === "client" ? "Client" : group.kind === "project" ? "Project" : "Custom group"} · ${group.name}`,
                    }))}
                  />
                </Field>
              )}
              <button
                type="button"
                onClick={() => fileInput.current?.click()}
                disabled={uploading}
                className="group flex w-full cursor-pointer items-center gap-3 rounded-lg border border-dashed border-border-strong bg-surface-2/60 px-3 py-2.5 text-left transition-colors hover:border-primary hover:bg-primary-soft disabled:cursor-wait"
              >
                <span className="grid size-8 shrink-0 place-items-center rounded-md bg-surface text-muted shadow-xs group-hover:text-primary"><UploadCloud className="size-4" /></span>
                <span className="min-w-0">
                  <span className="block text-xs font-medium text-foreground">
                    {uploading ? "Uploading…" : isAdmin ? uploadVisibility === "shared" ? "Upload shared documents" : "Upload a restricted document" : "Upload department documents"}
                  </span>
                  <span className="block text-[11px] leading-snug text-muted">PDF, Office, text, images, audio, video · up to {maxMb} MB each</span>
                </span>
              </button>
              <input ref={fileInput} type="file" accept={ACCEPT} multiple hidden onChange={(e) => void onUpload(e.target.files)} />
              <input ref={replaceInput} type="file" accept={ACCEPT} hidden onChange={(e) => void onReplace(e.target.files)} />
            </>
          )}
        </div>

        <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto">
          <DatabaseAnalysisPanel />

          <div className="space-y-3 p-4">
            {ingestion?.mode === "worker" &&
              ingestion.workers_online === 0 &&
              docs?.some((d) => d.status === "pending" || d.status === "processing") && (
                <Alert tone="warning" title="Document worker is offline">
                  Queued documents won't be processed until an administrator starts it (<code>python -m app.worker</code>).
                </Alert>
              )}

            {financialQueue && financialQueue.length > 0 && (
              <section className="rounded-lg border border-warning/30 bg-warning-soft p-3" aria-label="Finance extraction review queue">
                <p className="text-xs font-semibold text-warning-text">{financialQueue.length} pending finance review{financialQueue.length === 1 ? "" : "s"}</p>
                <ul className="mt-2 grid gap-1.5">
                  {financialQueue.map((item) => (
                    <li key={item.record_id} className="flex items-center justify-between gap-2 text-xs text-foreground-2">
                      <span className="min-w-0 truncate">{item.filename} · {item.profile.replaceAll("_", " ")} · {item.exception_count} exception{item.exception_count === 1 ? "" : "s"}</span>
                      {item.can_review ? (
                        <Button size="sm" className="h-7" onClick={() => {
                          setFinancialProfile(item.profile);
                          void onExtractFinancialFields(item.document_id, item.profile);
                        }}>Review</Button>
                      ) : <span className="text-muted">Read only</span>}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {docs && docs.length > 0 && (
              <div className="flex gap-2">
                <Input
                  type="search"
                  icon={<Search />}
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search documents"
                  aria-label="Search documents"
                  className="h-8 text-xs"
                />
                <Select
                  size="sm"
                  className="w-[150px] shrink-0"
                  aria-label="Filter by status"
                  value={statusFilter}
                  onValueChange={(v) => setStatusFilter(v as StatusFilter)}
                  options={STATUS_FILTERS.map((f) => ({ value: f.value, label: f.label }))}
                />
              </div>
            )}

            {!docs ? (
              <div className="grid gap-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-16 w-full" />)}</div>
            ) : docs.length === 0 ? (
              <EmptyState
                icon={<FolderOpen />}
                title="Your library is empty"
                description={canUpload ? "Upload documents to make them available in every chat." : "No documents are currently shared with you."}
              />
            ) : shownDocs.length === 0 ? (
              <EmptyState
                icon={<Search />}
                title="No documents match"
                action={<Button size="sm" variant="ghost" onClick={() => { setQuery(""); setStatusFilter("all"); }}>Clear filters</Button>}
              />
            ) : (
              <ul className="grid grid-cols-[minmax(0,1fr)] gap-2">
                {shownDocs.map((doc) => (
                  <li key={doc.id} className={cn("min-w-0 rounded-lg border bg-surface p-3 transition-colors", focusDoc?.id === doc.id ? "border-primary ring-[3px] ring-ring" : "border-border hover:border-border-strong")}>
                    <div className="flex items-start gap-2.5">
                      <span className={cn("grid size-8 shrink-0 place-items-center rounded-md", doc.status === "failed" ? "bg-danger-soft text-danger-text" : "bg-primary-soft text-primary")}>
                        <FileText className="size-4" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-start justify-between gap-2">
                          <p className="truncate text-[13px] leading-5 font-medium text-foreground" title={doc.filename}>{doc.filename}</p>
                          <Badge dot={doc.status !== "pending" && doc.status !== "processing"} tone={doc.status === "ready" ? "success" : doc.status === "failed" ? "danger" : "warning"}>
                            {(doc.status === "processing" || doc.status === "pending") && <Loader2 className="animate-spin" />}
                            {STATUS_LABEL[doc.status]}
                          </Badge>
                        </div>
                        <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-[11px] text-muted">
                          {doc.visibility !== "shared" && (
                            <span className="inline-flex items-center gap-1 text-warning-text" title={`${VISIBILITY[doc.visibility].title}, so other users' chats don't use it`}>
                              <Lock className="size-3" />{VISIBILITY[doc.visibility].label} ·
                            </span>
                          )}
                          {[
                            doc.page_count !== null && UNIT[doc.file_format] ? plural(doc.page_count, UNIT[doc.file_format]!) : null,
                            doc.status === "ready" ? plural(doc.chunk_count, "passage") : null,
                            formatSize(doc.size_bytes),
                            timeAgo(doc.updated_at),
                          ].filter(Boolean).join(" · ")}
                        </p>
                      </div>
                    </div>

                    {doc.error_message && doc.status !== "ready" && (
                      <p className={cn("mt-2 rounded-md px-2.5 py-1.5 text-[11px] leading-relaxed", doc.status === "failed" ? "bg-danger-soft text-danger-text" : "bg-surface-2 text-foreground-2")}>{doc.error_message}</p>
                    )}

                    {(doc.searchable || doc.can_modify || doc.source_system === "file_server") && (
                      <div className="mt-2.5 flex items-center gap-1.5">
                        {doc.searchable && (
                          <Button
                            size="sm"
                            variant={focusDoc?.id === doc.id ? "primary" : "soft"}
                            className="h-7 px-2.5"
                            title="Ask questions about this document only"
                            aria-pressed={focusDoc?.id === doc.id}
                            onClick={() => { setFocusDoc(focusDoc?.id === doc.id ? null : { id: doc.id, filename: doc.filename }); setView("chat"); }}
                          >
                            <MessageSquare className="size-3.5" />
                            {focusDoc?.id === doc.id ? "Asking this doc" : "Ask"}
                          </Button>
                        )}
                        {doc.can_modify && doc.status === "failed" && (
                          <Button size="sm" className="h-7 px-2.5" onClick={() => void onRetry(doc)}><RotateCw className="size-3.5" /> Retry</Button>
                        )}
                        {doc.can_modify && (doc.status === "pending" || doc.status === "processing") && (
                          <Button size="sm" variant="ghost" className="h-7 px-2.5" onClick={() => void onCancel(doc)}><X className="size-3.5" /> Cancel</Button>
                        )}
                        {financialBusy === doc.id && <Loader2 className="size-3.5 animate-spin text-muted" aria-label="Extracting fields" />}
                        <span className="flex-1" />
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button size="icon-xs" variant="ghost" aria-label={`Actions for ${doc.filename}`}><MoreHorizontal /></Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent className="w-56">
                            {doc.status === "ready" && (
                              <>
                                <DropdownMenuLabel>Extract fields</DropdownMenuLabel>
                                {([["auto", "Auto detect"], ["invoice", "Invoice"], ["purchase_order", "Purchase order"], ["form", "Generic form"]] as const).map(([value, label]) => (
                                  <DropdownMenuItem key={value} disabled={financialBusy === doc.id} onSelect={() => { setFinancialProfile(value); void onExtractFinancialFields(doc.id, value); }}>
                                    <ScanText /> {label}
                                  </DropdownMenuItem>
                                ))}
                                {(doc.source_system === "file_server" || doc.can_modify) && <DropdownMenuSeparator />}
                              </>
                            )}
                            {doc.source_system === "file_server" && (
                              <DropdownMenuItem onSelect={() => void onDownloadOriginal(doc)}><Download /> Download original</DropdownMenuItem>
                            )}
                            {doc.can_modify && (
                              <>
                                <DropdownMenuItem onSelect={() => { replaceTarget.current = doc.id; replaceInput.current?.click(); }}><UploadCloud /> Replace with new version</DropdownMenuItem>
                                <DropdownMenuSeparator />
                                <DropdownMenuItem danger onSelect={() => void onDelete(doc)}><Trash2 /> Delete</DropdownMenuItem>
                              </>
                            )}
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </div>
                    )}

                    <DocumentLifecyclePanel document={doc} />
                    {financialReview?.documentId === doc.id && financialReview.result.document_version !== doc.version && (
                      <p className="inline-alert inline-alert-warning" role="status">The source has a newer version. Extract it again before reviewing or exporting values.</p>
                    )}
                    {financialReview?.documentId === doc.id && financialReview.result.document_version === doc.version && (
                      <section className="financial-extraction" aria-label="Extracted financial fields">
                        <div className="financial-extraction-heading">
                          <strong>{financialReview.result.profile === "purchase_order" ? "Purchase order" : financialReview.result.profile === "invoice" ? "Invoice" : "Form"} fields</strong>
                          <span className={`pill pill-sm ${financialReview.result.review_status === "reviewed" ? "pill-success" : financialReview.result.status === "ready" ? "pill-warning" : "pill-error"}`}>
                            {financialReview.result.review_status === "reviewed" ? `Reviewed · revision ${financialReview.result.review?.revision}` : financialReview.result.review_status === "pending" ? "Draft · review required" : financialReview.result.status === "ready" ? "Ready to review" : "Needs review"}
                          </span>
                        </div>
                        <div className="financial-field-list">
                          {Object.entries(financialReview.result.fields).map(([name, field]) => (
                            <div key={name}>
                              <label>
                                <span>{name.replaceAll("_", " ")}</span>
                                {doc.can_modify ? (
                                  <input value={field.value ?? ""} onChange={(event) => setFinancialField(name, event.target.value)} aria-label={`Reviewed ${name.replaceAll("_", " ")}`} />
                                ) : <strong>{field.value ?? "Not extracted"}</strong>}
                              </label>
                              {extractionLocation(field.source) && <small className="financial-source">Source: {extractionLocation(field.source)}</small>}
                            </div>
                          ))}
                        </div>
                        {financialReview.result.exceptions.length > 0 && (
                          <ul className="financial-issues">
                            {financialReview.result.exceptions.map((issue, index) => (
                              <li key={`${issue.code}-${index}`}>
                                <span><strong>{issue.field}:</strong> {issue.message}</span>
                                {doc.can_modify && (
                                  <label className="financial-ack">
                                    <input
                                      type="checkbox"
                                      checked={financialReviewedExceptions.includes(index)}
                                      onChange={(event) => setFinancialReviewedExceptions((current) => event.target.checked
                                        ? [...new Set([...current, index])]
                                        : current.filter((value) => value !== index))}
                                    />
                                    I reviewed this exception against the source
                                  </label>
                                )}
                              </li>
                            ))}
                          </ul>
                        )}
                        {financialReview.result.line_items.length > 0 && (
                          <div className="financial-items-wrap">
                            <table className="financial-items">
                              <thead><tr><th>Item</th><th>Qty</th><th>Unit price</th><th>Amount</th><th>Currency</th><th>Source</th></tr></thead>
                              <tbody>{financialReview.result.line_items.map((item, index) => (
                                <tr key={`${index}-${item.description ?? "item"}`}>
                                  {(["description", "quantity", "unit_price", "amount", "currency"] as const).map((name) => (
                                    <td key={name}>
                                      {doc.can_modify ? (
                                        <input
                                          aria-label={`Line ${index + 1} ${name.replaceAll("_", " ")}`}
                                          value={item[name] ?? ""}
                                          onChange={(event) => setFinancialLineItem(index, name, event.target.value)}
                                        />
                                      ) : item[name] ?? "—"}
                                    </td>
                                  ))}
                                  <td>{extractionLocation(item.source) ?? "—"}</td>
                                </tr>
                              ))}</tbody>
                            </table>
                          </div>
                        )}
                        <p className="financial-review-note">{financialReview.result.note}</p>
                        {doc.can_modify && (
                          <div className="financial-review-controls">
                            <label className="field">
                              <span className="field-label">Review rationale</span>
                              <textarea value={financialRationale} maxLength={2000} onChange={(event) => setFinancialRationale(event.target.value)} />
                            </label>
                            <button
                              className="btn btn-primary btn-sm"
                              onClick={() => void onSaveFinancialReview(doc.id)}
                              disabled={financialBusy === doc.id || financialReviewedExceptions.length !== financialReview.result.exceptions.length || financialRationale.trim().length < 3}
                            >
                              {financialBusy === doc.id ? <Spinner size={12} /> : null}
                              Save reviewed values
                            </button>
                          </div>
                        )}
                        {financialReview.result.review_history.length > 0 && (
                          <details className="financial-history">
                            <summary>Review history ({financialReview.result.review_history.length} recent)</summary>
                            <ol>
                              {financialReview.result.review_history.map((revision) => (
                                <li key={revision.revision}>
                                  <strong>Revision {revision.revision}</strong> · {revision.reviewed_by_email} · {formatDate(revision.reviewed_at)}
                                  <p>{revision.rationale}</p>
                                </li>
                              ))}
                            </ol>
                          </details>
                        )}
                      </section>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </section>
      </div>
    </div>
  );
}

interface DisplayTurn {
  key: string;
  question: string;
  answer?: RagAnswer;
  streamingText?: string;
  unavailable?: number;
  error?: string;
  messageId?: number; // the saved assistant message, for feedback
  feedback?: -1 | 1 | null;
  ephemeral?: boolean;
}

function toTurns(messages: ConversationMessage[]): DisplayTurn[] {
  const turns: DisplayTurn[] = [];
  for (const m of messages) {
    if (m.role === "user") {
      turns.push({ key: `m${m.id}`, question: m.content });
    } else {
      const last = turns.at(-1);
      if (last && !last.answer && m.response)
        Object.assign(last, { answer: m.response, unavailable: m.sources_unavailable, messageId: m.id, feedback: m.feedback ?? null });
    }
  }
  return turns;
}

interface ChatProps {
  conversationId: string | null;
  title: string;
  readyCount: number;
  canManageLibrary: boolean;
  onAttach?: () => void;
  uploading: boolean;
  onOpenLibrary: () => void;
  focus: FocusDocument | null;
  onClearFocus: () => void;
  onCreated: (conversation: ConversationSummary) => void;
  onUpdated: (conversation: ConversationSummary) => void;
  onMissing: () => void;
}

function KnowledgeChat({
  conversationId, title, readyCount, canManageLibrary, onAttach, uploading, onOpenLibrary, focus, onClearFocus, onCreated, onUpdated, onMissing,
}: ChatProps) {
  const { toast } = useFeedback();
  const [turns, setTurns] = useState<DisplayTurn[]>([]);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState(false);
  const sending = useRef(false);
  const [recording, setRecording] = useState(false);
  const [microphoneRequestPending, setMicrophoneRequestPending] = useState(false);
  const [draft, setDraft] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);
  const voiceInput = useRef<HTMLInputElement>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const recordingStart = useRef(false);
  const mounted = useRef(true);
  const recordingStream = useRef<MediaStream | null>(null);
  const recordingChunks = useRef<Blob[]>([]);
  const recordingTimeout = useRef<number | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const justCreated = useRef<string | null>(null); // a chat created mid-send is already on screen
  // Per-chat model pick. "" = the organization's default (active) model, resolved by the server each time.
  const [models, setModels] = useState<ModelOption[]>([]);
  const [pick, setPick] = useState("");
  const keyOf = (provider: string | null, model: string | null) => (provider && model ? `${provider}\u0000${model}` : "");

  useEffect(() => {
    api.chatModels().then((r) => setModels(r.options)).catch(() => setModels([]));
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (recordingTimeout.current !== null) window.clearTimeout(recordingTimeout.current);
      const active = recorder.current;
      if (active && active.state !== "inactive") {
        active.onstop = null;
        active.stop();
      }
      recordingStream.current?.getTracks().forEach((track) => track.stop());
      recordingStream.current = null;
    };
  }, []);

  // Load the selected conversation from the server (permissions are re-checked there).
  useEffect(() => {
    if (!conversationId) {
      setTurns([]);
      setPick("");
      return;
    }
    // StrictMode deliberately re-runs mount effects in development. A new chat is created
    // before its first message is saved, so both effect passes must skip this initial GET;
    // clearing this sentinel in the first pass let the second GET return an empty/stale
    // snapshot that erased the in-flight answer.
    if (justCreated.current === conversationId) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setTurns([]);
    setLoading(true);
    api
      .getConversation(conversationId)
      .then((detail) => {
        if (cancelled) return;
        setTurns(toTurns(detail.messages));
        // Continue with the model this chat last used, if it is still available and not the default.
        const last = [...detail.messages].reverse().find((m) => m.response?.model);
        const key = keyOf(last?.response?.provider ?? null, last?.response?.model ?? null);
        const option = models.find((o) => keyOf(o.provider, o.model) === key);
        setPick(option && !option.is_default ? key : "");
      })
      .catch((err) => {
        if (cancelled) return;
        toast("error", "Could not open chat", describeError(err));
        if (err instanceof ApiError && err.status === 404) onMissing();
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Scroll only the conversation pane (never the page) to the newest message.
  useEffect(() => {
    const pane = scroller.current;
    if (pane && turns.length) pane.scrollTo({ top: pane.scrollHeight, behavior: "smooth" });
  }, [turns]);

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    const question = draft.trim();
    if (!question || sending.current || pending || loading || recording || microphoneRequestPending) return;
    setDraft("");
    if (input.current) input.current.style.height = "auto";
    const key = `pending-${Date.now()}`;
    setTurns((all) => [...all, { key, question }]);
    await send(key, question);
  }

  // A failed question is not saved on the server, so it is sent again from here.
  async function retry(turn: DisplayTurn) {
    if (pending || loading || recording || microphoneRequestPending) return;
    setTurns((all) => all.map((t) => (t.key === turn.key ? { key: t.key, question: t.question } : t)));
    await send(turn.key, turn.question);
  }

  async function send(key: string, question: string) {
    if (sending.current) return;
    sending.current = true;
    setPending(true);
    let id = conversationId;
    try {
      if (!id) {
        const created = await api.createConversation();
        id = created.id;
        justCreated.current = id;
        onCreated(created);
      }
      // Context comes from the saved conversation on the server, not from this browser.
      const chosen = models.find((o) => keyOf(o.provider, o.model) === pick);
      const result = await api.streamMessage(
        id,
        question,
        chosen ? { provider: chosen.provider, model: chosen.model } : undefined,
        focus ? [focus.id] : undefined,
        (delta) => setTurns((all) => all.map((t) => t.key === key && !t.answer ? { ...t, streamingText: (t.streamingText ?? "") + delta } : t)),
      );
      setTurns((all) =>
        all.map((t) =>
          t.key === key && !t.answer
            ? {
                ...t,
                streamingText: undefined,
                answer: result.assistant_message.response ?? undefined,
                unavailable: result.assistant_message.sources_unavailable,
                messageId: result.assistant_message.id,
                feedback: null,
              }
            : t,
        ),
      );
      onUpdated(result.conversation);
    } catch (err) {
      const message =
        err instanceof ApiError && err.code === "llm_not_configured"
          ? "No AI provider is active yet. Ask an administrator to activate one."
          : describeError(err);
      setTurns((all) => all.map((t) => (t.key === key && !t.answer ? { ...t, streamingText: undefined, error: message } : t)));
    } finally {
      sending.current = false;
      setPending(false);
      if (justCreated.current === id) justCreated.current = null;
      input.current?.focus();
    }
  }

  async function askByVoice(file: File) {
    if (pending || microphoneRequestPending) return;
    if (file.size > 8 * 1024 * 1024) {
      toast("error", "Voice question is too large", "Choose an audio file no larger than 8 MB.");
      return;
    }
    setPending(true);
    try {
      const chosen = models.find((o) => keyOf(o.provider, o.model) === pick);
      const result = await api.askSpokenQuestion(
        file,
        focus?.id,
        chosen ? { provider: chosen.provider, model: chosen.model } : undefined,
      );
      setTurns((all) => [
        ...all,
        {
          key: `voice-${Date.now()}`,
          question: result.transcript,
          answer: result.answer,
          ephemeral: true,
        },
      ]);
    } catch (err) {
      toast("error", "Could not answer voice question", describeError(err));
    } finally {
      setPending(false);
      input.current?.focus();
    }
  }

  function onVoiceFileChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (file) void askByVoice(file);
  }

  async function startVoiceRecording() {
    if (pending || recording || recordingStart.current) return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      toast("error", "Microphone unavailable", "This browser does not support secure microphone recording.");
      return;
    }
    recordingStart.current = true;
    setMicrophoneRequestPending(true);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!mounted.current) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      recordingStream.current = stream;
      const mimeType = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"].find((type) =>
        MediaRecorder.isTypeSupported(type),
      );
      const active = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      recorder.current = active;
      recordingChunks.current = [];
      active.ondataavailable = (event) => {
        if (event.data.size > 0) recordingChunks.current.push(event.data);
      };
      active.onstop = () => {
        const chunks = recordingChunks.current;
        recordingChunks.current = [];
        stream.getTracks().forEach((track) => track.stop());
        recordingStream.current = null;
        recorder.current = null;
        if (recordingTimeout.current !== null) window.clearTimeout(recordingTimeout.current);
        recordingTimeout.current = null;
        setRecording(false);
        const type = active.mimeType || "audio/webm";
        const extension = type.includes("ogg") ? "ogg" : type.includes("mp4") ? "m4a" : "webm";
        void askByVoice(new File(chunks, `voice-question.${extension}`, { type }));
      };
      active.start();
      setRecording(true);
      recordingTimeout.current = window.setTimeout(() => {
        if (active.state === "recording") active.stop();
      }, 120_000);
    } catch (err) {
      recordingStream.current?.getTracks().forEach((track) => track.stop());
      recordingStream.current = null;
      if (mounted.current) {
        setRecording(false);
        toast(
          "error",
          "Microphone unavailable",
          err instanceof DOMException && err.name === "NotAllowedError"
            ? "Allow microphone access in the browser, then try again."
            : "Check that a microphone is connected and available to the browser.",
        );
      }
    } finally {
      recordingStart.current = false;
      if (mounted.current) setMicrophoneRequestPending(false);
    }
  }

  function stopVoiceRecording() {
    const active = recorder.current;
    if (active?.state === "recording") active.stop();
  }

  async function rate(turn: DisplayTurn, value: -1 | 1) {
    if (!conversationId || turn.messageId === undefined) return;
    const next = turn.feedback === value ? null : value; // clicking the chosen rating again clears it
    const setFeedback = (feedback: -1 | 1 | null | undefined) =>
      setTurns((all) => all.map((t) => (t.key === turn.key ? { ...t, feedback } : t)));
    setFeedback(next);
    try {
      await api.rateMessage(conversationId, turn.messageId, next);
    } catch (err) {
      setFeedback(turn.feedback);
      toast("error", "Could not save feedback", describeError(err));
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <section className="flex min-h-0 min-w-0 flex-col bg-surface max-lg:h-[75dvh]">
      <h1 className="sr-only">Knowledge</h1>
      <header className="flex h-[52px] shrink-0 items-center gap-3 border-b border-border px-5">
        <MessageSquare className="size-4 shrink-0 text-muted" />
        <h3 className="min-w-0 truncate text-sm font-semibold text-foreground" title={title}>{title}</h3>
        {readyCount > 0 && <Badge className="ml-auto" tone="neutral"><FileText /> {readyCount} doc{readyCount === 1 ? "" : "s"} ready</Badge>}
      </header>
      <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto" ref={scroller}>
        {loading ? (
          <div className="mx-auto grid w-full max-w-3xl gap-4 px-5 py-8">
            <Skeleton className="ml-auto h-10 w-2/5 rounded-2xl" />
            <Skeleton className="h-28 w-4/5 rounded-2xl" />
          </div>
        ) : turns.length === 0 ? (
          <div className="mx-auto flex h-full w-full max-w-2xl flex-col items-center justify-center px-6 py-10 text-center">
            <span className="mb-4 grid size-12 place-items-center rounded-2xl bg-primary-soft text-primary shadow-xs ring-1 ring-primary/15">
              <Sparkles className="size-6" />
            </span>
            <h2 className="text-2xl font-semibold tracking-tight text-foreground">What would you like to know?</h2>
            <p className="mt-2 max-w-md text-[13px] leading-relaxed text-muted">
              {readyCount > 0
                ? "Ask about anything. I’ll use relevant company knowledge and link to the sources."
                : canManageLibrary
                  ? "Ask general questions now, or add company documents for source-backed answers."
                  : "Ask general questions now. Company documents will be used automatically when relevant."}
            </p>
            {readyCount > 0 && (
              <div className="mt-7 grid w-full gap-3 sm:grid-cols-2">
                {[
                  { title: "Summarize", detail: "Get the key takeaways", icon: ListChecks, prompt: "Summarize the key takeaways from my documents." },
                  { title: "Find action items", detail: "Turn information into next steps", icon: Target, prompt: "What action items and next steps are mentioned in my documents?" },
                ].map((suggestion) => (
                  <button
                    type="button"
                    key={suggestion.title}
                    onClick={() => { setDraft(suggestion.prompt); input.current?.focus(); }}
                    className="group flex cursor-pointer items-start gap-3 rounded-xl border border-border bg-surface p-4 text-left shadow-xs transition-all hover:-translate-y-px hover:border-primary/40 hover:shadow-sm"
                  >
                    <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-surface-2 text-foreground-2 group-hover:bg-primary-soft group-hover:text-primary"><suggestion.icon className="size-4" /></span>
                    <span className="min-w-0">
                      <span className="flex items-center gap-1 text-[13px] font-semibold text-foreground">{suggestion.title}<ArrowUpRight className="size-3.5 text-muted opacity-0 transition-opacity group-hover:opacity-100" /></span>
                      <span className="mt-0.5 block text-xs text-muted">{suggestion.detail}</span>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div className="mx-auto grid w-full max-w-3xl gap-6 px-5 py-6">
            {turns.map((turn) => (
              <div key={turn.key} className="grid gap-3">
                {turn.ephemeral && <p className="justify-self-end text-[11px] text-muted" role="note">Temporary voice question · not saved to chat</p>}
                <div className="max-w-[85%] justify-self-end rounded-2xl rounded-br-md bg-primary px-4 py-2.5 text-[13.5px] leading-relaxed whitespace-pre-wrap text-primary-foreground shadow-xs rtl:rounded-br-2xl rtl:rounded-bl-md">{turn.question}</div>
                {turn.answer ? (
                  <AnswerBubble
                    answer={turn.answer}
                    unavailable={turn.unavailable ?? 0}
                    providerLabel={models.find((o) => o.provider === turn.answer?.provider)?.provider_label}
                    rating={turn.feedback ?? null}
                    onRate={conversationId && turn.messageId !== undefined ? (value) => void rate(turn, value) : undefined}
                  />
                ) : turn.streamingText ? (
                  <div className="flex items-start gap-3 rounded-2xl rounded-tl-md border border-border bg-surface px-4 py-3 text-[13.5px] leading-relaxed text-foreground shadow-xs">
                    <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg bg-primary-soft text-primary"><Sparkles className="size-3.5" /></span>
                    <div className="min-w-0 flex-1"><Markdown text={turn.streamingText} /><span className="ml-1 inline-block size-1.5 animate-pulse rounded-full bg-primary" aria-label="Generating" /></div>
                  </div>
                ) : turn.error ? (
                  <Alert tone="danger" action={<Button size="sm" disabled={pending} onClick={() => void retry(turn)}><RotateCw /> Retry</Button>}>{turn.error}</Alert>
                ) : (
                  <div className="flex items-center gap-2.5 justify-self-start rounded-2xl rounded-bl-md border border-border bg-surface-2 px-4 py-3 text-[13px] text-muted">
                    <span className="flex gap-1" aria-hidden>
                      {[0, 1, 2].map((i) => <span key={i} className="size-1.5 animate-bounce rounded-full bg-primary/70" style={{ animationDelay: `${i * 120}ms` }} />)}
                    </span>
                    Thinking and checking relevant sources…
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
      <form className="shrink-0 px-4 pt-2 pb-4" onSubmit={submit}>
        <div className="mx-auto w-full max-w-3xl">
          {focus && (
            <div className="mb-2 inline-flex max-w-full items-center gap-2 rounded-full border border-primary/25 bg-primary-soft py-1 pr-1 pl-3 text-xs text-primary" role="status">
              <FileText className="size-3.5 shrink-0" />
              <span className="truncate">Asking about <strong title={focus.filename}>{focus.filename}</strong></span>
              <button type="button" className="grid size-5 shrink-0 cursor-pointer place-items-center rounded-full hover:bg-primary/15" onClick={onClearFocus} aria-label="Search the whole library again" title="Search the whole library">
                <X className="size-3" />
              </button>
            </div>
          )}
          <div className="rounded-2xl border border-border-strong bg-surface shadow-sm transition-[border-color,box-shadow] focus-within:border-primary focus-within:ring-[3px] focus-within:ring-ring">
            <textarea
              ref={input}
              rows={1}
              value={draft}
              onChange={(event) => { setDraft(event.target.value); event.target.style.height = "auto"; event.target.style.height = `${Math.min(event.target.scrollHeight, 180)}px`; }}
              maxLength={4000}
              placeholder={focus ? "Ask about this document…" : "Message InfraSpine AI…"}
              onKeyDown={onKeyDown}
              disabled={pending || loading || recording || microphoneRequestPending}
              aria-label="Question"
              className="block max-h-[180px] min-h-[52px] w-full resize-none border-0 bg-transparent px-4 pt-3.5 pb-1 text-[14px] leading-relaxed text-foreground shadow-none outline-none placeholder:text-muted focus:shadow-none disabled:cursor-not-allowed"
            />
            <div className="flex items-center gap-1 px-2 pb-2">
              <Tooltip content={onAttach ? "Upload documents, images, audio or video to the library" : "Choose a document from your library"}>
                <Button variant="ghost" size="icon-sm" aria-label={onAttach ? "Attach files" : "Choose library document"} disabled={uploading} onClick={onAttach ?? onOpenLibrary}><Paperclip /></Button>
              </Tooltip>
              <input
                ref={voiceInput}
                type="file"
                accept=".wav,.mp3,.m4a,.flac,.ogg,.opus,.aac,.webm,audio/*"
                className="sr-only"
                aria-label="Choose a short audio question"
                onChange={onVoiceFileChange}
              />
              <Tooltip content="Ask with an audio file. Transcribed locally; audio is discarded and not saved to chat history.">
                <Button type="button" variant="ghost" size="icon-sm" aria-label="Ask by audio file" disabled={pending || recording || microphoneRequestPending} onClick={() => voiceInput.current?.click()}>
                  <FileAudio />
                </Button>
              </Tooltip>
              <Tooltip content={recording ? "Stop and ask" : "Record a question (max 2 minutes). Transcribed locally; audio is discarded."}>
                <Button
                  type="button"
                  variant={recording ? "danger" : "ghost"}
                  size={recording ? "sm" : "icon-sm"}
                  aria-label={recording ? "Stop recording and ask" : "Record question"}
                  aria-pressed={recording}
                  disabled={pending || microphoneRequestPending || (!recording && loading)}
                  onClick={() => recording ? stopVoiceRecording() : void startVoiceRecording()}
                >
                  {recording ? <><Square className="size-3 fill-current" /> Stop &amp; ask</> : microphoneRequestPending ? <Loader2 className="animate-spin" /> : <Mic />}
                </Button>
              </Tooltip>
              {recording && <span className="flex items-center gap-1.5 text-xs text-danger-text" role="status"><span className="size-2 animate-pulse rounded-full bg-danger" /> Recording</span>}
              <span className="flex-1" />
              {models.length > 1 && (
                <Select
                  size="sm"
                  className="h-8 w-auto max-w-[240px] border-transparent bg-transparent shadow-none hover:bg-surface-2"
                  aria-label="Model for this chat"
                  value={pick}
                  onValueChange={setPick}
                  disabled={pending}
                  options={models.map((o) => ({
                    value: o.is_default ? "" : keyOf(o.provider, o.model),
                    label: `${o.provider_label} · ${o.model_label}${o.is_default ? " (default)" : ""}`,
                  }))}
                />
              )}
              <Button type="submit" variant="primary" size="icon-sm" className="rounded-lg" disabled={pending || recording || microphoneRequestPending || !draft.trim()} aria-label="Send" title="Send message">
                {pending ? <Loader2 className="animate-spin" /> : <ArrowUp />}
              </Button>
            </div>
          </div>
          <p className="mt-2 text-center text-[11px] text-muted">
            <kbd className="font-sans">Enter</kbd> to send · <kbd className="font-sans">Shift + Enter</kbd> for a new line · Relevant company facts include citations
          </p>
        </div>
      </form>
    </section>
  );
}

function AnswerBubble({
  answer,
  unavailable = 0,
  providerLabel,
  rating = null,
  onRate,
}: {
  answer: RagAnswer;
  unavailable?: number;
  providerLabel?: string;
  rating?: -1 | 1 | null;
  onRate?: (value: -1 | 1) => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const bySource = new Map(answer.citations.map((c) => [c.source_id, c]));
  const toggle = (c: Citation) => c.available && setOpen(open === c.source_id ? null : c.source_id);
  const answerModeLabel: Partial<Record<RagAnswer["strategy"], string>> = {
    general: "General knowledge",
    web_research: "Live web research",
    hybrid_research: "Web + company sources",
    web_image_search: "Public image source",
    image_generation: "AI-generated image",
    image_edit: "Edited image",
    identity: "Account context",
    library_inventory: "Library inventory",
    upload_capability: "Library access",
    web_capability: "Live web access",
  };
  const modeLabel = answerModeLabel[answer.strategy];

  return (
    <div className="flex items-start gap-3">
      <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg bg-primary-soft text-primary ring-1 ring-primary/15"><Sparkles className="size-3.5" /></span>
      <div className="min-w-0 flex-1 rounded-2xl rounded-tl-md border border-border bg-surface px-4 py-3 text-[13.5px] leading-relaxed text-foreground shadow-xs">
        {(answer.general_knowledge && !answer.insufficient_evidence) || answer.insufficient_evidence ? (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {answer.general_knowledge && !answer.insufficient_evidence && modeLabel && (
              <Badge tone="primary" title="How this response was prepared"><Info /> {modeLabel}</Badge>
            )}
            {answer.insufficient_evidence && <Badge tone="warning"><AlertTriangle /> {answer.strategy === "web_research" ? "Live lookup unavailable" : answer.strategy.includes("image") ? "Image unavailable" : "Insufficient document evidence"}</Badge>}
          </div>
        ) : null}
        <Markdown
          text={answer.answer}
          renderCitation={(tag, key) => {
            const source = bySource.get(tag);
            return source ? (
              <button
                key={key}
                className={cn(
                  "mx-0.5 inline-grid h-[18px] min-w-[18px] cursor-pointer place-items-center rounded-md px-1 align-[1px] text-[10.5px] font-semibold transition-colors",
                  !source.available ? "cursor-not-allowed bg-surface-2 text-muted line-through" : open === tag ? "bg-primary text-primary-foreground" : "bg-primary-soft text-primary hover:bg-primary hover:text-primary-foreground",
                )}
                title={source.available ? `${source.filename}, ${citationPlace(source)}` : "No longer available to you"}
                onClick={() => toggle(source)}
              >
                {tag.slice(1)}
              </button>
            ) : null;
          }}
        />
        {answer.strategy === "whole_document" && answer.citations.length > 0 && (
          <p className="mt-2 text-xs text-muted">Answered from the whole document, not just the closest passages.</p>
        )}
        {answer.chart && <ChartView chart={answer.chart} sources={bySource} />}
        {answer.page_images.length > 0 && (
          <div className="mt-3 grid grid-cols-[repeat(auto-fill,minmax(120px,1fr))] gap-2">
            {answer.page_images.map((img) => (
              <a key={img.page_number} className="group overflow-hidden rounded-lg border border-border bg-surface-2" href={img.url} target="_blank" rel="noopener" title="Open full size">
                <img className="aspect-[3/4] w-full object-cover object-top transition-transform group-hover:scale-[1.02]" src={`${img.url}?width=480`} alt={`${img.filename}, page ${img.page_number}`} loading="lazy" />
                <span className="block border-t border-border px-2 py-1 text-[11px] text-muted">Page {img.page_number}</span>
              </a>
            ))}
          </div>
        )}
        {answer.citations.length > 0 && (
          <div className="mt-3 border-t border-border pt-3">
            <p className="mb-1.5 text-[10.5px] font-semibold tracking-[0.08em] text-muted uppercase">Sources</p>
            <div className="grid gap-1">
              {answer.citations.map((c: Citation) => (
                <div key={c.source_id} className={cn("rounded-lg border transition-colors", open === c.source_id ? "border-primary/30 bg-primary-soft/40" : "border-transparent")}>
                  <button
                    className="flex w-full min-w-0 cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs hover:bg-surface-2 disabled:cursor-default disabled:hover:bg-transparent"
                    onClick={() => toggle(c)}
                    disabled={!c.available}
                    aria-expanded={open === c.source_id}
                  >
                    <span className={cn("grid h-[18px] min-w-[18px] shrink-0 place-items-center rounded-md px-1 text-[10.5px] font-semibold", c.available ? "bg-primary-soft text-primary" : "bg-surface-2 text-muted")}>{c.source_id.slice(1)}</span>
                    <FileText className="size-3.5 shrink-0 text-muted" />
                    <span className={cn("truncate font-medium", c.available ? "text-foreground" : "text-muted italic")}>{c.filename}</span>
                    <span className="shrink-0 text-muted">· {c.available ? citationPlace(c) : "no longer available"}</span>
                    {c.available && <ChevronDown className={cn("ml-auto size-3.5 shrink-0 text-muted transition-transform", open === c.source_id && "rotate-180")} />}
                  </button>
                  {open === c.source_id && (
                    <div className="px-3 pb-3">
                      <blockquote className="border-l-2 border-primary/40 pl-3 text-xs leading-relaxed text-foreground-2 italic rtl:border-r-2 rtl:border-l-0 rtl:pr-3 rtl:pl-0">{c.snippet}…</blockquote>
                      <div className="mt-2 flex flex-wrap gap-3">
                        {(isPageCitation(c) || c.locator?.kind === "image") && (
                          <a className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline" href={`/api/rag/documents/${c.document_id}/pages/${c.page_number}/image`} target="_blank" rel="noopener">
                            {isPageCitation(c) ? `Open page ${c.page_number} as image` : "Open image"} <ExternalLink className="size-3" />
                          </a>
                        )}
                        {c.available && mediaPreviewHref(c) && (
                          <a className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline" href={mediaPreviewHref(c) ?? undefined} target="_blank" rel="noopener noreferrer">
                            Open original media at {citationPlace(c)} <ExternalLink className="size-3" />
                          </a>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
        {unavailable > 0 && (
          <p className="mt-2 text-xs text-muted">
            {plural(unavailable, "source document")} {unavailable === 1 ? "is" : "are"} no longer available to you, so
            {unavailable === 1 ? " its" : " their"} text is hidden.
          </p>
        )}
        {(
          <div className="mt-3 flex items-center justify-between gap-3">
            {answer.model ? (
              <p className="truncate text-[11px] text-muted" title="AI provider and model that produced this answer">
                {providerLabel ?? answer.provider} · {answer.model?.replace(/^models\//, "")}
                {!!answer.memories_applied && (
                  <span title="Your saved preferences and context shaped this answer. They are not sources.">
                    {" "}· personalised with {answer.memories_applied} {answer.memories_applied === 1 ? "memory" : "memories"}
                  </span>
                )}
              </p>
            ) : <span />}
            <div className="flex shrink-0 items-center gap-1">
            <CopyButton text={answer.answer} compact />
            {onRate && (
              <div className="flex shrink-0 gap-0.5" role="group" aria-label="Rate this answer">
                <Tooltip content="Helpful">
                  <Button size="icon-xs" variant={rating === 1 ? "soft" : "ghost"} aria-pressed={rating === 1} aria-label="Helpful" onClick={() => onRate(1)}><ThumbsUp className="size-3.5" /></Button>
                </Tooltip>
                <Tooltip content="Not helpful">
                  <Button size="icon-xs" variant={rating === -1 ? "soft" : "ghost"} aria-pressed={rating === -1} aria-label="Not helpful" onClick={() => onRate(-1)}><ThumbsDown className="size-3.5" /></Button>
                </Tooltip>
              </div>
            )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
