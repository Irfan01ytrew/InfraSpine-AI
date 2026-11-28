import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const styles = readFileSync(join(root, "src", "styles.css"), "utf8");

const read = (rel) => readFileSync(join(root, rel), "utf8");

// Agent 65, group 3 — Reports: chart legend at the 12px floor. The daily
// rows, Stat labels/hints (12px) and provider table (now 12px headers /
// 14px body via the shared Table fix in group 2) already sit on scale.
const REPORTS_FILES = [
  "src/components/ReportsPage.tsx",
  "src/components/ChartView.tsx",
];

test("page type scale: shared Badge and PageHeader eyebrow are at the 12px floor", () => {
  const primitives = read("src/components/ui/primitives.tsx");
  assert.ok(!primitives.includes("text-[11px]"), "no 11px text may remain in the shared ui primitives");
  assert.ok(primitives.includes("py-0.5 text-xs font-medium"), "Badge must be 12px");
  assert.ok(primitives.includes('mb-1 text-xs font-semibold tracking-wider'), "PageHeader eyebrow must be 12px");
});

test("page type scale: no sub-12px text in the reports pages", () => {
  for (const file of REPORTS_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 65, group 2 — People + Org: "This is you" at the 12px floor; the
// shared pill at 12px (DepartmentManagerPage counts on it); the shared
// ui Table now matches the shared table style (12px headers / 14px body).
// Note: .pill/.pill-sm are defined twice in styles.css (like the .card
// duplication) — both definitions are aligned at 12px here; removing the
// duplicate is a repo-wide decision, not a per-group edit.
const PEOPLE_FILES = [
  "src/components/PeoplePage.tsx",
  "src/components/InviteEmployeesPage.tsx",
  "src/components/DepartmentManagerPage.tsx",
];

test("page type scale: pill and shared Table use the scale", () => {
  assert.ok(!/\.pill \{[^}]*font-size: 11px/s.test(styles), "no 11px .pill may remain");
  assert.ok(!/\.pill-sm \{[^}]*font-size: (10\.5|11)px/s.test(styles), "no sub-12px .pill-sm may remain");
  const primitives = read("src/components/ui/primitives.tsx");
  assert.ok(primitives.includes("[&_th]:text-xs"), "shared Table headers must be 12px");
  assert.ok(!primitives.includes("[&_th]:text-[11.5px]"), "no 11.5px Table header may remain");
  assert.ok(primitives.includes('text-left text-sm'), "shared Table body must be 14px");
});

test("page type scale: no sub-12px text in the people and org pages", () => {
  for (const file of PEOPLE_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 65, group 1 — Home + Approvals: eyebrow, KPI hints, chart legend,
// tooltip and day labels at the 12px floor; the shared chip-sm at 12px
// (ApprovalsPage and its PolicyImpactSection use it for every chip).
const HOME_FILES = [
  "src/components/HomePage.tsx",
  "src/components/HomeDashboard.tsx",
  "src/components/ApprovalsPage.tsx",
  "src/components/ApprovalDelegationCard.tsx",
  "src/components/PolicyImpactSection.tsx",
];

test("page type scale: chip-sm is at the 12px floor", () => {
  assert.match(styles, /\.chip-sm \{[^}]*font-size: 12px/s, "chip-sm must be 12px");
  assert.ok(!/\.chip-sm \{[^}]*font-size: 11\.5px/s.test(styles), "no 11.5px chip-sm may remain");
});

test("page type scale: no sub-12px text in the home and approvals pages", () => {
  for (const file of HOME_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 41, group 2 — Finance detail views: one shared table style
// (headers 12px, body 14px), forecast leftovers at the 12/13px floor,
// empty states at the 14px base.
const FINANCE_FILES = [
  "src/finance/FinancePage.tsx",
  "src/components/BudgetsPage.tsx",
  "src/components/ForecastsPage.tsx",
  "src/components/ClaimsPage.tsx",
];

test("page type scale: shared table style is 12px headers / 14px body", () => {
  assert.match(styles, /\.table-wrap th \{[^}]*font-size: 12px/s, "table-wrap headers must be 12px");
  assert.ok(!/\.table-wrap th \{[^}]*font-size: (10\.5|11\.5)px/s.test(styles), "no 10.5/11.5px table-wrap header may remain");
  assert.match(styles, /\.table-wrap td \{ font-size: 14px; \}/, "table-wrap body must be 14px");
  assert.match(styles, /\.parser-table thead th \{[^}]*font-size: 12px/s, "parser-table headers must be 12px");
  assert.match(styles, /\.parser-table \{[^}]*font-size: 14px/s, "parser-table body must be 14px");
  assert.match(styles, /\.chart-table \{[^}]*font-size: 14px/s, "chart-table body must be 14px");
});

test("page type scale: forecast page and empty states use the scale", () => {
  assert.match(styles, /\.forecast-observation legend \{[^}]*font-size: 13px/s, "forecast legend must be 13px");
  assert.match(styles, /\.forecast-digest \{[^}]*font-size: 12px/s, "forecast digest must be 12px");
  assert.match(styles, /\.empty \{[^}]*font-size: 14px/s, "empty states must be 14px");
});

test("page type scale: no sub-12px text in the finance pages", () => {
  for (const file of FINANCE_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});
// Agent 41, group 3 — Settings tabs + ModuleWorkspaces: form labels at
// the 13px label scale, tab description at the 14px base, nothing below
// the 12px floor.
const SETTINGS_FILES = [
  "src/components/SettingsPage.tsx",
  "src/components/ModuleWorkspaces.tsx",
  "src/components/OrgSettingsSection.tsx",
  "src/components/OrgStructureSection.tsx",
];

test("page type scale: settings description and workspace labels use the scale", () => {
  assert.ok(
    read("src/components/SettingsPage.tsx").includes('<p className="mb-5 text-sm text-muted">'),
    "settings tab description must be 14px",
  );
  const workspaces = read("src/components/ModuleWorkspaces.tsx");
  assert.ok(!workspaces.includes("gap-1 text-xs font-semibold"), "workspace form labels must be 13px, not 12px");
});

test("page type scale: no sub-12px text in the settings pages", () => {
  for (const file of SETTINGS_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 41, group 1 — Agent Studio pages: the type scale agreed in the
// redesign (base 14px, labels 13px, metadata 12px) must hold, and no text
// may drop below the 12px floor.
const STUDIO_FILES = [
  "src/components/AgentStudioPage.tsx",
  "src/components/AgentStudioAnalyticsPanel.tsx",
  "src/components/AgentStudioWhatsAppPanel.tsx",
  "src/components/AgentStudioWhatsAppQrPanel.tsx",
  "src/components/AgentCatalogPage.tsx",
  "src/components/AgentCatalogReviewSection.tsx",
  "src/components/AgentRunRetentionPanel.tsx",
];

test("page type scale: studio form labels use the 13px label scale", () => {
  assert.match(
    styles,
    /\.automation-form \.field, \.automation-approval \.field \{[^}]*font-size: 13px/s,
    "automation form fields must be 13px like .field-label",
  );
  assert.match(
    styles,
    /\.agent-tools legend \{[^}]*font-size: 13px/s,
    "agent-tools legend must be 13px",
  );
});

test("page type scale: no sub-12px text in the studio pages", () => {
  for (const file of STUDIO_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 77, group 1 — Automation + Handoffs: workflow node-kind label at
// the 12px floor (was 10px). HandoffsPage leans on the shared chip-sm
// (12px) and .pill/.pill-sm (12px); its .small (12.5px) and the duplicated
// .field-label (12px effective) are left untouched pending the repo-wide
// decision, as is the duplicated .btn-sm (13px vs 12px).
const AUTOMATION_FILES = [
  "src/components/AutomationPage.tsx",
  "src/components/HandoffsPage.tsx",
];

test("page type scale: workflow node-kind label is at the 12px floor", () => {
  assert.match(styles, /\.workflow-node-kind \{[^}]*font-size: 12px/s, "workflow node-kind must be 12px");
  assert.ok(!/\.workflow-node-kind \{[^}]*font-size: 10px/s.test(styles), "no 10px workflow node-kind may remain");
});

test("page type scale: no sub-12px text in the automation and handoffs pages", () => {
  for (const file of AUTOMATION_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 77, group 2 — Training + Meetings: no source change needed, both
// pages already sit on scale — metadata/empty states at text-xs (12px),
// form labels at text-[13px]. Guards only, locking the scale in place.
const TRAINING_FILES = [
  "src/components/TrainingPage.tsx",
  "src/components/MeetingsPage.tsx",
];

test("page type scale: training and meetings labels use the 13px scale", () => {
  for (const file of TRAINING_FILES) {
    const source = read(file);
    assert.ok(source.includes("text-[13px]"), `${file} must keep its 13px labels`);
  }
});

test("page type scale: no sub-12px text in the training and meetings pages", () => {
  for (const file of TRAINING_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 77, group 3 — AuditLog + Traces: mono stack trace at the 12px
// floor (was 11.5px). AuditLog leans on the shared chip-sm/.pill (12px),
// .segment (12px) and .timeline-meta/.timeline-time (12.5px); TracesPage
// stats at 12.5px labels/hints and 16px values. Nothing else below the
// floor in either file.
const AUDIT_FILES = [
  "src/components/AuditLog.tsx",
  "src/components/TracesPage.tsx",
];

test("page type scale: trace stack is at the 12px floor", () => {
  assert.match(styles, /\.trace-stack \{[^}]*font-size: 12px/s, "trace stack must be 12px");
  assert.ok(!/\.trace-stack \{[^}]*font-size: 11\.5px/s.test(styles), "no 11.5px trace stack may remain");
});

test("page type scale: no sub-12px text in the audit log and traces pages", () => {
  for (const file of AUDIT_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 79, group 1 — Webhooks + CloudConnectors + ProviderPanel: the only
// sub-12px text was the last-synced timestamp on CloudConnectorsPage (11px,
// now text-xs = 12px). WebhooksPage and ProviderPanel were already on
// scale (text-xs metadata, 13px labels, 14px body). Shared classes used
// here sit at the floor: .chip/.chip-sm 12px, .model-card-id 12px,
// .field-label 12px effective, .btn-sm 12px effective (later definition).
const INTEGRATIONS_FILES = [
  "src/components/WebhooksPage.tsx",
  "src/components/CloudConnectorsPage.tsx",
  "src/components/ProviderPanel.tsx",
];

test("page type scale: cloud connector last-synced timestamp is at the 12px floor", () => {
  const source = read("src/components/CloudConnectorsPage.tsx");
  assert.ok(!source.includes("text-[11px]"), "no 11px timestamp may remain on the cloud connectors page");
  assert.ok(source.includes("text-xs text-muted"), "last-synced timestamp must be 12px (text-xs)");
});

test("page type scale: no sub-12px text in the webhooks, cloud connectors and provider panel pages", () => {
  for (const file of INTEGRATIONS_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 79, group 2 — Knowledge suite: 10 spots on KnowledgePage (upload
// hint, doc meta line, doc error message, ephemeral voice note, composer
// footer hint, citation chips at 10.5px, page caption, Sources heading,
// provider/model line) and 3 on KnowledgeDashboard (metric note, "Fetched"
// timestamp, outcomes footnote) moved from 11px/10.5px to text-xs = 12px.
// OrgKnowledgePage and KnowledgeInterviewsPage were already on scale —
// they lean on the shared btn/.field-label/.pill/.card/.chip (all 12px+).
// Dead CSS found, left untouched: .doc-meta (11.5px) and .sources-label
// (11px) in styles.css have zero usages anywhere in src/.
const KNOWLEDGE_FILES = [
  "src/components/KnowledgePage.tsx",
  "src/components/OrgKnowledgePage.tsx",
  "src/components/KnowledgeDashboard.tsx",
  "src/components/KnowledgeInterviewsPage.tsx",
];

test("page type scale: knowledge page hints, meta lines and citation chips are at the 12px floor", () => {
  const source = read("src/components/KnowledgePage.tsx");
  assert.ok(!source.includes("text-[11px]"), "no 11px text may remain on the knowledge page");
  assert.ok(!source.includes("text-[10.5px]"), "no 10.5px text may remain on the knowledge page");
  assert.ok(source.includes("text-xs leading-snug text-muted"), "upload hint must be 12px (text-xs)");
  assert.ok(source.includes("text-xs font-semibold tracking-[0.08em]"), "Sources heading must be 12px (text-xs)");
});

test("page type scale: knowledge dashboard notes are at the 12px floor", () => {
  const source = read("src/components/KnowledgeDashboard.tsx");
  assert.ok(!source.includes("text-[11px]"), "no 11px text may remain on the knowledge dashboard");
  assert.ok(source.includes("mt-2 block text-xs text-muted"), "metric note must be 12px (text-xs)");
});

test("page type scale: no sub-12px text in the knowledge suite pages", () => {
  for (const file of KNOWLEDGE_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 79, group 3 — ServiceRequests + RequestForms + MyWork: 5 spots
// moved to text-xs = 12px — the approval-stage label (ServiceRequests),
// the "Updated" timestamp (RequestForms), and the MyWork calendar's
// weekday headers, today-date circle and "N due" count badge (10px).
// Everything else in the group was already on scale (text-xs metadata,
// 13px body/labels). MyWork leans on the shared .small (12.5px, known
// pending repo-wide decision) and .field-label/.chip (12px effective).
const MYWORK_FILES = [
  "src/components/ServiceRequestsPage.tsx",
  "src/components/RequestFormsPage.tsx",
  "src/components/MyWorkPage.tsx",
];

test("page type scale: my-work calendar labels and counts are at the 12px floor", () => {
  const source = read("src/components/MyWorkPage.tsx");
  assert.ok(!source.includes("text-[11px]"), "no 11px text may remain on the my-work page");
  assert.ok(!source.includes("text-[10px]"), "no 10px text may remain on the my-work page");
  assert.ok(source.includes("py-1 text-center text-xs font-semibold text-muted"), "weekday headers must be 12px (text-xs)");
  assert.ok(source.includes("work-calendar-count max-w-full truncate rounded-full bg-warning-soft px-1.5 text-xs font-medium"), "due-count badge must be 12px (text-xs)");
});

test("page type scale: no sub-12px text in the service requests, request forms and my-work pages", () => {
  for (const file of MYWORK_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 86, group 1 — Login + Onboarding + AdminConsole: already on scale,
// guards only. The login hero eyebrow is 12px (.login-eyebrow), the login
// footer 12.5px, and the shell leans on the shared .pill (12px) and
// .field-label (12px effective — known pending repo-wide decision).
const ENTRY_FILES = [
  "src/components/LoginPage.tsx",
  "src/components/OnboardingPage.tsx",
  "src/components/AdminConsole.tsx",
];

test("page type scale: login eyebrow and admin shell are at the 12px floor", () => {
  assert.ok(/\.login-eyebrow \{[^}]*font-size: 12px/s.test(styles), "login eyebrow must be 12px");
  assert.ok(!/\.login-eyebrow \{[^}]*font-size: (10|10\.5|11|11\.5)px/s.test(styles), "no sub-12px login eyebrow may remain");
  const login = read("src/components/LoginPage.tsx");
  assert.ok(login.includes("login-eyebrow"), "login page must keep its eyebrow");
});

test("page type scale: no sub-12px text in the login, onboarding and admin console pages", () => {
  for (const file of ENTRY_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 86, group 2 — ModuleControlCenter + Workspaces + CompanySetup: 21 spots
// moved to text-xs = 12px — module rows (MODULE label, assistant status,
// mono path chips, thread hints and timestamps, approval/owner notes,
// stat-card and tile labels), workspace member counts, section headings,
// avatar initials and stat labels, and the company-setup template version
// and definition labels.
const CONTROL_FILES = [
  "src/components/ModuleControlCenterPage.tsx",
  "src/components/WorkspacesPage.tsx",
  "src/components/CompanySetupPage.tsx",
];

test("page type scale: module control center labels and notes are at the 12px floor", () => {
  const source = read("src/components/ModuleControlCenterPage.tsx");
  assert.ok(!source.includes("text-[11px]"), "no 11px text may remain on the module control center page");
  assert.ok(!source.includes("text-[10px]"), "no 10px text may remain on the module control center page");
  assert.ok(source.includes("font-mono text-xs text-foreground-2"), "module path chips must be 12px (text-xs)");
  assert.ok(source.includes("mt-1 text-xs text-muted"), "run timestamps must be 12px (text-xs)");
});

test("page type scale: workspaces and company setup labels are at the 12px floor", () => {
  const workspaces = read("src/components/WorkspacesPage.tsx");
  assert.ok(!workspaces.includes("text-[11px]"), "no 11px text may remain on the workspaces page");
  assert.ok(workspaces.includes("block text-xs text-muted"), "workspace member counts must be 12px (text-xs)");
  const setup = read("src/components/CompanySetupPage.tsx");
  assert.ok(!setup.includes("text-[11px]"), "no 11px text may remain on the company setup page");
});

test("page type scale: no sub-12px text in the module control center, workspaces and company setup pages", () => {
  for (const file of CONTROL_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 86, group 3 — DatabaseAnalysis + EmbeddingModels + Expertise: 3 spots
// moved to text-xs = 12px — the database-analysis "Read-only calculations"
// caption, and the two <small> hints on the expertise page. The <small>
// element renders at 80% of the 14px body (11.2px) under Tailwind preflight,
// so text-xs is added while keeping the <small> element (the
// .expertise-form-footer small / .expertise-result > small layout rules in
// styles.css still apply). EmbeddingModels was already on scale
// (.stat-label 12.5px, .embedding-model-device 12px). Note: the database
// analysis surface is DatabaseAnalysisPanel.tsx — there is no
// DatabaseAnalysisPage.tsx.
const ANALYSIS_FILES = [
  "src/components/DatabaseAnalysisPanel.tsx",
  "src/components/EmbeddingModelsPage.tsx",
  "src/components/ExpertisePage.tsx",
];

test("page type scale: database analysis caption and expertise hints are at the 12px floor", () => {
  const panel = read("src/components/DatabaseAnalysisPanel.tsx");
  assert.ok(!panel.includes("text-[11px]"), "no 11px text may remain in the database analysis panel");
  assert.ok(panel.includes("block text-xs text-muted"), "database analysis caption must be 12px (text-xs)");
  const expertise = read("src/components/ExpertisePage.tsx");
  assert.ok(expertise.includes('<small className="muted text-xs">'), "expertise hints must be 12px (text-xs)");
  assert.ok(!expertise.includes('<small className="muted">'), "no bare <small> hint may remain on the expertise page");
});

test("page type scale: no sub-12px text in the database analysis, embedding models and expertise pages", () => {
  for (const file of ANALYSIS_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 98, group 1 - AppSidebar + ui/overlays: 10 inline spots moved to
// text-xs = 12px - the nav group toggle, "AI service" label, Ctrl K and Esc
// kbd hints, both role labels, the theme picker buttons and the page
// switcher group label (AppSidebar), plus the shared DropdownMenuLabel and
// the Tabs count badge (overlays.tsx, used by AdminConsole, AgentStudio,
// Knowledge, People, ServiceRequests, Settings, Workspaces, Finance and
// CreateRoleDialog). The sidebar CSS rules .nav-section (10px),
// .sidebar-status-label (11px) and .user-chip-role (11px, live in App.tsx)
// are aligned at 12px; .nav-section and .sidebar-status-label have no TSX
// usages (dead CSS, like .doc-meta) but are fixed per the round-6 brief.
const SHELL_FILES = [
  "src/components/AppSidebar.tsx",
  "src/components/ui/overlays.tsx",
];

test("page type scale: sidebar shell rules are at the 12px floor", () => {
  assert.match(styles, /\.nav-section \{[^}]*font-size: 12px/s, "nav-section must be 12px");
  assert.ok(!/\.nav-section \{[^}]*font-size: 10px/s.test(styles), "no 10px nav-section may remain");
  assert.match(styles, /\.sidebar-status-label \{[^}]*font-size: 12px/s, "sidebar status label must be 12px");
  assert.ok(!/\.sidebar-status-label \{[^}]*font-size: 11px/s.test(styles), "no 11px sidebar status label may remain");
  assert.match(styles, /\.user-chip-role \{[^}]*font-size: 12px/s, "user chip role must be 12px");
  assert.ok(!/\.user-chip-role \{[^}]*font-size: 11px/s.test(styles), "no 11px user chip role may remain");
});

test("page type scale: sidebar labels and kbd hints are at the 12px floor", () => {
  const sidebar = read("src/components/AppSidebar.tsx");
  assert.ok(!sidebar.includes("text-[11px]"), "no 11px text may remain in the app sidebar");
  assert.ok(!sidebar.includes("text-[10px]"), "no 10px text may remain in the app sidebar");
  assert.ok(sidebar.includes("text-xs font-semibold tracking-[0.08em]"), "nav group toggle must be 12px (text-xs)");
  assert.ok(sidebar.includes("text-xs font-semibold tracking-wider text-muted uppercase"), "AI service label must be 12px (text-xs)");
  assert.ok(sidebar.includes("px-1.5 font-sans text-xs font-medium"), "Ctrl K hint must be 12px (text-xs)");
  assert.ok(sidebar.includes('text-xs text-muted">{item.group}'), "page switcher group label must be 12px (text-xs)");
  const overlays = read("src/components/ui/overlays.tsx");
  assert.ok(!overlays.includes("text-[11px]"), "no 11px text may remain in the ui overlays");
  assert.ok(!overlays.includes("text-[10px]"), "no 10px text may remain in the ui overlays");
  assert.ok(overlays.includes("text-xs font-semibold tracking-wide text-muted uppercase"), "dropdown menu label must be 12px (text-xs)");
  assert.ok(overlays.includes("px-1.5 text-xs leading-4"), "tabs count badge must be 12px (text-xs)");
});

test("page type scale: no sub-12px text in the app sidebar and ui overlays", () => {
  for (const file of SHELL_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 98, group 2 - Research + MarketResearch: 4 spots moved to
// text-xs = 12px - the browser-run id code on ResearchPage (11px) and the
// three bare <small> elements (citation location and snippet on
// ResearchPage, source URL on MarketResearchPage), which render at 11.2px
// under Tailwind preflight. The <small> elements are kept so the
// .research-source small color rule still applies; MarketResearchPage had
// no other sub-12px text. This follows Agent 86's in-group precedent for
// <small>; the repo-wide decision (global small rule vs per-page text-xs)
// is still open.
const RESEARCH_FILES = [
  "src/components/ResearchPage.tsx",
  "src/components/MarketResearchPage.tsx",
];

test("page type scale: research run id and citation hints are at the 12px floor", () => {
  const research = read("src/components/ResearchPage.tsx");
  assert.ok(!research.includes("text-[11px]"), "no 11px text may remain on the research page");
  assert.ok(research.includes('<code className="text-xs text-muted">{run.id.slice(0, 8)}</code>'), "browser run id must be 12px (text-xs)");
  assert.ok(research.includes('<small className="text-xs">{citation.location'), "citation location must be 12px (text-xs)");
  assert.ok(research.includes('<small className="text-xs">{citation.snippet}</small>'), "citation snippet must be 12px (text-xs)");
  assert.ok(!research.includes("<small>{citation"), "no bare <small> citation hint may remain on the research page");
  const market = read("src/components/MarketResearchPage.tsx");
  assert.ok(!market.includes("text-[11px]"), "no 11px text may remain on the market research page");
  assert.ok(market.includes('<small className="text-xs">{source.url}</small>'), "source URL must be 12px (text-xs)");
});

test("page type scale: no sub-12px text in the research and market research pages", () => {
  for (const file of RESEARCH_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 98, group 3 - PartnerPortal + PartnerWorkspaces + ProposalCoverage:
// 18 spots moved to the 12px floor - 14 bare <small> elements now carry
// text-xs (1 citation location on PartnerPortal; invitation, identity and
// source status lines on PartnerWorkspaces; the RFP picker hint, evidence
// location/excerpt pairs, case-row summary, origin-evidence pairs,
// citation pairs and review-history pairs on ProposalCoverage), and 4 CSS
// rules aligned at 12px (.partner-question/.partner-answer-heading spans
// 11px, .partner-evidence-state 11px, .partner-footer 11.5px,
// .partner-admin-list small 11px - all used only by these pages). The CSS
// fixes matter because the unlayered styles.css rules beat the layered
// Tailwind text-xs utility, so text-xs alone would not have moved the
// partner-admin-list smalls. The <small> elements are kept so the
// .research-source small / .proposal-*-row small color rules still apply.
// No inline text-[10px]/text-[11px] existed in these three files.
const PARTNER_FILES = [
  "src/components/PartnerPortalPage.tsx",
  "src/components/PartnerWorkspacesPage.tsx",
  "src/components/ProposalCoveragePage.tsx",
];

test("page type scale: partner shell rules are at the 12px floor", () => {
  assert.match(styles, /\.partner-question > span, \.partner-answer-heading > span:first-child \{[^}]*font-size: 12px/s, "partner question heading must be 12px");
  assert.ok(!/\.partner-question > span, \.partner-answer-heading > span:first-child \{[^}]*font-size: 11px/s.test(styles), "no 11px partner question heading may remain");
  assert.match(styles, /\.partner-evidence-state \{[^}]*font-size: 12px/s, "partner evidence state must be 12px");
  assert.ok(!/\.partner-evidence-state \{[^}]*font-size: 11px/s.test(styles), "no 11px partner evidence state may remain");
  assert.match(styles, /\.partner-footer \{[^}]*font-size: 12px/s, "partner footer must be 12px");
  assert.ok(!/\.partner-footer \{[^}]*font-size: 11\.5px/s.test(styles), "no 11.5px partner footer may remain");
  assert.match(styles, /\.partner-admin-list small \{[^}]*font-size: 12px/s, "partner admin list small must be 12px");
  assert.ok(!/\.partner-admin-list small \{[^}]*font-size: 11px/s.test(styles), "no 11px partner admin list small may remain");
});

test("page type scale: partner and proposal small hints are at the 12px floor", () => {
  const portal = read("src/components/PartnerPortalPage.tsx");
  assert.ok(!portal.includes("text-[11px]"), "no 11px text may remain on the partner portal page");
  assert.ok(portal.includes('<small className="text-xs">{citation.location_label}</small>'), "partner citation location must be 12px (text-xs)");
  assert.ok(!portal.includes("<small>{citation"), "no bare <small> citation hint may remain on the partner portal page");
  const workspaces = read("src/components/PartnerWorkspacesPage.tsx");
  assert.ok(!workspaces.includes("text-[11px]"), "no 11px text may remain on the partner workspaces page");
  assert.ok(workspaces.includes('<small className="text-xs">{invitation.accepted_at'), "invitation status must be 12px (text-xs)");
  assert.ok(workspaces.includes('<small className="text-xs">{identity.is_active'), "identity status must be 12px (text-xs)");
  assert.ok(workspaces.includes('<small className="text-xs">{source.status}'), "source status must be 12px (text-xs)");
  assert.ok(!workspaces.includes("<small>{invitation"), "no bare <small> invitation status may remain on the partner workspaces page");
  const coverage = read("src/components/ProposalCoveragePage.tsx");
  assert.ok(!coverage.includes("text-[11px]"), "no 11px text may remain on the proposal coverage page");
  assert.ok(coverage.includes('<small className="muted text-xs">Select up to five'), "RFP picker hint must be 12px (text-xs)");
  assert.ok(coverage.includes('<small className="text-xs">{evidence.location}</small>'), "evidence location must be 12px (text-xs)");
  assert.ok(coverage.includes('<small className="text-xs">{row.department_name'), "case row summary must be 12px (text-xs)");
  assert.ok(coverage.includes('<small className="text-xs">Revision {review.revision_number}'), "review history must be 12px (text-xs)");
  assert.ok(!coverage.includes("<small>{evidence"), "no bare <small> evidence hint may remain on the proposal coverage page");
  assert.ok(!coverage.includes("<small>{row."), "no bare <small> case summary may remain on the proposal coverage page");
  assert.ok(!coverage.includes("<small>Revision"), "no bare <small> review hint may remain on the proposal coverage page");
});

test("page type scale: no sub-12px text in the partner and proposal pages", () => {
  for (const file of PARTNER_FILES) {
    const source = read(file);
    for (const tiny of ["text-[10px]", "text-[10.5px]", "text-[11px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 101, group 1 — ConversationSidebar + RecoveryPage + RunsPanel:
// the only 3 files left with inline sub-12px classes. ConversationSidebar
// section heading 10.5px, RecoveryPage metric detail 11px, RunsPanel
// blocked-reason + failure-code 11px (2 spots) — all moved to text-xs.
// Note: the .convo-group h4 11px rule in styles.css is dead CSS (zero TSX
// usages) and is left for the repo-wide dead-CSS decision; the sidebar
// heading here sits in a plain <section>, not a .convo-group.
const SIDEBAR_RECOVERY_FILES = [
  "src/components/ConversationSidebar.tsx",
  "src/recovery/RecoveryPage.tsx",
  "src/workflows/RunsPanel.tsx",
];

test("page type scale: sidebar, recovery and runs panel are at the 12px floor", () => {
  const sidebar = read("src/components/ConversationSidebar.tsx");
  assert.ok(!sidebar.includes("text-[10.5px]"), "no 10.5px text may remain in the conversation sidebar");
  assert.ok(sidebar.includes('pt-2 pb-1 text-xs font-semibold tracking-[0.08em]'), "sidebar section heading must be 12px (text-xs)");
  const recovery = read("src/recovery/RecoveryPage.tsx");
  assert.ok(!recovery.includes("text-[11px]"), "no 11px text may remain on the recovery page");
  assert.ok(recovery.includes('<p className="text-xs text-muted">{m.detail}</p>'), "recovery metric detail must be 12px (text-xs)");
  const runs = read("src/workflows/RunsPanel.tsx");
  assert.ok(!runs.includes("text-[11px]"), "no 11px text may remain in the runs panel");
  assert.ok(runs.includes('<div className="text-xs text-warning-text">{run.blocked_reason}</div>'), "runs blocked reason must be 12px (text-xs)");
  assert.ok(runs.includes('<div className="text-xs text-danger-text">{step.failure_code}</div>'), "runs failure code must be 12px (text-xs)");
});

test("page type scale: no sub-12px text in the sidebar, recovery and runs panel files", () => {
  for (const file of SIDEBAR_RECOVERY_FILES) {
    const source = read(file);
    for (const tiny of ["text-[9px]", "text-[10px]", "text-[10.5px]", "text-[11px]", "text-[11.5px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 101, group 2 — the last 7 bare <small>s: AgentCatalogPage citation
// location + snippet (2), DepartmentManagerPage invite-link note (1),
// ProcessIntelligencePage metric notes (4). Each now carries text-xs while
// keeping the <small> element so the .research-source small /
// .invite-link small color and layout rules still apply. No styles.css
// rule sets a font-size on any of these smalls (checked first, per the
// Agent 98 finding), so the layered text-xs utility beats the preflight
// small { font-size: 80% } base rule.
const SMALL_HINT_FILES = [
  "src/components/AgentCatalogPage.tsx",
  "src/components/DepartmentManagerPage.tsx",
  "src/process-intelligence/ProcessIntelligencePage.tsx",
];

test("page type scale: catalog, department and process-intelligence small hints are at the 12px floor", () => {
  const catalog = read("src/components/AgentCatalogPage.tsx");
  assert.ok(!catalog.includes("<small>{citation"), "no bare <small> citation hint may remain on the agent catalog page");
  assert.ok(catalog.includes('<small className="text-xs">{citation.location'), "catalog citation location must be 12px (text-xs)");
  assert.ok(catalog.includes('<small className="text-xs">{citation.snippet}</small>'), "catalog citation snippet must be 12px (text-xs)");
  const department = read("src/components/DepartmentManagerPage.tsx");
  assert.ok(!department.includes("<small>Only this department"), "no bare <small> invite note may remain on the department manager page");
  assert.ok(department.includes('<small className="text-xs">Only this department is assigned.'), "department invite note must be 12px (text-xs)");
  const process = read("src/process-intelligence/ProcessIntelligencePage.tsx");
  assert.ok(!process.includes("<small>{completed"), "no bare <small> completed-cases note may remain on the process intelligence page");
  assert.ok(!process.includes("<small>Completed cases only"), "no bare <small> completed-only note may remain on the process intelligence page");
  assert.ok(!process.includes("<small>{model.data.baseline"), "no bare <small> baseline note may remain on the process intelligence page");
  assert.ok(!process.includes("<small>{new Set(cases"), "no bare <small> sequences note may remain on the process intelligence page");
  assert.ok(process.includes('<small className="text-xs">{completed.length} completed'), "process completed-cases note must be 12px (text-xs)");
  assert.ok(process.includes('<small className="text-xs">Completed cases only</small>'), "process completed-only note must be 12px (text-xs)");
});

test("page type scale: no bare <small> in the catalog, department and process-intelligence files", () => {
  for (const file of SMALL_HINT_FILES) {
    const source = read(file);
    assert.ok(!source.includes("<small>"), `${file} must not contain a bare <small>`);
    for (const tiny of ["text-[9px]", "text-[10px]", "text-[10.5px]", "text-[11px]", "text-[11.5px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 101, group 3 — Feedback, MediaTranscription, OcrEngine,
// DocumentParsers, VectorStore, RoleTemplates: no source change needed.
// Verified by sweep: zero inline text-[9px]/text-[10px]/text-[10.5px]/
// text-[11px]/text-[11.5px], zero bare <small>, zero fontSize style props,
// and no usage of any sub-12px styles.css class (.slider-scale,
// .composer-foot, .voice-*, .timeline-time, .chart-tick, .memory-hint,
// .deployment-license-details, .starter-prompts, .sources-label, .doc-meta,
// .module-ux-hero-mark, .cg-map-*). Their shared classes sit on scale
// (.stat-label/.stat-hint 12.5px, .stat-value 16px, .ocr-engine-details
// h3 13px / ul 12.5px, .parser-table 14px, .parser-result-row td 12px,
// .trace-stack 12px). The one .small usage (MediaTranscriptionSection
// note) is 12.5px, on scale, and part of the repo-wide .small decision
// anyway. ModuleWorkspaces (live PPTX file) is untouched per the brief.
const SECTION_FILES = [
  "src/components/Feedback.tsx",
  "src/components/MediaTranscriptionSection.tsx",
  "src/components/OcrEngineSection.tsx",
  "src/components/DocumentParsersSection.tsx",
  "src/components/VectorStoreSection.tsx",
  "src/components/RoleTemplatesSection.tsx",
];

test("page type scale: feedback, media, ocr, parsers, vector store and role templates are already at the 12px floor", () => {
  for (const file of SECTION_FILES) {
    const source = read(file);
    for (const tiny of ["text-[9px]", "text-[10px]", "text-[10.5px]", "text-[11px]", "text-[11.5px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
    assert.ok(!source.includes("<small>"), `${file} must not contain a bare <small>`);
  }
});

// Agent 106, group 1 — CSS-rule-driven sub-12px spots on already-"done"
// pages: .slider-scale 11.5px (ProviderPanel), .timeline-time 11px inside
// the max-width:440px media query (AuditLog, TracesPage — the base rule is
// already 12.5px), .chart-tick 11px (ChartView SVG ticks), .memory-hint
// 11.5px (ClaimsPage, MemoryPage). Inline text in these files was fixed in
// earlier rounds; the unlayered styles.css rules still set sub-12px, and
// unlayered rules beat the layered Tailwind text-xs utility, so the fix
// had to land in CSS itself (Agent 98 finding).
const CSS_RULE_FILES = [
  "src/components/ProviderPanel.tsx",
  "src/components/AuditLog.tsx",
  "src/components/TracesPage.tsx",
  "src/components/ChartView.tsx",
  "src/components/ClaimsPage.tsx",
  "src/components/MemoryPage.tsx",
];

test("page type scale: slider scale, timeline time, chart ticks and memory hints are at the 12px floor", () => {
  assert.match(styles, /\.slider-scale \{[^}]*font-size: 12px/s, "slider scale must be 12px");
  assert.ok(!/\.slider-scale \{[^}]*font-size: 11\.5px/s.test(styles), "no 11.5px slider scale may remain");
  assert.match(styles, /\.timeline-time \{[^}]*font-size: 12\.5px/s, "timeline time base must stay 12.5px");
  assert.ok(!/\.timeline-time \{[^}]*font-size: 11px/s.test(styles), "no 11px timeline time may remain in the narrow media query");
  assert.match(styles, /\.chart-tick \{[^}]*font-size: 12px/s, "chart tick must be 12px");
  assert.ok(!/\.chart-tick \{[^}]*font-size: 11px/s.test(styles), "no 11px chart tick may remain");
  assert.match(styles, /\.memory-hint \{[^}]*font-size: 12px/s, "memory hint must be 12px");
  assert.ok(!/\.memory-hint \{[^}]*font-size: 11\.5px/s.test(styles), "no 11.5px memory hint may remain");
});

test("page type scale: no sub-12px text in the slider, timeline, chart and memory files", () => {
  for (const file of CSS_RULE_FILES) {
    const source = read(file);
    for (const tiny of ["text-[9px]", "text-[10px]", "text-[10.5px]", "text-[11px]", "text-[11.5px]"]) {
      assert.ok(!source.includes(tiny), `${file} must not contain ${tiny}`);
    }
  }
});

// Agent 106, group 2 — dead-CSS sweep, sizes only (no deletion: the
// repo-wide dead-CSS decision is still open). All 12 selectors verified
// zero TSX usages by grep at commit time; .nav-section and
// .sidebar-status-label were already 12px (Agent 98) and need no change.
// Fixing the sizes keeps the "no sub-12px anywhere in styles.css"
// trajectory and means a future deletion is purely mechanical; if any
// rule is ever revived it renders on scale.
test("page type scale: dead css rules are at the 12px floor", () => {
  assert.match(styles, /\.convo-group h4 \{[^}]*font-size: 12px/s, "convo-group heading must be 12px");
  assert.ok(!/\.convo-group h4 \{[^}]*font-size: 11px/s.test(styles), "no 11px convo-group heading may remain");
  assert.match(styles, /\.starter-prompts button > span \{[^}]*font-size: 12px/s, "starter prompt hint must be 12px");
  assert.ok(!/\.starter-prompts button > span \{[^}]*font-size: 11\.5px/s.test(styles), "no 11.5px starter prompt hint may remain");
  assert.match(styles, /\.composer-foot \{[^}]*font-size: 12px/s, "composer foot must be 12px");
  assert.ok(!/\.composer-foot \{[^}]*font-size: 10px/s.test(styles), "no 10px composer foot may remain");
  assert.match(styles, /\.voice-question-button \{[^}]*font-size: 12px/s, "voice question button must be 12px");
  assert.ok(!/\.voice-question-button \{[^}]*font-size: 11px/s.test(styles), "no 11px voice question button may remain");
  assert.match(styles, /\.voice-privacy-note \{[^}]*font-size: 12px/s, "voice privacy note must be 12px");
  assert.ok(!/\.voice-privacy-note \{[^}]*font-size: 10px/s.test(styles), "no 10px voice privacy note may remain");
  assert.match(styles, /\.voice-ephemeral-note \{[^}]*font-size: 12px/s, "voice ephemeral note must be 12px");
  assert.ok(!/\.voice-ephemeral-note \{[^}]*font-size: 10px/s.test(styles), "no 10px voice ephemeral note may remain");
  assert.match(styles, /\.deployment-license-details dt \{[^}]*font-size: 12px/s, "deployment license detail term must be 12px");
  assert.ok(!/\.deployment-license-details dt \{[^}]*font-size: 11px/s.test(styles), "no 11px deployment license detail term may remain");
  assert.match(styles, /\.people-checks-compact label \{[^}]*font-size: 12px/s, "people checks compact label must be 12px");
  assert.ok(!/\.people-checks-compact label \{[^}]*font-size: 11px/s.test(styles), "no 11px people checks compact label may remain");
  assert.match(styles, /\.doc-meta \{[^}]*font-size: 12px/s, "doc meta must be 12px");
  assert.ok(!/\.doc-meta \{[^}]*font-size: 11\.5px/s.test(styles), "no 11.5px doc meta may remain");
  assert.match(styles, /\.sources-label \{[^}]*font-size: 12px/s, "sources label must be 12px");
  assert.ok(!/\.sources-label \{[^}]*font-size: 11px/s.test(styles), "no 11px sources label may remain");
});

// Agent 106, group 3 — ModuleWorkspaces + company graph: .module-ux-hero-mark
// 10px (ModuleWorkspaces.tsx IRFANIX mark), .cg-map-controls 11px and
// .cg-map-help 10px (GraphMap.tsx zoom % + help line), plus two same-surface
// SVG text rules found in the same company-graph block: .cg-node-kind
// 11px (node kind + version label) and .cg-edge text 10px (edge labels).
// Only styles.css changed — the ModuleWorkspaces.tsx live PPTX file is
// untouched; reconcile at merge per the brief.
test("page type scale: module workspace hero mark and company graph text are at the 12px floor", () => {
  assert.match(styles, /\.module-ux-hero-mark \{[^}]*font-size: 12px/s, "module hero mark must be 12px");
  assert.ok(!/\.module-ux-hero-mark \{[^}]*font-size: 10px/s.test(styles), "no 10px module hero mark may remain");
  assert.match(styles, /\.cg-map-controls \{[^}]*font-size: 12px/s, "map controls must be 12px");
  assert.ok(!/\.cg-map-controls \{[^}]*font-size: 11px/s.test(styles), "no 11px map controls may remain");
  assert.match(styles, /\.cg-map-help \{[^}]*font-size: 12px/s, "map help must be 12px");
  assert.ok(!/\.cg-map-help \{[^}]*font-size: 10px/s.test(styles), "no 10px map help may remain");
  assert.match(styles, /\.cg-node-kind \{[^}]*font: 12px/s, "graph node kind must be 12px");
  assert.ok(!/\.cg-node-kind \{[^}]*font: 11px/s.test(styles), "no 11px graph node kind may remain");
  assert.match(styles, /\.cg-edge text \{[^}]*font: 600 12px/s, "graph edge labels must be 12px");
  assert.ok(!/\.cg-edge text \{[^}]*font: 600 10px/s.test(styles), "no 10px graph edge labels may remain");
});

// Agent 106, group 1 addendum — .cite-chip 11px: found by the round-8
// final sweep. The citation-number chip (ChartView.tsx sources, grounded
// answer citations) is live on the already-"done" Knowledge/Chart pages
// and renders at 11px. Same category as group 1 (CSS-rule-driven spot on
// a done page), fixed in CSS for the same reason: the unlayered rule
// beats any layered utility.
test("page type scale: citation chips are at the 12px floor", () => {
  assert.match(styles, /\.cite-chip \{[^}]*font-size: 12px/s, "citation chip must be 12px");
  assert.ok(!/\.cite-chip \{[^}]*font-size: 11px/s.test(styles), "no 11px citation chip may remain");
});
