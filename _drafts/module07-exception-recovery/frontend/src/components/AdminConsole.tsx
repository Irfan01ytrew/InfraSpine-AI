import { RecoveryPage } from "../recovery/RecoveryPage";
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { Menu, Plus, RefreshCw, X } from "lucide-react";
import { api, describeError } from "../api";
import { AccountSecurityPage } from "./AccountSecurityPage";
import { cn } from "../lib/utils";
import { AppSidebar, buildNav, CommandPalette } from "./AppSidebar";
import { Alert, Button, PageHeader, Skeleton, Tabs } from "./ui";
import type { AuditEntry, ProviderList, ProviderMetadata, ProviderState, User } from "../types";
import { AuditLog } from "./AuditLog";
import { ApprovalsPage } from "./ApprovalsPage";
import { AddProviderForm } from "./AddProviderForm";
import { ClaimsPage } from "./ClaimsPage";
import { HomePage } from "./HomePage";
import { SettingsPage } from "./SettingsPage";
import { InviteEmployeesPage } from "./InviteEmployeesPage";
import { KnowledgePage } from "./KnowledgePage";
import { KnowledgeInterviewsPage } from "./KnowledgeInterviewsPage";
import { HandoffsPage } from "./HandoffsPage";
import { DepartmentManagerPage } from "./DepartmentManagerPage";
import { MemoryPage } from "./MemoryPage";
import { MeetingsPage } from "./MeetingsPage";
import { PeoplePage } from "./PeoplePage";
import { OrgKnowledgePage } from "./OrgKnowledgePage";
import { ProviderPanel } from "./ProviderPanel";
import { ReportsPage } from "./ReportsPage";
import { ResearchPage } from "./ResearchPage";
import { TrainingPage } from "./TrainingPage";
import { MarketResearchPage } from "./MarketResearchPage";
import { TracesPage } from "./TracesPage";
import { CompanySetupPage } from "./CompanySetupPage";
import { AutomationPage } from "./AutomationPage";
import { AgentCatalogPage } from "./AgentCatalogPage";
import { ServiceRequestsPage } from "./ServiceRequestsPage";
import { ExpertisePage } from "./ExpertisePage";
import { ForecastsPage } from "./ForecastsPage";
import { ProposalCoveragePage } from "./ProposalCoveragePage";
import { MyWorkPage } from "./MyWorkPage";
import { BudgetsPage } from "./BudgetsPage";
import { RequestFormsPage } from "./RequestFormsPage";
import { WebhooksPage } from "./WebhooksPage";
import { PartnerWorkspacesPage } from "./PartnerWorkspacesPage";
import { CloudConnectorsPage } from "./CloudConnectorsPage";
import { requestRefresh, useLiveRefresh } from "../lib/useLiveRefresh";
const WorkspacesPage = lazy(async () => {
  const module = await import("./WorkspacesPage");
  return { default: module.WorkspacesPage };
});

type View = "recovery" | "home" | "invite" | "knowledge" | "interviews" | "my-work" | "research" | "proposals" | "forecasts" | "market-research" | "training" | "org" | "claims" | "approvals" | "handoffs" | "meetings" | "requests" | "expertise" | "departments" | "workspaces" | "partner-workspaces" | "memory" | "automation" | "agents" | "security" | "settings" | "providers" | "traces" | "logs" | "people" | "reports" | "deployment" | "connectors" | "budgets" | "request-forms" | "webhooks";

// The view lives in the URL hash so a refresh keeps the page and it can be linked (#/logs).
function viewFromHash(isAdmin: boolean, isManager: boolean, canViewReports = false, canInvite = false): View {
  const view = window.location.hash.replace(/^#\/?/, "") as View;
  if (view === "recovery") return view;
  if (view === "reports" && canViewReports) return view;
  if (view === "invite" && canInvite) return view;
  if (view === "settings" || view === "providers" || view === "traces" || view === "logs" || view === "people" || view === "reports" || view === "deployment" || view === "workspaces" || view === "partner-workspaces" || view === "connectors") return isAdmin ? view : "knowledge";
  if (view === "departments") return isManager ? view : "knowledge";
  if (view === "budgets") return isAdmin || isManager ? view : "knowledge";
  if (view === "request-forms" || view === "webhooks") return isAdmin ? view : "knowledge";
  if (view === "memory" || view === "claims" || view === "org" || view === "research" || view === "proposals" || view === "forecasts" || view === "market-research" || view === "training" || view === "approvals" || view === "handoffs" || view === "meetings" || view === "requests" || view === "expertise" || view === "my-work" || view === "automation" || view === "agents" || view === "interviews" || view === "security") return view;
  if (view.startsWith("c/")) return "knowledge"; // a saved conversation link (#/c/<id>)
  if (view === "home") return isAdmin ? view : "knowledge";
  // Admins land on the setup/home page; everyone else lands in Knowledge.
  if (view === "knowledge" || !isAdmin) return "knowledge";
  return "home";
}

/** App shell for every signed-in user. Admin-only pages and data are loaded for admins only;
 * the backend enforces the same rules independently. */
export function Console({ user, onSignOut, brandName = "InfraSpine AI", onCompanyProfileSaved }: { user: User; onSignOut: () => void; brandName?: string; onCompanyProfileSaved?: () => void | Promise<void> }) {
  const isAdmin = user.role === "admin";
  const isManager = user.role === "manager";
  const [capabilities, setCapabilities] = useState<string[]>(user.capabilities ?? []);
  const canViewReports = isAdmin || capabilities.includes("reports.view");
  const canInvite = !isAdmin && capabilities.includes("people.invite");
  const effectiveUser = useMemo(() => ({ ...user, capabilities }), [user, capabilities]);
  const [view, setViewState] = useState<View>(() => viewFromHash(isAdmin, isManager, canViewReports, canInvite));
  useEffect(() => {
    // The sign-in response does not carry delegated permissions; read them once for the navigation.
    api.me().then((me) => setCapabilities(me.capabilities ?? [])).catch(() => undefined);
  }, []);
  useEffect(() => { setViewState(viewFromHash(isAdmin, isManager, canViewReports, canInvite)); }, [isAdmin, isManager, canViewReports, canInvite]);
  const [catalog, setCatalog] = useState<ProviderMetadata[]>([]);
  const [list, setList] = useState<ProviderList | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [addingProvider, setAddingProvider] = useState(false);
  const [providerPreset, setProviderPreset] = useState<"gemini" | undefined>();
  useEffect(() => {
    // Market research's "Set up Gemini" opens AI providers with the Gemini form pre-filled.
    if (view !== "providers") return;
    let requested: string | null = null;
    try { requested = sessionStorage.getItem("infraspine_add_provider"); sessionStorage.removeItem("infraspine_add_provider"); } catch { /* storage unavailable */ }
    if (requested === "gemini") { setProviderPreset("gemini"); setAddingProvider(true); }
  }, [view]);

  const refresh = useCallback(async () => {
    if (!isAdmin) return;
    try {
      const [cat, providers, logs] = await Promise.all([api.catalog(), api.providers(), api.auditLogs(200)]);
      setCatalog(cat.providers);
      setList(providers);
      setAudit(logs.entries);
      // Keep the selection unless that provider no longer exists (a removed custom provider).
      setSelected((current) =>
        current && cat.providers.some((p) => p.id === current) ? current : cat.providers[0]?.id ?? null,
      );
      setLoadError(null);
    } catch (err) {
      setLoadError(describeError(err));
    }
  }, [isAdmin]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useLiveRefresh(refresh);

  useEffect(() => {
    const onHash = () => setViewState(viewFromHash(isAdmin, isManager, canViewReports, canInvite));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [isAdmin, isManager, canViewReports, canInvite]);

  function setView(next: View) {
    if (next === view) requestRefresh();
    window.location.hash = next === "home" || (next === "knowledge" && !isAdmin) ? "/" : `/${next}`;
    if (next === "logs") void refresh(); // always show the latest events
  }

  async function providerAdded(providerId: string) {
    await refresh();
    setSelected(providerId);
    setAddingProvider(false);
  }

  const metadata = catalog.find((p) => p.id === selected);
  const state = list?.providers.find((p) => p.provider === selected);
  const active = list?.providers.find((p) => p.is_active);

  const navGroups = useMemo(() => buildNav(isAdmin, isManager, canViewReports, canInvite), [isAdmin, isManager, canViewReports, canInvite]);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const wide = view === "knowledge";
  const currentLabel = navGroups.flatMap((g) => g.items).find((i) => i.view === view)?.label ?? (view === "security" ? "Account security" : "");

  return (
    <div className="flex h-dvh w-full min-w-0 overflow-hidden bg-background">
      <AppSidebar
        groups={navGroups}
        view={view}
        onNavigate={(v) => setView(v as View)}
        brandName={brandName}
        user={user}
        onSignOut={onSignOut}
        aiStatus={isAdmin && list ? { ok: Boolean(active), label: active ? `${active.display_name} · ${active.model}` : "Not configured" } : null}
        mobileOpen={mobileNavOpen}
        onMobileOpenChange={setMobileNavOpen}
      />
      <CommandPalette groups={navGroups} onNavigate={(v) => setView(v as View)} />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center gap-3 border-b border-border bg-surface px-3 lg:hidden">
          <Button variant="ghost" size="icon-sm" aria-label="Open navigation" onClick={() => setMobileNavOpen(true)}><Menu /></Button>
          <span className="truncate text-sm font-semibold text-foreground">{currentLabel || brandName}</span>
        </header>
      <main className={cn("relative min-h-0 min-w-0 flex-1", // Phones stack the Knowledge columns, so the page scrolls; desktops keep each column scrolling on its own.
        wide ? "flex flex-col scrollbar-thin max-lg:overflow-y-auto lg:overflow-hidden" : "scrollbar-thin overflow-x-hidden overflow-y-auto")}>
        <div className={wide ? "contents" : "page-container mx-auto w-full max-w-[1240px] px-4 py-6 sm:px-6 lg:px-8 lg:py-8"}>
        {view === "home" ? (
          <HomePage user={user} brandName={brandName} onNavigate={(v) => setView(v as View)} />
        ) : view === "invite" ? (
          <InviteEmployeesPage />
        ) : view === "security" ? (
          <AccountSecurityPage user={user} />
        ) : view === "deployment" ? (
          <CompanySetupPage onProfileSaved={onCompanyProfileSaved} />
        ) : view === "settings" ? (
          <SettingsPage />
        ) : view === "knowledge" ? (
          <KnowledgePage user={user} />
        ) : view === "interviews" ? (
          <KnowledgeInterviewsPage user={user} />
        ) : view === "research" ? (
          <ResearchPage user={user} />
        ) : view === "proposals" ? (
          <ProposalCoveragePage />
        ) : view === "forecasts" ? (
          <ForecastsPage />
        ) : view === "market-research" ? (
          <MarketResearchPage isAdmin={isAdmin} />
        ) : view === "training" ? (
          <TrainingPage user={effectiveUser} />
        ) : view === "meetings" ? (
          <MeetingsPage user={user} />
        ) : view === "org" ? (
          <OrgKnowledgePage />
        ) : view === "claims" ? (
          <ClaimsPage />
        ) : view === "approvals" ? (
          <ApprovalsPage />
        ) : view === "handoffs" ? (
          <HandoffsPage user={user} />
        ) : view === "requests" ? (
          <ServiceRequestsPage user={user} />
        ) : view === "webhooks" ? (
          <WebhooksPage />
        ) : view === "request-forms" ? (
          <RequestFormsPage />
        ) : view === "budgets" ? (
          <BudgetsPage user={user} />
        ) : view === "expertise" ? (
          <ExpertisePage />
        ) : view === "my-work" ? (
          <MyWorkPage user={user} />
        ) : view === "departments" ? (
          <DepartmentManagerPage />
        ) : view === "memory" ? (
          <MemoryPage />
        ) : view === "recovery" ? (
          <RecoveryPage user={user} />
        ) : view === "automation" ? (
          <AutomationPage user={user} />
        ) : view === "agents" ? (
          <AgentCatalogPage user={user} />
        ) : view === "people" ? (
          <PeoplePage />
        ) : view === "workspaces" ? (
          <Suspense fallback={<Skeleton className="h-64 w-full" />}><WorkspacesPage /></Suspense>
        ) : view === "partner-workspaces" ? (
          <PartnerWorkspacesPage />
        ) : view === "connectors" ? (
          <CloudConnectorsPage />
        ) : view === "reports" ? (
          <ReportsPage />
        ) : view === "traces" ? (
          <TracesPage />
        ) : loadError ? (
          <Alert tone="danger" title="Couldn't load this page">{loadError}</Alert>
        ) : !list ? (
          <div className="grid gap-3"><Skeleton className="h-8 w-60" /><Skeleton className="h-4 w-96" /><Skeleton className="h-64 w-full" /></div>
        ) : view === "providers" ? (
          <>
            <PageHeader
              title="AI providers"
              description={`Choose which model powers ${brandName}. Changes take effect immediately.`}
              actions={<Button variant={addingProvider ? "secondary" : "primary"} onClick={() => setAddingProvider(!addingProvider)}>{addingProvider ? <><X /> Close</> : <><Plus /> Add provider</>}</Button>}
            />
            {addingProvider && (
              <AddProviderForm preset={providerPreset} onCancel={() => { setAddingProvider(false); setProviderPreset(undefined); }} onCreated={providerAdded} />
            )}
            {list.providers.length > 1 && (
              <Tabs
                className="mb-5"
                value={selected ?? ""}
                onValueChange={setSelected}
                items={list.providers.map((p: ProviderState) => ({
                  value: p.provider,
                  label: <><span className={cn("size-1.5 rounded-full", p.is_active ? "bg-success" : "bg-border-strong")} />{p.display_name}</>,
                }))}
              />
            )}
            {metadata && state && <ProviderPanel key={metadata.id} metadata={metadata} state={state} onChanged={refresh} />}
          </>
        ) : (
          <>
            <PageHeader
              title="Audit logs"
              description="Sign-ins and configuration changes, newest first. Entries cannot be edited or deleted."
              actions={<Button onClick={() => void refresh()}><RefreshCw /> Refresh</Button>}
            />
            <AuditLog entries={audit} />
          </>
        )}
        </div>
      </main>
      </div>
    </div>
  );
}

export function StatusPill({ state }: { state: ProviderState }) {
  if (state.is_active)
    return (
      <span className="pill pill-success">
        <span className="dot dot-success dot-pulse" /> Active
      </span>
    );
  if (!state.configured)
    return (
      <span className="pill">
        <span className="dot dot-muted" /> Not configured
      </span>
    );
  return (
    <span className="pill pill-warning">
      <span className="dot dot-warning" /> Inactive
    </span>
  );
}
