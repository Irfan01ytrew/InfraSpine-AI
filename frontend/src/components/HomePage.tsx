import { useCallback, useEffect, useState, type ComponentType } from "react";
import {
  ArrowRight, BarChart3, Bot, BookOpen, Building2, CheckCircle2, Circle, ClipboardCheck, Cpu, FileUp, GraduationCap,
  MessageSquare, RefreshCw, ShieldCheck, Sparkles, UserPlus, Users,
} from "lucide-react";
import { api } from "../api";
import { cn } from "../lib/utils";
import type { User } from "../types";
import { Alert, Badge, Button, Card, Skeleton } from "./ui";
import { HomeDashboard, type DashboardData } from "./HomeDashboard";
import { useLiveRefresh } from "../lib/useLiveRefresh";
import {
  loadDashboardSnapshot,
  recordDashboardSnapshot,
  staleLabel,
} from "../lib/offlineReadCache.mjs";
import type { DashboardSnapshot } from "../lib/offlineReadCache.mjs";

type Navigate = (view: string) => void;

interface SetupState extends DashboardData {
  agents: number;
}

type Step = {
  key: string;
  title: string;
  why: string;
  done: boolean;
  detail: string;
  action: string;
  view: string;
  icon: ComponentType<{ className?: string }>;
};

async function loadSetup(): Promise<SetupState> {
  const [providers, groups, users, invites, docs, agents, usage, requests, queue, audit] = await Promise.allSettled([
    api.providers(), api.adminGroups(), api.adminUsers(), api.invitations(), api.documents(), api.agentProfiles(),
    api.usageReport(14), api.serviceRequests(), api.governanceQueue(), api.auditLogs(8),
  ]);
  const value = <T,>(r: PromiseSettledResult<T>): T | null => (r.status === "fulfilled" ? r.value : null);
  const active = value(providers)?.providers.find((p) => p.is_active);
  return {
    aiActive: Boolean(active),
    aiLabel: active ? `${active.display_name} · ${active.model}` : "Not connected",
    departments: value(groups)?.groups.filter((g) => g.kind === "department" && g.lifecycle_status !== "archived").length ?? 0,
    people: value(users)?.users.filter((u) => u.is_active).length ?? 0,
    pendingInvites: value(invites)?.invitations.filter((i) => !i.accepted_at && !i.revoked_at).length ?? 0,
    readyDocs: value(docs)?.documents.filter((d) => d.status === "ready").length ?? 0,
    agents: value(agents)?.agents.filter((a) => a.enabled).length ?? 0,
    processingDocs: value(docs)?.documents.filter((d) => d.status === "pending" || d.status === "processing").length ?? null,
    failedDocs: value(docs)?.documents.filter((d) => d.status === "failed").length ?? null,
    usage: value(usage),
    openRequests: value(requests)?.items.filter((r) => !["resolved", "rejected", "cancelled"].includes(r.status)).length ?? null,
    reviewQueue: value(queue)?.items.length ?? null,
    activity: value(audit)?.entries ?? null,
  };
}

/** Admin landing page: a live setup checklist plus the everyday tasks, each one click from its page. */
export function HomePage({ user, brandName, onNavigate }: { user: User; brandName: string; onNavigate: Navigate }) {
  const [state, setState] = useState<SetupState | null>(null);
  const [loading, setLoading] = useState(false);
  const [showSetup, setShowSetup] = useState(false);

  // Offline read-model cache (Step U, slice 5): the last dashboard
  // snapshot, served read-only while offline and always labelled stale.
  const [online, setOnline] = useState(() =>
    typeof navigator === "undefined" ? true : navigator.onLine,
  );
  const [cachedSnapshot, setCachedSnapshot] = useState<DashboardSnapshot | null>(() => {
    try {
      return typeof localStorage === "undefined" ? null : loadDashboardSnapshot(localStorage);
    } catch {
      return null;
    }
  });

  const refresh = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const data = await loadSetup();
      setState(data);
      // Record the snapshot only while online. An all-empty load must
      // never overwrite a good cached snapshot; recordDashboardSnapshot
      // refuses empty data, so this check is defence in depth.
      if (typeof navigator !== "undefined" && navigator.onLine) {
        try {
          const snapshot = recordDashboardSnapshot(localStorage, data);
          if (snapshot) setCachedSnapshot(snapshot);
        } catch {
          // The dashboard is already on screen; the offline copy is best-effort.
        }
      }
    } finally { setLoading(false); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    const onOnline = () => {
      setOnline(true);
      void refresh(true); // Replace the stale snapshot with live data.
    };
    const onOffline = () => setOnline(false);
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    return () => {
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
    };
  }, [refresh]);
  useLiveRefresh(() => refresh(true));

  // While offline, show the last saved dashboard instead of the empty
  // load — clearly marked stale below, never used for decisions.
  const displayState = !online && cachedSnapshot ? cachedSnapshot.data : state;
  const steps: Step[] = displayState ? [
    {
      key: "ai", title: "Connect an AI model", icon: Cpu, view: "providers", action: "Open AI providers",
      why: "Answers, research and agents need one active AI provider.",
      done: displayState.aiActive, detail: displayState.aiLabel,
    },
    {
      key: "departments", title: "Create your departments", icon: Building2, view: "people", action: "Add departments",
      why: "Departments decide who can see which documents, agents and reports.",
      done: displayState.departments > 0, detail: `${displayState.departments} department${displayState.departments === 1 ? "" : "s"}`,
    },
    {
      key: "people", title: "Invite employees", icon: UserPlus, view: "people", action: "Invite people",
      why: "Invite one by one, or upload a CSV to add hundreds at once with their department and role.",
      done: displayState.people > 1 || displayState.pendingInvites > 0,
      detail: `${displayState.people} active account${displayState.people === 1 ? "" : "s"}${displayState.pendingInvites ? ` · ${displayState.pendingInvites} invitation${displayState.pendingInvites === 1 ? "" : "s"} pending` : ""}`,
    },
    {
      key: "docs", title: "Add company documents", icon: FileUp, view: "knowledge", action: "Upload documents",
      why: "PDF, Office, images, audio and video become searchable with page and time citations.",
      done: displayState.readyDocs > 0, detail: `${displayState.readyDocs} document${displayState.readyDocs === 1 ? "" : "s"} ready`,
    },
    {
      key: "agents", title: "Create an AI agent", icon: Bot, view: "agents", action: "Create agent",
      why: "Give a department its own assistant with a fixed knowledge scope and allowed tools.",
      done: displayState.agents > 0, detail: `${displayState.agents} active agent${displayState.agents === 1 ? "" : "s"}`,
    },
  ] : [];
  const completed = steps.filter((s) => s.done).length;
  const next = steps.find((s) => !s.done);
  const setupDone = Boolean(displayState) && completed === steps.length;

  const tasks: { title: string; detail: string; view: string; icon: ComponentType<{ className?: string }> }[] = [
    { title: "Ask your documents", detail: "Cited answers from company knowledge", view: "knowledge", icon: MessageSquare },
    { title: "People & departments", detail: "Invite, assign roles, bulk CSV import", view: "people", icon: Users },
    { title: "Agents", detail: "Department assistants and their tools", view: "agents", icon: Bot },
    { title: "Approvals", detail: "Review actions waiting for a decision", view: "approvals", icon: ClipboardCheck },
    { title: "Employee training", detail: "Courses and quizzes from your documents", view: "training", icon: GraduationCap },
    { title: "Usage reports", detail: "Activity, cost and quotas by department", view: "reports", icon: BarChart3 },
    { title: "Company setup", detail: "Brand, industry template and license", view: "deployment", icon: Sparkles },
    { title: "Account security", detail: "Password and two-factor sign-in", view: "security", icon: ShieldCheck },
  ];

  return (
    <div className="grid gap-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="mb-1 text-[11px] font-semibold tracking-wider text-primary uppercase">{brandName}</p>
          <h1 className="text-[24px] font-semibold tracking-tight text-foreground">{greeting()}</h1>
          <p className="mt-1 text-[13px] text-muted">Signed in as {user.email}. {setupDone ? `Here is how ${brandName} is doing.` : "Start here: each step opens the page where it is done."}</p>
        </div>
        <Button variant="ghost" onClick={() => void refresh()} loading={loading}>{!loading && <RefreshCw />} Refresh</Button>
      </header>

      {!online && cachedSnapshot && (
        <Alert tone="warning" title="Offline — showing a saved dashboard">
          Saved {staleLabel(cachedSnapshot.savedAt)}. These numbers may be out of date and are
          read-only — do not use them for decisions. They refresh automatically when you are
          back online.
        </Alert>
      )}

      {displayState && setupDone && <HomeDashboard data={displayState} onNavigate={onNavigate} />}

      {setupDone && !showSetup ? (
        <Card className="flex flex-wrap items-center justify-between gap-3 px-5 py-3.5">
          <span className="flex items-center gap-2.5 text-[13px] text-foreground-2"><CheckCircle2 className="size-5 shrink-0 text-success" /> Workspace setup is complete: AI model, departments, people, documents and agents are in place.</span>
          <Button size="sm" variant="ghost" onClick={() => setShowSetup(true)}>Review setup</Button>
        </Card>
      ) : (
      <Card className="overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-4 border-b border-border px-5 py-4">
          <div>
            <h2 className="text-[15px] font-semibold text-foreground">Set up your workspace</h2>
            <p className="mt-0.5 text-xs text-muted">{displayState ? `${completed} of ${steps.length} steps complete` : "Checking your setup…"}</p>
          </div>
          {displayState && (
            <div className="flex min-w-48 flex-1 items-center gap-3 sm:max-w-xs">
              <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface-2" role="progressbar" aria-valuemin={0} aria-valuemax={steps.length} aria-valuenow={completed} aria-label="Setup progress">
                <div className="h-full rounded-full bg-primary transition-[width] duration-500" style={{ width: `${(completed / steps.length) * 100}%` }} />
              </div>
              <span className="text-xs font-semibold text-foreground">{Math.round((completed / steps.length) * 100)}%</span>
            </div>
          )}
        </div>
        {!state ? (
          <div className="grid gap-3 p-5">{[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-14 w-full" />)}</div>
        ) : (
          <ol className="divide-y divide-border">
            {steps.map((step, index) => {
              const isNext = step.key === next?.key;
              return (
                <li key={step.key} className={cn("flex flex-wrap items-center gap-4 px-5 py-4 transition-colors", isNext && "bg-primary-soft/50")}>
                  <span className="shrink-0">
                    {step.done ? <CheckCircle2 className="size-6 text-success" aria-label="Done" /> : <Circle className={cn("size-6", isNext ? "text-primary" : "text-border-strong")} aria-label="Not done" />}
                  </span>
                  <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-surface-2 text-foreground-2"><step.icon className="size-[18px]" /></span>
                  <div className="min-w-0 flex-1 basis-56">
                    <p className="flex flex-wrap items-center gap-2 text-[13.5px] font-semibold text-foreground">
                      <span className="text-muted">{index + 1}.</span> {step.title}
                      {isNext && <Badge tone="primary">Next step</Badge>}
                    </p>
                    <p className="mt-0.5 text-xs leading-relaxed text-muted">{step.why}</p>
                    <p className={cn("mt-1 text-xs font-medium", step.done ? "text-success-text" : "text-foreground-2")}>{step.detail}</p>
                  </div>
                  <Button variant={isNext ? "primary" : "secondary"} size="sm" onClick={() => onNavigate(step.view)}>
                    {step.done ? "Manage" : step.action} <ArrowRight />
                  </Button>
                </li>
              );
            })}
          </ol>
        )}
      </Card>
      )}

      <section aria-labelledby="everyday-tasks">
        <h2 id="everyday-tasks" className="mb-3 text-[15px] font-semibold text-foreground">Everyday tasks</h2>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {tasks.map((task) => (
            <button
              key={task.view + task.title}
              type="button"
              onClick={() => onNavigate(task.view)}
              className="group flex cursor-pointer items-start gap-3 rounded-xl border border-border bg-surface p-4 text-left shadow-xs transition-all outline-none hover:-translate-y-px hover:border-primary/40 hover:shadow-sm focus-visible:ring-[3px] focus-visible:ring-ring"
            >
              <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary-soft text-primary"><task.icon className="size-[18px]" /></span>
              <span className="min-w-0">
                <span className="block text-[13px] font-semibold text-foreground">{task.title}</span>
                <span className="mt-0.5 block text-xs leading-relaxed text-muted">{task.detail}</span>
              </span>
            </button>
          ))}
        </div>
      </section>

      <p className="flex items-center gap-2 text-xs text-muted">
        <BookOpen className="size-3.5" /> Tip: press <kbd className="rounded border border-border bg-surface-2 px-1.5 font-sans text-[10px]">Ctrl K</kbd> anywhere to jump to any page by name.
      </p>
    </div>
  );
}

function greeting(): string {
  const hour = new Date().getHours();
  return hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
}
