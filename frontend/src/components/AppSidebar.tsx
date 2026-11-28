import { useEffect, useMemo, useRef, useState, type ComponentType } from "react";
import { Dialog as D } from "radix-ui";
import {
  BarChart3, BookOpen, Home, Bot, Boxes, Briefcase, Building2, ChevronDown, ChevronsLeft, ChevronsRight, ClipboardCheck, Cloud,
  Cpu, FileSearch, FileText, GraduationCap, Handshake, KeyRound, Languages, LayoutList, LifeBuoy, LogOut, Mic, Network,
  Receipt, ScrollText, UserPlus, Search, Settings2, ShieldCheck, Sparkles, TrendingUp, UserCog, Users, Workflow, X, Brain, Globe2, Sun, Moon, Monitor, Wallet, ClipboardList, Webhook,
} from "lucide-react";
import { initials } from "../format";
import { savedTheme, setTheme, type ThemeChoice } from "../theme";
import { cn } from "../lib/utils";
import type { User } from "../types";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger, Tooltip } from "./ui";

export type NavEntry = { view: string; label: string; icon: ComponentType<{ className?: string }>; keywords?: string };
export type NavGroup = { id: string; label: string; items: NavEntry[] };

export function buildNav(isAdmin: boolean, isManager: boolean, canViewReports = false, canInvite = false): NavGroup[] {
  const groups: NavGroup[] = [
    {
      id: "workspace", label: "Workspace", items: [
        ...(isAdmin ? [{ view: "module-control-center", label: "Module control", icon: Boxes, keywords: "master command dashboard infraspine modules performance" }] : []),
        ...(isAdmin ? [{ view: "home", label: "Home", icon: Home, keywords: "start setup getting started checklist" }] : []),
        { view: "knowledge", label: "Knowledge", icon: BookOpen, keywords: "chat ask documents" },
        { view: "agent-studio", label: "AI Agents", icon: Bot, keywords: "langgraph autonomous tools whatsapp" },
        { view: "my-work", label: "My work", icon: Briefcase, keywords: "tasks calendar" },
        { view: "meetings", label: "Meeting minutes", icon: Mic },
        { view: "memory", label: "Memory", icon: Brain },
      ],
    },
    ...(isAdmin ? [{
      id: "infraspine-modules", label: "InfraSpine modules", items: [
        { view: "knowledge", label: "01 · Knowledge & AI Brain", icon: BookOpen },
        { view: "agent-studio", label: "02 · IRFANIX AI Agent", icon: Bot },
        { view: "company-graph", label: "03 · Company intelligence", icon: Network },
        { view: "process-intelligence", label: "04 · Process intelligence", icon: Workflow },
        { view: "workflow-engine", label: "05 · Workflow engine", icon: Workflow },
        { view: "governance", label: "06 · Governance & security", icon: ShieldCheck },
        { view: "recovery", label: "07 · Exception & recovery", icon: LifeBuoy },
        { view: "continuity", label: "08 · Business continuity", icon: ShieldCheck },
        { view: "frontend-ux", label: "09 · Frontend & UX", icon: Sparkles },
        { view: "integrations", label: "10 · Integrations", icon: Cloud },
        { view: "infrastructure", label: "11 · Infrastructure", icon: Cpu },
        { view: "quality", label: "12 · QA & benchmarking", icon: ClipboardCheck },
      ],
    }] : []),
    {
      id: "research", label: "Research & insights", items: [
        { view: "research", label: "Company data research", icon: FileSearch },
        { view: "proposals", label: "Tender & RFP coverage", icon: FileText },
        { view: "forecasts", label: "What-if forecasts", icon: TrendingUp },
        { view: "predictive-continuity", label: "Predictive continuity", icon: TrendingUp },
        { view: "market-research", label: "Market research", icon: Globe2, keywords: "competitor" },
      ],
    },
    {
      id: "knowledge-ops", label: "Knowledge", items: [
        { view: "org", label: "Org knowledge", icon: Network },
        { view: "interviews", label: "Knowledge interviews", icon: Sparkles },
        { view: "handoffs", label: "Knowledge handoffs", icon: Handshake },
        { view: "expertise", label: "Expertise directory", icon: Users },
        { view: "training", label: "Employee training", icon: GraduationCap },
      ],
    },
    {
      id: "operations", label: "Operations", items: [
        { view: "approvals", label: "Approvals", icon: ClipboardCheck },
        { view: "finance", label: "Invoices & payments", icon: Receipt, keywords: "finance invoice verification payment spend vendor" },
        { view: "claims", label: "Claims", icon: Receipt },
        { view: "process-intelligence", label: "Process intelligence", icon: Workflow },
        { view: "automation", label: "Workflows & tasks", icon: Workflow },
        { view: "agents", label: "Agent profiles", icon: Bot },
        { view: "requests", label: "Help & requests", icon: LifeBuoy },
        ...(isAdmin ? [{ view: "request-forms", label: "Request forms", icon: ClipboardList }] : []),
        ...(isAdmin || isManager ? [{ view: "budgets", label: "Budgets & spend", icon: Wallet }] : []),
        ...(isManager ? [{ view: "departments", label: "My departments", icon: Building2 }] : []),
        ...(!isAdmin && canViewReports ? [{ view: "reports", label: "Usage reports", icon: BarChart3 }] : []),
        ...(canInvite ? [{ view: "invite", label: "Invite employees", icon: UserPlus }] : []),
      ],
    },
  ];
  if (isAdmin) {
    groups.push({
      id: "admin", label: "Administration", items: [
        { view: "people", label: "People & access", icon: UserCog },
        { view: "partner-workspaces", label: "Partner access", icon: KeyRound, keywords: "external" },
        { view: "workspaces", label: "Client workspaces", icon: Boxes, keywords: "project" },
        { view: "connectors", label: "Cloud connectors", icon: Cloud },
        { view: "webhooks", label: "Webhooks", icon: Webhook },
        { view: "providers", label: "AI providers", icon: Cpu, keywords: "model llm" },
        { view: "settings", label: "Settings", icon: Settings2, keywords: "ocr parser vector embedding" },
        { view: "deployment", label: "Company setup", icon: Building2, keywords: "brand license" },
        { view: "reports", label: "Usage reports", icon: BarChart3 },
        { view: "traces", label: "Knowledge Quality", icon: LayoutList },
        { view: "logs", label: "Audit logs", icon: ScrollText },
      ],
    });
  }
  return groups;
}

const COLLAPSE_KEY = "infraspine_sidebar_collapsed";
const GROUPS_KEY = "infraspine_sidebar_groups";

function readJSON<T>(key: string, fallback: T): T {
  try { const raw = localStorage.getItem(key); return raw ? (JSON.parse(raw) as T) : fallback; } catch { return fallback; }
}
function writeJSON(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ }
}

export function AppSidebar({ groups, view, onNavigate, brandName, user, onSignOut, aiStatus, mobileOpen, onMobileOpenChange }: {
  groups: NavGroup[]; view: string; onNavigate: (view: string) => void; brandName: string; user: User; onSignOut: () => void;
  aiStatus?: { ok: boolean; label: string } | null; mobileOpen: boolean; onMobileOpenChange: (open: boolean) => void;
}) {
  // Default to the icon rail on narrower desktops so the three-column Knowledge view keeps a readable chat.
  const [collapsed, setCollapsed] = useState(() => readJSON(COLLAPSE_KEY, typeof window !== "undefined" && window.innerWidth < 1280));
  const [closedGroups, setClosedGroups] = useState<string[]>(() => readJSON(GROUPS_KEY, []));

  function toggleCollapsed() { setCollapsed((c) => { writeJSON(COLLAPSE_KEY, !c); return !c; }); }
  function toggleGroup(id: string) {
    setClosedGroups((g) => { const next = g.includes(id) ? g.filter((x) => x !== id) : [...g, id]; writeJSON(GROUPS_KEY, next); return next; });
  }

  const content = (rail: boolean) => (
    <div className="flex h-full min-h-0 flex-col">
      {/* Brand */}
      <div className={cn("flex h-14 shrink-0 items-center gap-2.5 border-b border-border", rail ? "justify-center px-2" : "px-4")}>
        <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-primary text-sm font-bold text-primary-foreground shadow-xs">
          {brandName.trim().charAt(0).toUpperCase() || "I"}
        </span>
        {!rail && <span className="truncate text-[14px] font-semibold tracking-tight text-foreground">{brandName}</span>}
      </div>

      {/* Search trigger */}
      <div className={cn("shrink-0 pt-3", rail ? "px-2" : "px-3")}>
        <CommandTrigger rail={rail} onClick={() => window.dispatchEvent(new Event("infraspine:open-command"))} />
      </div>

      {/* Navigation: the only part of the sidebar that scrolls */}
      <nav aria-label="Main navigation" className={cn("scrollbar-thin min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain py-3", rail ? "px-2" : "px-3")}>
        {groups.map((group) => {
          const open = rail || !closedGroups.includes(group.id);
          return (
            <div key={group.id} className="mb-2 last:mb-0">
              {rail ? (
                <div className="mx-auto my-2 h-px w-6 bg-border first:hidden" aria-hidden />
              ) : (
                <button
                  type="button"
                  onClick={() => toggleGroup(group.id)}
                  aria-expanded={open}
                  className="group flex w-full cursor-pointer items-center justify-between rounded-md px-2 py-1.5 text-[10.5px] font-semibold tracking-[0.08em] text-muted uppercase hover:text-foreground-2"
                >
                  {group.label}
                  <ChevronDown className={cn("size-3.5 transition-transform", !open && "-rotate-90")} />
                </button>
              )}
              {open && (
                <ul className="grid gap-0.5">
                  {group.items.map((item) => (
                    <li key={item.view}>
                      <NavButton item={item} active={view === item.view} rail={rail} onClick={() => { onNavigate(item.view); onMobileOpenChange(false); }} />
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </nav>

      {/* Footer */}
      <div className={cn("shrink-0 space-y-2 border-t border-border", rail ? "p-2" : "p-3")}>
        {aiStatus && !rail && (
          <div className="flex items-center gap-2.5 rounded-lg border border-border bg-surface-2 px-3 py-2">
            <span className={cn("relative flex size-2 shrink-0 rounded-full", aiStatus.ok ? "bg-success" : "bg-muted")}>
              {aiStatus.ok && <span className="absolute inset-0 animate-ping rounded-full bg-success opacity-60" />}
            </span>
            <div className="min-w-0">
              <p className="text-[10px] font-semibold tracking-wider text-muted uppercase">AI service</p>
              <p className="truncate text-xs font-medium text-foreground" title={aiStatus.label}>{aiStatus.label}</p>
            </div>
          </div>
        )}
        <UserMenu user={user} rail={rail} onSignOut={onSignOut} onNavigate={(v) => { onNavigate(v); onMobileOpenChange(false); }} />
        <button
          type="button"
          onClick={toggleCollapsed}
          className="hidden w-full cursor-pointer items-center justify-center gap-2 rounded-md py-1.5 text-xs text-muted hover:bg-surface-2 hover:text-foreground lg:flex"
          aria-label={rail ? "Expand sidebar" : "Collapse sidebar"}
        >
          {rail ? <ChevronsRight className="size-4" /> : <><ChevronsLeft className="size-4" /> Collapse</>}
        </button>
      </div>
    </div>
  );

  return (
    <>
      <aside className={cn("hidden h-full min-h-0 shrink-0 border-r border-border bg-sidebar transition-[width] duration-200 lg:block rtl:border-r-0 rtl:border-l", collapsed ? "w-[68px]" : "w-[252px]")}>
        {content(collapsed)}
      </aside>
      <D.Root open={mobileOpen} onOpenChange={onMobileOpenChange}>
        <D.Portal>
          <D.Overlay className="fixed inset-0 z-40 bg-black/40 lg:hidden" />
          <D.Content className="fixed inset-y-0 left-0 z-50 w-[280px] max-w-[85vw] bg-sidebar shadow-lg outline-none lg:hidden rtl:right-0 rtl:left-auto">
            <D.Title className="sr-only">Navigation</D.Title>
            <D.Description className="sr-only">Pages in {brandName}</D.Description>
            <D.Close className="absolute top-3 right-3 z-10 grid size-8 cursor-pointer place-items-center rounded-md text-muted hover:bg-surface-2 rtl:right-auto rtl:left-3" aria-label="Close navigation">
              <X className="size-4" />
            </D.Close>
            {content(false)}
          </D.Content>
        </D.Portal>
      </D.Root>
    </>
  );
}

function NavButton({ item, active, rail, onClick }: { item: NavEntry; active: boolean; rail: boolean; onClick: () => void }) {
  const Icon = item.icon;
  const button = (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      aria-label={rail ? item.label : undefined}
      className={cn(
        "relative flex h-9 w-full cursor-pointer items-center gap-2.5 rounded-md text-[13px] font-medium outline-none transition-colors focus-visible:ring-[3px] focus-visible:ring-ring",
        rail ? "justify-center px-0" : "px-2.5",
        active ? "bg-primary-soft text-primary" : "text-foreground-2 hover:bg-surface-2 hover:text-foreground",
      )}
    >
      {active && !rail && <span className="absolute top-2 bottom-2 left-0 w-[3px] rounded-r-full bg-primary rtl:right-0 rtl:left-auto" aria-hidden />}
      <Icon className={cn("size-[17px] shrink-0", active ? "text-primary" : "text-muted")} />
      {!rail && <span className="truncate">{item.label}</span>}
    </button>
  );
  return rail ? <Tooltip content={item.label} side="right">{button}</Tooltip> : button;
}

function CommandTrigger({ rail, onClick }: { rail: boolean; onClick: () => void }) {
  if (rail) {
    return (
      <Tooltip content="Search pages (Ctrl K)" side="right">
        <button type="button" onClick={onClick} aria-label="Search pages" className="grid h-9 w-full cursor-pointer place-items-center rounded-md border border-border bg-surface text-muted hover:border-border-strong hover:text-foreground">
          <Search className="size-4" />
        </button>
      </Tooltip>
    );
  }
  return (
    <button type="button" onClick={onClick} className="flex h-9 w-full cursor-pointer items-center gap-2 rounded-md border border-border bg-surface px-2.5 text-[13px] text-muted shadow-xs hover:border-border-strong hover:text-foreground-2">
      <Search className="size-4" />
      <span className="flex-1 text-left">Search pages…</span>
      <kbd className="rounded border border-border bg-surface-2 px-1.5 font-sans text-[10px] font-medium text-muted">Ctrl K</kbd>
    </button>
  );
}

function roleLabel(role: User["role"]) {
  return role === "admin" ? "Administrator" : role === "manager" ? "Department manager" : "Employee";
}

function UserMenu({ user, rail, onSignOut, onNavigate }: { user: User; rail: boolean; onSignOut: () => void; onNavigate: (view: string) => void }) {
  const [rtl, setRtl] = useState(() => {
    try { const saved = localStorage.getItem("infraspine_direction"); if (saved) { document.documentElement.dir = saved; return saved === "rtl"; } } catch { /* ignore */ }
    return document.documentElement.dir === "rtl";
  });
  const [theme, setThemeState] = useState<ThemeChoice>(savedTheme);
  function chooseTheme(next: ThemeChoice) {
    setTheme(next);
    setThemeState(next);
  }
  function toggleDirection() {
    const dir = rtl ? "ltr" : "rtl";
    document.documentElement.dir = dir;
    try { localStorage.setItem("infraspine_direction", dir); } catch { /* ignore */ }
    setRtl(!rtl);
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button type="button" className={cn("flex w-full cursor-pointer items-center gap-2.5 rounded-lg p-1.5 text-left outline-none hover:bg-surface-2 focus-visible:ring-[3px] focus-visible:ring-ring", rail && "justify-center")} aria-label="Account menu">
          <span className="grid size-8 shrink-0 place-items-center rounded-full bg-primary-soft text-xs font-semibold text-primary">{initials(user.email)}</span>
          {!rail && (
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px] font-medium text-foreground" title={user.email}>{user.email}</span>
              <span className="block truncate text-[11px] text-muted">{roleLabel(user.role)}</span>
            </span>
          )}
          {!rail && <ChevronDown className="size-4 shrink-0 text-muted" />}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align={rail ? "start" : "end"} className="w-60">
        <DropdownMenuLabel className="normal-case tracking-normal">
          <span className="block truncate text-[13px] font-medium text-foreground">{user.email}</span>
          <span className="block text-[11px] font-normal text-muted">{roleLabel(user.role)}</span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => onNavigate("security")}><ShieldCheck /> Account security</DropdownMenuItem>
        <DropdownMenuItem onSelect={toggleDirection}><Languages /> {rtl ? "Left-to-right layout" : "Right-to-left layout (Urdu)"}</DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuLabel>Theme</DropdownMenuLabel>
        <div className="grid grid-cols-3 gap-1 px-1 pb-1" role="radiogroup" aria-label="Theme">
          {([["light", Sun, "Light"], ["dark", Moon, "Dark"], ["system", Monitor, "System"]] as const).map(([value, Icon, label]) => (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={theme === value}
              onClick={() => chooseTheme(value)}
              className={cn("flex cursor-pointer flex-col items-center gap-1 rounded-md border px-1 py-1.5 text-[11px] font-medium outline-none transition-colors focus-visible:ring-[3px] focus-visible:ring-ring [&_svg]:size-4", theme === value ? "border-primary bg-primary-soft text-primary" : "border-transparent text-foreground-2 hover:bg-surface-2")}
            >
              <Icon />{label}
            </button>
          ))}
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem danger onSelect={onSignOut}><LogOut /> Sign out</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** Ctrl/⌘ K page switcher. */
export function CommandPalette({ groups, onNavigate }: { groups: NavGroup[]; onNavigate: (view: string) => void }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setOpen((o) => !o); }
    };
    const onOpen = () => setOpen(true);
    window.addEventListener("keydown", onKey);
    window.addEventListener("infraspine:open-command", onOpen);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("infraspine:open-command", onOpen); };
  }, []);

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    return groups.flatMap((g) => g.items.map((item) => ({ ...item, group: g.label })))
      .filter((item) => !q || `${item.label} ${item.group} ${item.keywords ?? ""}`.toLowerCase().includes(q));
  }, [groups, query]);

  useEffect(() => { setIndex(0); }, [query, open]);
  useEffect(() => { listRef.current?.querySelector<HTMLElement>(`[data-index="${index}"]`)?.scrollIntoView({ block: "nearest" }); }, [index]);

  function go(view: string) { onNavigate(view); setOpen(false); setQuery(""); }

  return (
    <D.Root open={open} onOpenChange={setOpen}>
      <D.Portal>
        <D.Overlay className="fixed inset-0 z-50 bg-black/40 backdrop-blur-[2px]" />
        <D.Content className="fixed top-[15vh] left-1/2 z-50 w-[calc(100vw-32px)] max-w-lg -translate-x-1/2 overflow-hidden rounded-xl border border-border bg-surface shadow-lg outline-none data-[state=open]:animate-in">
          <D.Title className="sr-only">Search pages</D.Title>
          <D.Description className="sr-only">Type to find a page, then press Enter.</D.Description>
          <div className="flex items-center gap-2 border-b border-border px-4">
            <Search className="size-4 text-muted" />
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") { e.preventDefault(); setIndex((i) => Math.min(i + 1, results.length - 1)); }
                else if (e.key === "ArrowUp") { e.preventDefault(); setIndex((i) => Math.max(i - 1, 0)); }
                else if (e.key === "Enter" && results[index]) { e.preventDefault(); go(results[index].view); }
              }}
              placeholder="Search pages…"
              aria-label="Search pages"
              className="h-12 flex-1 border-0 bg-transparent text-sm text-foreground shadow-none outline-none placeholder:text-muted focus:shadow-none"
            />
            <kbd className="rounded border border-border bg-surface-2 px-1.5 text-[10px] text-muted">Esc</kbd>
          </div>
          <ul ref={listRef} className="scrollbar-thin max-h-[50vh] overflow-y-auto p-1.5" role="listbox">
            {results.length === 0 && <li className="px-3 py-8 text-center text-[13px] text-muted">No pages match “{query}”.</li>}
            {results.map((item, i) => {
              const Icon = item.icon;
              return (
                <li key={item.view} data-index={i} role="option" aria-selected={i === index}>
                  <button
                    type="button"
                    onMouseMove={() => setIndex(i)}
                    onClick={() => go(item.view)}
                    className={cn("flex w-full cursor-pointer items-center gap-3 rounded-md px-3 py-2 text-left text-[13px]", i === index ? "bg-primary-soft text-primary" : "text-foreground-2")}
                  >
                    <Icon className="size-4 shrink-0" />
                    <span className="flex-1 truncate font-medium">{item.label}</span>
                    <span className="text-[11px] text-muted">{item.group}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </D.Content>
      </D.Portal>
    </D.Root>
  );
}
