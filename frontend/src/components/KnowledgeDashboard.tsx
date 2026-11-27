import { useCallback, useEffect, useState } from "react";
import { Activity, ArrowUpRight, CheckCircle2, Clock3, FileText, RefreshCw, Server, ShieldCheck, TriangleAlert } from "lucide-react";
import { api, describeError } from "../api";
import type { IngestionStatus, RagDocument, TraceSummary } from "../types";
import { Alert, Badge, Button, Skeleton } from "./ui";

interface Props {
  documents: RagDocument[] | null;
  ingestion: IngestionStatus | null;
  isAdmin: boolean;
  onRefresh: () => Promise<void>;
  onOpenLibrary: (filter: "all" | "ready" | "working" | "failed") => void;
}

function latency(value: number | null): string {
  if (value === null) return "No observations";
  return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(1)} s`;
}

export function KnowledgeDashboard({ documents, ingestion, isAdmin, onRefresh, onOpenLibrary }: Props) {
  const [summary, setSummary] = useState<TraceSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [revision, setRevision] = useState(0);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  useEffect(() => {
    if (!isAdmin) return;
    let cancelled = false;
    setError(null);
    api.traces({ days: 7, status: "all", outcome: "all" }).then((result) => {
      if (!cancelled) { setSummary(result.summary); setUpdatedAt(new Date()); }
    }).catch((err) => { if (!cancelled) setError(describeError(err)); });
    return () => { cancelled = true; };
  }, [isAdmin, revision]);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try { await onRefresh(); setRevision((value) => value + 1); }
    finally { setRefreshing(false); }
  }, [onRefresh]);

  const docs = documents ?? [];
  const ready = docs.filter((doc) => doc.searchable).length;
  const working = docs.filter((doc) => ["pending", "processing"].includes(doc.status));
  const failed = docs.filter((doc) => doc.status === "failed");
  const updating = docs.filter((doc) => doc.searchable && doc.indexed_version !== doc.version).length;
  const formats = [...new Set(docs.map((doc) => doc.file_format))].map((format) => ({
    format, count: docs.filter((doc) => doc.file_format === format).length,
  })).sort((a, b) => b.count - a.count);
  const daily = [...(summary?.daily ?? [])].sort((a, b) => a.day.localeCompare(b.day));
  const maxQuestions = Math.max(1, ...daily.map((day) => day.questions));
  const workerOffline = ingestion?.mode === "worker" && ingestion.workers_online === 0;

  return (
    <section className="scrollbar-thin min-h-0 flex-1 overflow-y-auto bg-surface-2/40 p-4 sm:p-7" aria-label="Knowledge dashboard">
      <div className="mx-auto max-w-6xl space-y-6">
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div><h2 className="text-xl font-semibold tracking-tight text-foreground">Knowledge overview</h2><p className="mt-1 text-sm text-muted">Your accessible library and {isAdmin ? "organization-wide assistant activity" : "document processing status"}.</p></div>
          <Button size="sm" onClick={() => void refresh()} loading={refreshing}><RefreshCw /> Refresh</Button>
        </header>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {[
            { label: "Accessible documents", value: docs.length, icon: FileText, filter: "all" as const, note: "Limited to your current permissions" },
            { label: "Searchable", value: ready, icon: CheckCircle2, filter: "ready" as const, note: updating ? `${updating} serving a previous version during indexing` : "Available for cited answers" },
            { label: "In progress", value: working.length, icon: Clock3, filter: "working" as const, note: "Queued or currently processing" },
            { label: "Need attention", value: failed.length, icon: TriangleAlert, filter: "failed" as const, note: "Inspect the error before retrying" },
          ].map((metric) => <button key={metric.label} type="button" onClick={() => onOpenLibrary(metric.filter)} className="group rounded-xl border border-border bg-surface p-4 text-left shadow-xs transition-colors hover:border-primary/40 focus-visible:outline-2 focus-visible:outline-primary">
            <span className="flex items-center justify-between text-xs font-medium text-muted"><span className="flex items-center gap-2"><metric.icon className="size-4" />{metric.label}</span><ArrowUpRight className="size-3.5" /></span>
            {documents === null ? <Skeleton className="my-3 h-8 w-16" /> : <strong className="mt-3 block text-3xl font-semibold tracking-tight text-foreground">{metric.value}</strong>}
            <span className="mt-2 block text-[11px] text-muted">{metric.note}</span>
          </button>)}
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          <section className="rounded-xl border border-border bg-surface p-5 shadow-xs" aria-label="Indexing health">
            <div className="flex items-center justify-between gap-2"><h3 className="flex items-center gap-2 text-sm font-semibold"><Server className="size-4 text-muted" /> Indexing health</h3>
              <Badge tone={workerOffline ? "warning" : ingestion ? "success" : "neutral"}>{!ingestion ? "Unknown" : ingestion.mode === "embedded" ? "API-managed" : workerOffline ? "Worker offline" : `${ingestion.workers_online} worker${ingestion.workers_online === 1 ? "" : "s"} online`}</Badge>
            </div>
            <p className="mt-3 text-xs leading-relaxed text-muted">{workerOffline ? "New uploads cannot finish until a document worker is available. Already indexed documents remain searchable." : "Documents become searchable after extraction and indexing finish. Replacements keep the previous index available while processing."}</p>
            {working.length ? <ul className="mt-4 divide-y divide-border">{working.slice(0, 5).map((doc) => <li key={doc.id} className="flex items-center justify-between gap-3 py-2 text-xs"><span className="truncate" title={doc.filename}>{doc.filename}</span><Badge>{doc.status === "pending" ? "Queued" : "Processing"}</Badge></li>)}</ul> : <p className="mt-4 text-xs text-muted">{documents === null ? "Loading queue…" : "No documents waiting in your library."}</p>}
          </section>
          <section className="rounded-xl border border-border bg-surface p-5 shadow-xs" aria-label="Library composition">
            <h3 className="flex items-center gap-2 text-sm font-semibold"><ShieldCheck className="size-4 text-muted" /> Your library</h3>
            <p className="mt-3 text-xs text-muted">Shared, restricted and private sources appear only when your account can access them.</p>
            <div className="mt-4 flex flex-wrap gap-2">{formats.map(({ format, count }) => <Badge key={format}>{format.toUpperCase()} <span className="font-normal">{count}</span></Badge>)}</div>
            {!formats.length && <p className="mt-3 text-xs text-muted">{documents === null ? "Loading formats…" : "No accessible documents yet."}</p>}
            <Button variant="link" size="sm" className="mt-4" onClick={() => onOpenLibrary("all")}>Browse library <ArrowUpRight /></Button>
          </section>
        </div>
        {failed.length > 0 && <section className="rounded-xl border border-warning/30 bg-warning-soft p-4" aria-label="Document errors"><h3 className="text-sm font-semibold text-warning-text">Documents need attention</h3><ul className="mt-3 space-y-3">{failed.slice(0, 5).map((doc) => <li key={doc.id}><p className="truncate text-xs font-medium">{doc.filename}</p><p className="mt-1 text-xs text-muted">{doc.error_message || "Processing failed. Open the library for details and recovery actions."}</p></li>)}</ul><Button size="sm" className="mt-4" onClick={() => onOpenLibrary("failed")}>Review failed documents</Button></section>}
        {isAdmin && <section className="space-y-4" aria-label="Assistant activity">
          <header className="flex flex-wrap items-center justify-between gap-2"><h3 className="flex items-center gap-2 text-sm font-semibold"><Activity className="size-4 text-muted" /> Assistant activity · last 7 days</h3>{updatedAt && <span className="text-[11px] text-muted">Fetched {updatedAt.toLocaleTimeString()}</span>}</header>
          {error && <Alert tone="warning" title="Activity could not be refreshed">{error}{summary && " Previous observations are shown below."}</Alert>}
          {!summary && !error ? <Skeleton className="h-40 w-full" /> : summary && <>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{[
              ["Questions", summary.questions.toLocaleString()], ["Errors", summary.errors.toLocaleString()],
              ["Median response time", latency(summary.latency_p50_ms)], ["95th percentile", latency(summary.latency_p95_ms)],
            ].map(([label, value]) => <div key={label} className="rounded-xl border border-border bg-surface p-4"><p className="text-xs text-muted">{label}</p><p className="mt-2 text-xl font-semibold">{value}</p></div>)}</div>
            <div className="grid gap-4 lg:grid-cols-2">
              <div className="rounded-xl border border-border bg-surface p-5"><h4 className="text-xs font-semibold">Daily questions (UTC)</h4>{daily.length ? <ul className="mt-4 space-y-2">{daily.map((day) => <li key={day.day} className="grid grid-cols-[80px_1fr_35px] items-center gap-3 text-xs"><span className="text-muted">{day.day.slice(5)}</span><span className="h-3 overflow-hidden rounded bg-surface-2"><span className="block h-full rounded bg-primary/70" style={{ width: `${day.questions / maxQuestions * 100}%` }} /></span><span className="text-right tabular-nums">{day.questions}</span></li>)}</ul> : <p className="mt-4 text-xs text-muted">No observed questions in this period.</p>}</div>
              <div className="rounded-xl border border-border bg-surface p-5"><h4 className="text-xs font-semibold">Observed outcomes</h4><dl className="mt-4 grid grid-cols-2 gap-y-3 text-xs"><dt className="text-muted">Insufficient-evidence replies</dt><dd className="text-right tabular-nums">{summary.refused}</dd><dt className="text-muted">General knowledge replies</dt><dd className="text-right tabular-nums">{summary.general}</dd><dt className="text-muted">Helpful / not helpful</dt><dd className="text-right tabular-nums">{summary.helpful} / {summary.not_helpful}</dd><dt className="text-muted">Recorded input / output tokens</dt><dd className="text-right tabular-nums">{summary.prompt_tokens.toLocaleString()} / {summary.completion_tokens.toLocaleString()}</dd></dl><p className="mt-4 text-[11px] leading-relaxed text-muted">These are observed outcomes, not measured answer accuracy. Token totals include only usage returned by providers.</p></div>
            </div>
          </>}
        </section>}
      </div>
    </section>
  );
}
