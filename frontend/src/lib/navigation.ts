/** UI visibility only. Every API remains responsible for tenant and action authorization. */
export type NavigationIdentity = { role: string; capabilities?: readonly string[] };
const adminViews = new Set(["home", "settings", "providers", "traces", "logs", "people", "deployment", "workspaces", "partner-workspaces", "connectors", "request-forms", "webhooks"]);
const sharedViews = new Set(["workspace", "experience", "knowledge", "memory", "claims", "org", "research", "proposals", "forecasts", "market-research", "training", "approvals", "handoffs", "meetings", "requests", "expertise", "my-work", "automation", "agents", "agent-studio", "interviews", "security"]);

export function canOpenView(view: string, user: NavigationIdentity): boolean {
  if (sharedViews.has(view)) return true;
  if (adminViews.has(view)) return user.role === "admin";
  if (view === "reports") return user.role === "admin" || !!user.capabilities?.includes("reports.view");
  if (view === "invite") return user.role === "admin" || !!user.capabilities?.includes("people.invite");
  if (view === "departments") return user.role === "manager";
  if (view === "budgets") return user.role === "admin" || user.role === "manager";
  return false;
}

export function resolveRoute(hash: string, user: NavigationIdentity) {
  const raw = hash.replace(/^#\/?/, "").split("?")[0];
  const view = raw.startsWith("c/") ? "knowledge" : raw || (user.role === "admin" ? "home" : "knowledge");
  return { view: canOpenView(view, user) ? view : "workspace", unavailable: !canOpenView(view, user) };
}

export type ModuleContract = { id: string; name: string; description: string; views: readonly string[]; pending?: string };
/** Add a route only after its owning module supplies a working screen and authorized API. */
export const enterpriseModules: readonly ModuleContract[] = [
  { id: "01", name: "Knowledge & AI Brain", description: "Ask questions, explore sources, and manage company knowledge.", views: ["knowledge", "research", "interviews"] },
  { id: "02", name: "IRFANIX Master AI Agent", description: "Run agents and inspect their tools, progress, and evidence.", views: ["agent-studio", "agents"] },
  { id: "03", name: "Company Intelligence Graph", description: "Explore organizational knowledge and expertise.", views: ["org", "expertise"], pending: "Interactive Company Map integration pending." },
  { id: "04", name: "Adaptive Process Intelligence", description: "Process maps, dependencies, and bottleneck analysis.", views: [], pending: "Process dashboard integration pending." },
  { id: "05", name: "Universal Workflow Engine", description: "Manage workflows, tasks, and approval queues.", views: ["automation", "my-work", "approvals"], pending: "Visual executable builder integration pending." },
  { id: "06", name: "Governance & Security", description: "Review access, approvals, and recorded activity.", views: ["people", "approvals", "logs", "security"] },
  { id: "07", name: "Exception & Recovery", description: "Incident diagnostics and controlled recovery.", views: [], pending: "Incident dashboard integration pending." },
  { id: "08", name: "Business Continuity", description: "Preserve knowledge and prepare employee handoffs.", views: ["handoffs", "training"], pending: "Continuity planning dashboard integration pending." },
  { id: "09", name: "Enterprise Frontend & UX", description: "Personalize your workspace and learn keyboard shortcuts.", views: ["experience"] },
  { id: "10", name: "Integrations & Communication", description: "Connect data sources and manage communication channels.", views: ["connectors", "webhooks", "meetings", "requests"] },
  { id: "11", name: "Infrastructure & Deployment", description: "Configure your company and AI services.", views: ["deployment", "providers", "settings"], pending: "Operational service-health dashboard integration pending." },
  { id: "12", name: "QA & Benchmarking", description: "Inspect recorded Knowledge quality and usage outcomes.", views: ["traces", "reports"], pending: "Release-readiness dashboard integration pending." },
];
