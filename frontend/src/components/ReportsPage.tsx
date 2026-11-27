import { useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import { useFeedback } from "./Feedback";
import type { UsageReport } from "../types";
import { AlertTriangle, BarChart3, CalendarRange, Coins, Cpu, Download, MessageSquare, Printer, RefreshCw, ShieldQuestion, ThumbsUp, Timer } from "lucide-react";
import { cn } from "../lib/utils";
import { Alert, Button, Card, CardHeader, EmptyState, PageHeader, Select, Skeleton, Stat, Table } from "./ui";

const RANGES = [7, 30, 90, 365] as const;
const EXPORTS = [
  ["csv", "CSV"], ["pdf", "PDF"], ["docx", "Word"], ["xlsx", "Excel"], ["pptx", "PowerPoint"],
] as const;

export function ReportsPage() {
  const { toast } = useFeedback();
  const [days, setDays] = useState<number>(30);
  const [report, setReport] = useState<UsageReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [exporting, setExporting] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let timedOut = false;
    setBusy(true);
    setReport(null);
    setError(null);
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 30_000);
    void api.usageReport(days, controller.signal)
      .then((value) => setReport(value))
      .catch((err) => {
        if (timedOut) setError("The usage report timed out. Check service health, then try again.");
        else if (!controller.signal.aborted) setError(describeError(err));
      })
      .finally(() => {
        window.clearTimeout(timeout);
        if (timedOut || !controller.signal.aborted) setBusy(false);
      });
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [days, reload]);

  async function exportReport(format: (typeof EXPORTS)[number][0]) {
    if (!report || exporting) return;
    setExporting(format);
    try {
      const blob = await api.usageReportDownload(days, format);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `infraspine-usage-${days}-days.${format}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      // Revoking right after click() can cancel the download in some browsers; give it time to start.
      window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
      setError(null);
      toast("success", "Report downloaded", `${link.download} (${Math.max(1, Math.round(blob.size / 1024))} KB) was saved by your browser, usually to Downloads.`);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setExporting(null);
    }
  }

  const summary = report?.summary;
  const maxQuestions = Math.max(1, ...(report?.daily.map((row) => row.questions) ?? []));
  const feedbackTotal = summary ? summary.helpful + summary.not_helpful : 0;
  return (
    <>
      <PageHeader
        title="Usage reports"
        description="Question volume, reliability, evidence gaps, feedback and model speed. No question or answer text is ever included."
        actions={<div className="report-actions flex flex-wrap items-center gap-2">
          <Select size="sm" className="w-36" aria-label="Report period" value={String(days)} onValueChange={(value) => setDays(Number(value))} options={RANGES.map((range) => ({ value: String(range), label: `Last ${range} days` }))} />
          <div className="inline-flex overflow-hidden rounded-md border border-border shadow-xs" role="group" aria-label="Export">
            {EXPORTS.map(([format, label], index) => (
              <button
                key={format}
                type="button"
                className={cn("inline-flex h-8 cursor-pointer items-center gap-1 bg-surface px-2.5 text-xs font-medium text-foreground-2 outline-none transition-colors hover:bg-surface-2 hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50", index > 0 && "border-l border-border")}
                onClick={() => void exportReport(format)}
                disabled={!report || Boolean(exporting) || busy}
                aria-label={`Export report as ${label}`}
              >
                {exporting === format ? "Preparing…" : <>{index === 0 && <Download className="size-3.5" />}{label}</>}
              </button>
            ))}
          </div>
          <Button size="sm" variant="ghost" onClick={() => window.print()} disabled={!report || busy}><Printer /> Print</Button>
        </div>}
      />
      {error && <Alert tone="danger" className="mb-5" action={!busy && !report ? <Button size="sm" onClick={() => setReload((current) => current + 1)}><RefreshCw /> Retry report</Button> : undefined}>{error}</Alert>}
      {busy && <div className="grid gap-3" aria-live="polite"><p className="text-xs text-muted">Preparing report…</p><div className="grid grid-cols-2 gap-3 lg:grid-cols-3">{Array.from({ length: 6 }, (_, index) => <Skeleton key={index} className="h-[88px]" />)}</div><Skeleton className="h-64" /></div>}
      {report && summary && <>
        <p className="mb-3 flex items-center gap-1.5 text-xs text-muted"><CalendarRange className="size-3.5" /> {formatDate(report.period_start)} – {formatDate(report.period_end)} · generated {formatDate(report.generated_at)}</p>
        <section className="report-metrics mb-5 grid grid-cols-2 gap-3 lg:grid-cols-3">
          <Stat icon={<MessageSquare />} label="Questions" value={summary.questions.toLocaleString()} hint={`${summary.llm_calls.toLocaleString()} model calls`} />
          <Stat icon={<AlertTriangle />} label="Errors" value={<span className={summary.errors ? "text-danger-text" : "text-success-text"}>{summary.errors.toLocaleString()}</span>} hint={summary.questions ? `${((summary.errors / summary.questions) * 100).toFixed(1)}% of questions` : "No questions recorded"} />
          <Stat icon={<ShieldQuestion />} label="Evidence gaps" value={summary.refused.toLocaleString()} hint="Answers withheld for lack of evidence" />
          <Stat icon={<ThumbsUp />} label="Helpful feedback" value={`${summary.helpful.toLocaleString()} / ${feedbackTotal.toLocaleString()}`} hint={`${summary.not_helpful.toLocaleString()} not helpful`} />
          <Stat icon={<Timer />} label="Median latency" value={summary.latency_p50_ms === null ? "—" : `${summary.latency_p50_ms.toLocaleString()} ms`} hint={summary.latency_p95_ms === null ? "p95 unavailable" : `p95 ${summary.latency_p95_ms.toLocaleString()} ms`} />
          <Stat icon={<Coins />} label="Tokens reported" value={(summary.prompt_tokens + summary.completion_tokens).toLocaleString()} hint={`${summary.prompt_tokens.toLocaleString()} prompt · ${summary.completion_tokens.toLocaleString()} completion`} />
        </section>

        <Card className="mb-5 overflow-hidden">
          <CardHeader
            icon={<BarChart3 />}
            title="Daily activity"
            description="Questions per day, with the share that ended in a service error."
            actions={<div className="flex items-center gap-3 text-[11px] text-muted"><span className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm bg-primary/70" />Questions</span><span className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm bg-danger" />Errors</span></div>}
          />
          {report.daily.length === 0 ? <EmptyState icon={<BarChart3 />} title="No activity in this period" description="Pick a longer period, or check back after people start asking questions." /> : (
            <div className="grid max-h-[420px] gap-1.5 overflow-y-auto px-5 py-4">
              {report.daily.map((row) => (
                <div className="grid grid-cols-[72px_minmax(0,1fr)_40px] items-center gap-3 text-xs sm:grid-cols-[92px_minmax(0,1fr)_40px_150px]" key={row.day}>
                  <time className="text-muted tabular-nums">{row.day}</time>
                  <div className="relative h-2.5 overflow-hidden rounded-full bg-surface-2" role="img" aria-label={`${row.day}: ${row.questions} questions, ${row.errors} errors`}>
                    <span className="absolute inset-y-0 left-0 rounded-full bg-primary/70" style={{ width: `${Math.max(2, (row.questions / maxQuestions) * 100)}%` }} />
                    <span className="absolute inset-y-0 left-0 rounded-full bg-danger" style={{ width: `${(row.errors / maxQuestions) * 100}%` }} />
                  </div>
                  <strong className="text-right font-semibold tabular-nums text-foreground">{row.questions}</strong>
                  <span className="hidden text-muted sm:block">{row.errors} errors · {row.refused} gaps</span>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card className="overflow-hidden">
          <CardHeader icon={<Cpu />} title="AI provider usage" description="Call totals and latency from metadata-only traces." />
          {report.providers.length === 0 ? <EmptyState icon={<Cpu />} title="No provider usage recorded" description="Calls to an AI provider appear here once answers are generated." /> : (
            <Table>
              <thead><tr><th>Provider</th><th>Model</th><th className="text-right">Calls</th><th className="text-right">Median latency</th><th className="text-right">p95 latency</th></tr></thead>
              <tbody>{report.providers.map((row) => (
                <tr key={`${row.provider}-${row.model}`}>
                  <td className="font-medium text-foreground">{row.provider}</td>
                  <td>{row.model ?? "—"}</td>
                  <td className="text-right tabular-nums">{row.calls.toLocaleString()}</td>
                  <td className="text-right tabular-nums">{row.latency_p50_ms === null ? "—" : `${row.latency_p50_ms.toLocaleString()} ms`}</td>
                  <td className="text-right tabular-nums">{row.latency_p95_ms === null ? "—" : `${row.latency_p95_ms.toLocaleString()} ms`}</td>
                </tr>
              ))}</tbody>
            </Table>
          )}
        </Card>
      </>}
    </>
  );
}
