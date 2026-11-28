import type { ComponentType } from "react";
import { Activity, ArrowRight, BarChart3, Building2, CheckCircle2, Cpu, FileText, Gauge, MessageSquare, Users } from "lucide-react";
import { formatDate, formatNumber, timeAgo } from "../format";
import { donutSegments, fillRunDays, topModules } from "../lib/dashboardCharts.mjs";
import { cn } from "../lib/utils";
import type { AgentStudioAnalytics, AuditEntry, UsageReport } from "../types";
import { Button, Card, EmptyState } from "./ui";

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
  /** Agent Studio run analytics (30 days). Null when the endpoint could not be read — charts show their empty state, never a guess. */
  moduleAnalytics: AgentStudioAnalytics | null;
}

const show = (value: number | null) => (value === null ? "—" : formatNumber(value));

const OUTCOME_DOT: Record<string, string> = {
  completed: "bg-success",
  failed: "bg-danger",
  awaiting: "bg-warning",
  active: "bg-primary",
  cancelled: "bg-border-strong",
};

/** Admin overview built only from live API data. */
export function HomeDashboard({ data, onNavigate }: { data: DashboardData; onNavigate: (view: string) => void }) {
  const summary = data.usage?.summary ?? null;
  const answered = summary ? Math.max(0, summary.questions - summary.errors) : null;
  const successRate = summary && summary.questions > 0 ? Math.round((answered! / summary.questions) * 100) : null;
  const days = lastDays(14, data.usage?.daily ?? []);
  const peak = Math.max(1, ...days.map((d) => d.questions));

  // Module activity, all from the live Agent Studio analytics payload.
  const runs = data.moduleAnalytics;
  const runDays = runs ? fillRunDays(30, runs.daily) : [];
  const runPeak = Math.max(1, ...runDays.map((d) => d.completed + d.failed));
  const runTotal = runs?.totals.total_runs ?? 0;
  const runSuccess = runs?.totals.success_rate ?? null;
  const outcomes = donutSegments([
    { key: "completed", label: "Completed", value: runs?.totals.completed ?? 0, color: "var(--success)" },
    { key: "failed", label: "Failed", value: runs?.totals.failed ?? 0, color: "var(--danger)" },
    { key: "awaiting", label: "Awaiting approval", value: runs?.totals.awaiting_approval ?? 0, color: "var(--warning)" },
    { key: "active", label: "Queued + running", value: (runs?.totals.queued ?? 0) + (runs?.totals.running ?? 0), color: "var(--primary)" },
    { key: "cancelled", label: "Cancelled", value: runs?.totals.cancelled ?? 0, color: "var(--border-strong)" },
  ]);
  const busiest = runs ? topModules(runs.agents, 5) : [];

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
            <EmptyState
              icon={<BarChart3 />}
              title="Usage report could not be loaded"
              description="The 14-day questions trend is unavailable right now. Your data is safe — try refreshing the page."
            />
          ) : (
            <div className="px-5 pt-6 pb-3">
              <div className="flex h-40 items-end gap-1.5" role="img" aria-label={`Questions per day for the last 14 days; busiest day had ${peak}`}>
                {days.map((day) => {
                  const ok = Math.max(0, day.questions - day.errors);
                  return (
                    <div key={day.day} className="group relative flex h-full min-w-0 flex-1 flex-col justify-end">
                      {day.questions > 0 ? (
                        <div className="chart-bar-grow flex flex-col overflow-hidden rounded-t-[4px]" style={{ height: `${Math.max(4, (day.questions / peak) * 100)}%` }}>
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

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <Card className="overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-5 py-3.5">
            <div>
              <h2 className="text-[14px] font-semibold text-foreground">Module runs</h2>
              <p className="text-xs text-muted">Last 30 days · {runs === null ? "—" : `${formatNumber(runTotal)} runs`}</p>
            </div>
            <div className="flex items-center gap-3 text-xs text-muted">
              <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm bg-primary" />Completed</span>
              <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm bg-danger" />Failed</span>
            </div>
          </div>
          {runs === null ? (
            <EmptyState
              icon={<BarChart3 />}
              title="Module run data could not be loaded"
              description="The 30-day runs trend is unavailable right now. Your data is safe — try refreshing the page."
            />
          ) : runTotal === 0 ? (
            <EmptyState
              icon={<BarChart3 />}
              title="No module runs yet"
              description="Module specialists have not run in the last 30 days. Runs will appear here as soon as an agent starts work."
            />
          ) : (
            <>
              <div className="px-5 pt-6 pb-3">
                <div className="flex h-40 items-end gap-1" role="img" aria-label={`Module runs per day for the last 30 days; busiest day had ${runPeak} runs`}>
                  {runDays.map((day) => {
                    const total = day.completed + day.failed;
                    return (
                      <div key={day.day} className="group relative flex h-full min-w-0 flex-1 flex-col justify-end">
                        {total > 0 ? (
                          <div className="chart-bar-grow flex flex-col overflow-hidden rounded-t-[4px]" style={{ height: `${Math.max(4, (total / runPeak) * 100)}%` }}>
                            {day.failed > 0 && <div className="bg-danger" style={{ flexGrow: day.failed }} />}
                            {day.completed > 0 && <div className="bg-primary" style={{ flexGrow: day.completed }} />}
                          </div>
                        ) : <div className="h-[3px] rounded-full bg-surface-2" />}
                        <span className="pointer-events-none absolute bottom-full left-1/2 z-10 mb-1 hidden -translate-x-1/2 rounded-md border border-border bg-surface px-2 py-1 text-xs whitespace-nowrap text-foreground shadow-lg group-hover:block">
                          {day.label}: {total} runs · {day.failed} failed
                        </span>
                      </div>
                    );
                  })}
                </div>
                <div className="mt-2 flex justify-between text-xs text-muted"><span>{runDays[0]?.label}</span><span>Today</span></div>
              </div>
              {busiest.length > 0 && (
                <div className="border-t border-border px-5 py-4">
                  <h3 className="text-xs font-semibold text-foreground">Most active modules</h3>
                  <ul className="mt-3 grid gap-2.5">
                    {busiest.map((m) => (
                      <li key={m.id} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3">
                        <span className="min-w-0">
                          <span className="block truncate text-[13px] font-medium text-foreground">{m.name}</span>
                          <span className="mt-1 block h-1.5 overflow-hidden rounded-full bg-surface-2">
                            <span className="block h-full rounded-full bg-primary" style={{ width: `${Math.max(3, m.share * 100)}%` }} />
                          </span>
                        </span>
                        <span className="text-xs tabular-nums text-muted">{formatNumber(m.total)} runs · {formatNumber(m.completed)} done</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
        </Card>

        <Card className="overflow-hidden">
          <div className="border-b border-border px-5 py-3.5">
            <h2 className="text-[14px] font-semibold text-foreground">Run outcomes</h2>
            <p className="text-xs text-muted">Last 30 days</p>
          </div>
          {runs === null ? (
            <EmptyState
              icon={<Gauge />}
              title="Run outcomes could not be loaded"
              description="The outcomes breakdown is unavailable right now. Your data is safe — try refreshing the page."
            />
          ) : runTotal === 0 ? (
            <EmptyState
              icon={<Gauge />}
              title="No runs to break down yet"
              description="Once module specialists run, their completed, failed and awaiting outcomes will appear here."
            />
          ) : (
            <div className="flex flex-col items-center gap-4 px-5 py-6">
              <svg viewBox="0 0 140 140" className="size-40" role="img" aria-label={`Run outcomes: ${outcomes.segments.map((s) => `${s.label} ${s.value}`).join(", ")}`}>
                <circle cx="70" cy="70" r="56" fill="none" stroke="var(--surface-2)" strokeWidth="18" />
                {outcomes.segments.map((s) => (
                  <circle key={s.key} cx="70" cy="70" r="56" fill="none" stroke={s.color} strokeWidth="18" strokeDasharray={s.dash} strokeDashoffset={s.offset} transform="rotate(-90 70 70)" />
                ))}
                <text x="70" y="66" textAnchor="middle" className="fill-foreground text-xl font-semibold">{runSuccess === null ? "—" : `${runSuccess}%`}</text>
                <text x="70" y="84" textAnchor="middle" className="fill-muted text-xs">success</text>
              </svg>
              <ul className="grid w-full gap-2">
                {outcomes.segments.map((s) => (
                  <li key={s.key} className="flex items-center gap-2 text-[13px]">
                    <span className={`size-2.5 shrink-0 rounded-sm ${OUTCOME_DOT[s.key]}`} />
                    <span className="flex-1 text-foreground-2">{s.label}</span>
                    <span className="font-semibold tabular-nums text-foreground">{formatNumber(s.value)}</span>
                  </li>
                ))}
              </ul>
            </div>
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
        {data.activity === null ? (
          <EmptyState icon={<Activity />} title="Audit log could not be loaded" description="Recent activity is unavailable right now. Your data is safe — try refreshing the page." />
        ) : data.activity.length === 0 ? (
          <EmptyState icon={<Activity />} title="No activity recorded yet" description="Actions taken in InfraSpine will appear here as soon as your team starts working." />
        ) : (
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
