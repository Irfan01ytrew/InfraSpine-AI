import type { ComponentType } from "react";
import { Activity, ArrowRight, Building2, CheckCircle2, Cpu, FileText, Gauge, MessageSquare, Users } from "lucide-react";
import { formatDate, formatNumber, timeAgo } from "../format";
import { cn } from "../lib/utils";
import type { AuditEntry, UsageReport } from "../types";
import { Button, Card } from "./ui";

export interface DashboardData {
  aiActive: boolean;
  aiLabel: string;
  departments: number;
  people: number;
  pendingInvites: number;
  readyDocs: number;
  /** Each live source is null when it could not be read, so the page shows "—" instead of a made-up number. */
  processingDocs: number | null;
  failedDocs: number | null;
  usage: UsageReport | null;
  openRequests: number | null;
  reviewQueue: number | null;
  activity: AuditEntry[] | null;
}

const show = (value: number | null) => (value === null ? "—" : formatNumber(value));

/** Admin overview built only from live API data. */
export function HomeDashboard({ data, onNavigate }: { data: DashboardData; onNavigate: (view: string) => void }) {
  const summary = data.usage?.summary ?? null;
  const answered = summary ? Math.max(0, summary.questions - summary.errors) : null;
  const successRate = summary && summary.questions > 0 ? Math.round((answered! / summary.questions) * 100) : null;
  const days = lastDays(14, data.usage?.daily ?? []);
  const peak = Math.max(1, ...days.map((d) => d.questions));

  const attention = [
    !data.aiActive ? { label: "No AI model is connected", view: "providers", tone: "danger" } : null,
    data.failedDocs ? { label: `${data.failedDocs} document${data.failedDocs === 1 ? "" : "s"} failed to process`, view: "knowledge", tone: "danger" } : null,
    successRate !== null && successRate < 80 ? { label: `Only ${successRate}% of questions were answered without an error in the last 14 days`, view: "reports", tone: "danger" } : null,
    data.reviewQueue ? { label: `${data.reviewQueue} item${data.reviewQueue === 1 ? "" : "s"} waiting for review`, view: "approvals", tone: "warning" } : null,
    data.openRequests ? { label: `${data.openRequests} open help request${data.openRequests === 1 ? "" : "s"}`, view: "requests", tone: "warning" } : null,
    data.pendingInvites ? { label: `${data.pendingInvites} invitation${data.pendingInvites === 1 ? "" : "s"} not yet accepted`, view: "people", tone: "neutral" } : null,
  ].filter((item): item is { label: string; view: string; tone: string } => item !== null);

  const kpis: { label: string; value: string; hint: string; view: string; icon: ComponentType<{ className?: string }> }[] = [
    { label: "Active people", value: show(data.people), hint: data.pendingInvites ? `${data.pendingInvites} invited, not joined` : "All invitations accepted", view: "people", icon: Users },
    { label: "Departments", value: show(data.departments), hint: "Access boundaries", view: "people", icon: Building2 },
    { label: "Documents ready", value: show(data.readyDocs), hint: data.processingDocs ? `${data.processingDocs} processing` : "Searchable now", view: "knowledge", icon: FileText },
    { label: "Questions · 14 days", value: show(summary?.questions ?? null), hint: answered === null ? "Report unavailable" : `${formatNumber(answered)} answered`, view: "reports", icon: MessageSquare },
    { label: "Answer success", value: successRate === null ? "—" : `${successRate}%`, hint: summary?.latency_p50_ms != null ? `Median ${formatNumber(Math.round(summary.latency_p50_ms))} ms` : "No questions yet", view: "reports", icon: Gauge },
    { label: "AI model", value: data.aiActive ? "Connected" : "Not set", hint: data.aiLabel, view: "providers", icon: Cpu },
  ];

  return (
    <div className="grid gap-5">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {kpis.map((kpi) => (
          <button
            key={kpi.label}
            type="button"
            onClick={() => onNavigate(kpi.view)}
            className="group flex min-w-0 cursor-pointer flex-col gap-2 rounded-xl border border-border bg-surface p-4 text-left shadow-xs outline-none transition-all hover:border-primary/40 hover:shadow-sm focus-visible:ring-[3px] focus-visible:ring-ring"
          >
            <span className="flex items-center justify-between gap-2 text-xs font-medium text-muted">
              <span className="truncate">{kpi.label}</span>
              <kpi.icon className="size-4 shrink-0 transition-colors group-hover:text-primary" />
            </span>
            <span className="truncate text-[22px] leading-none font-semibold tracking-tight text-foreground tabular-nums">{kpi.value}</span>
            <span className="truncate text-xs text-muted" title={kpi.hint}>{kpi.hint}</span>
          </button>
        ))}
      </div>

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <Card className="overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-5 py-3.5">
            <div>
              <h2 className="text-[14px] font-semibold text-foreground">Questions asked</h2>
              <p className="text-xs text-muted">Last 14 days</p>
            </div>
            <div className="flex items-center gap-3 text-xs text-muted">
              <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm bg-primary" />Answered</span>
              <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm bg-danger" />Failed</span>
            </div>
          </div>
          {data.usage === null ? (
            <p className="px-5 py-10 text-center text-xs text-muted">The usage report could not be loaded.</p>
          ) : (
            <div className="px-5 pt-6 pb-3">
              <div className="flex h-40 items-end gap-1.5" role="img" aria-label={`Questions per day for the last 14 days; busiest day had ${peak}`}>
                {days.map((day) => {
                  const ok = Math.max(0, day.questions - day.errors);
                  return (
                    <div key={day.day} className="group relative flex h-full min-w-0 flex-1 flex-col justify-end">
                      {day.questions > 0 ? (
                        <div className="flex flex-col overflow-hidden rounded-t-[4px]" style={{ height: `${Math.max(4, (day.questions / peak) * 100)}%` }}>
                          {day.errors > 0 && <div className="bg-danger" style={{ flexGrow: day.errors }} />}
                          {ok > 0 && <div className="bg-primary" style={{ flexGrow: ok }} />}
                        </div>
                      ) : <div className="h-[3px] rounded-full bg-surface-2" />}
                      <span className="pointer-events-none absolute bottom-full left-1/2 z-10 mb-1 hidden -translate-x-1/2 rounded-md border border-border bg-surface px-2 py-1 text-xs whitespace-nowrap text-foreground shadow-lg group-hover:block">
                        {day.label}: {day.questions} asked · {day.errors} failed
                      </span>
                    </div>
                  );
                })}
              </div>
              <div className="mt-2 flex justify-between text-xs text-muted"><span>{days[0]?.label}</span><span>Today</span></div>
            </div>
          )}
        </Card>

        <Card className="overflow-hidden">
          <div className="border-b border-border px-5 py-3.5">
            <h2 className="text-[14px] font-semibold text-foreground">Needs attention</h2>
            <p className="text-xs text-muted">Most urgent first</p>
          </div>
          {attention.length === 0 ? (
            <p className="flex items-center gap-2 px-5 py-6 text-[13px] text-foreground-2"><CheckCircle2 className="size-5 text-success" /> Nothing needs your attention right now.</p>
          ) : (
            <ul className="divide-y divide-border">
              {attention.map((item) => (
                <li key={item.label}>
                  <button type="button" onClick={() => onNavigate(item.view)} className="flex w-full cursor-pointer items-center gap-3 px-5 py-3 text-left text-[13px] text-foreground outline-none hover:bg-surface-2 focus-visible:bg-surface-2">
                    <span className={cn("size-2 shrink-0 rounded-full", item.tone === "danger" ? "bg-danger" : item.tone === "warning" ? "bg-warning" : "bg-border-strong")} />
                    <span className="min-w-0 flex-1">{item.label}</span>
                    <ArrowRight className="size-4 shrink-0 text-muted" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card className="overflow-hidden">
        <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <div>
            <h2 className="text-[14px] font-semibold text-foreground">Recent activity</h2>
            <p className="text-xs text-muted">From the audit log</p>
          </div>
          <Button size="sm" variant="ghost" onClick={() => onNavigate("logs")}>View all <ArrowRight /></Button>
        </div>
        {data.activity === null ? <p className="px-5 py-6 text-xs text-muted">The audit log could not be loaded.</p>
          : data.activity.length === 0 ? <p className="px-5 py-6 text-xs text-muted">No activity recorded yet.</p> : (
          <ul className="divide-y divide-border">
            {data.activity.map((entry) => (
              <li key={entry.id} className="flex items-center gap-3 px-5 py-2.5 text-[13px]">
                <span className={cn("grid size-7 shrink-0 place-items-center rounded-full", entry.outcome === "failure" ? "bg-danger-soft text-danger-text" : "bg-primary-soft text-primary")}><Activity className="size-3.5" /></span>
                <span className="min-w-0 flex-1 truncate">
                  <span className="font-medium text-foreground">{actionLabel(entry.action)}</span>
                  <span className="text-muted">{entry.actor_email ? ` · ${entry.actor_email}` : ""}{entry.target ? ` · ${entry.target}` : ""}</span>
                </span>
                <time className="shrink-0 text-xs text-muted" dateTime={entry.created_at} title={formatDate(entry.created_at)}>{timeAgo(entry.created_at)}</time>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

/** "provider.activated" -> "Provider activated". */
function actionLabel(action: string): string {
  const text = action.replace(/[._]/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** The last `count` calendar days, oldest first; days without activity count as zero. */
function lastDays(count: number, daily: { day: string; questions: number; errors: number }[]) {
  const byDay = new Map(daily.map((row) => [row.day, row]));
  const out: { day: string; label: string; questions: number; errors: number }[] = [];
  for (let offset = count - 1; offset >= 0; offset--) {
    const date = new Date();
    date.setDate(date.getDate() - offset);
    const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
    const row = byDay.get(key);
    out.push({ day: key, label: date.toLocaleDateString(undefined, { month: "short", day: "numeric" }), questions: row?.questions ?? 0, errors: row?.errors ?? 0 });
  }
  return out;
}
