import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowDown, ArrowUp, Archive, Link2, Unlink, BookOpen, Briefcase, ChevronDown, ChevronRight, ListOrdered, Trash2, GitBranch, History, Pencil, Plus, RotateCcw, Search, UserRound, Users } from "lucide-react";
import { api, describeError } from "../api";
import { formatDate, formatMoney, initials, regionalSettings } from "../format";
import { cn } from "../lib/utils";
import type { ApprovalChainStep, ApprovalLimit, JobFamily, LibraryPosition, OrgChartPerson, OrgHistoryEntry, Position } from "../types";
import { useFeedback } from "./Feedback";
import { Alert, Badge, Button, Card, CardBody, CardHeader, Dialog, EmptyState, Field, Input, Select, Skeleton, Stat, Table, Textarea } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

export const JOB_FAMILY_LABELS: Record<JobFamily, string> = {
  executive: "Executive",
  management: "Management",
  professional: "Professional",
  technical: "Technical",
  operations: "Operations",
  administration: "Administration",
  governance: "Governance & compliance",
  sales: "Sales & customers",
  frontline: "Frontline & field",
  external: "External",
  other: "Other",
};
const FAMILY_OPTIONS = (Object.keys(JOB_FAMILY_LABELS) as JobFamily[]).map((value) => ({ value, label: JOB_FAMILY_LABELS[value] }));
const LEVEL_HINT = "Optional seniority band: 1 is the most senior.";

/** Positions are job titles. They describe people; they never decide what someone can access. */
export function PositionsSection() {
  const { toast } = useFeedback();
  const [positions, setPositions] = useState<Position[] | null>(null);
  const [library, setLibrary] = useState<LibraryPosition[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [family, setFamily] = useState<string>("");
  const [showArchived, setShowArchived] = useState(false);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState({ title: "", job_family: "other" as JobFamily, level: "", description: "" });
  const [editing, setEditing] = useState<Position | null>(null);

  const load = useCallback(async () => {
    try {
      const [list, lib] = await Promise.all([api.positions(), api.positionLibrary()]);
      setPositions(list.positions);
      setLibrary(lib.positions);
      setError(null);
    } catch (err) { setError(describeError(err)); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const created = await api.createPosition({ title: draft.title.trim(), job_family: draft.job_family, level: draft.level ? Number(draft.level) : null, description: draft.description.trim() });
      toast("success", `${created.title} added`, "Assign it to people in the Org chart tab.");
      setDraft({ title: "", job_family: draft.job_family, level: "", description: "" });
      await load();
    } catch (err) { toast("error", "Could not add the position", describeError(err)); }
    finally { setBusy(false); }
  }

  async function setActive(position: Position, active: boolean) {
    try {
      await api.updatePosition(position.id, { is_active: active });
      toast("success", active ? `${position.title} restored` : `${position.title} archived`, active ? undefined : "People who hold it keep it; it can no longer be given to anyone new. History is kept.");
      await load();
    } catch (err) { toast("error", "Could not change the position", describeError(err)); }
  }

  async function addFromLibrary(titles: string[] | null) {
    setBusy(true);
    try {
      const result = await api.applyPositionLibrary(titles);
      toast("success", `${result.created.length} position${result.created.length === 1 ? "" : "s"} added`, result.skipped.length ? `${result.skipped.length} already existed.` : undefined);
      await load();
    } catch (err) { toast("error", "Could not add positions", describeError(err)); }
    finally { setBusy(false); }
  }

  const visible = (positions ?? []).filter((p) => (showArchived || p.is_active)
    && (!family || p.job_family === family)
    && (!query.trim() || `${p.title} ${p.description}`.toLowerCase().includes(query.trim().toLowerCase())));
  const missing = library.filter((p) => !p.added);
  const active = (positions ?? []).filter((p) => p.is_active);

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-5">
      <Alert tone="info">A position is what someone is called or does (CEO, Accountant, Security Guard…). It never grants access: what a person can see and do comes from their software role, custom roles and departments.</Alert>
      {error && <Alert tone="danger">{error}</Alert>}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat icon={<Briefcase />} label="Active positions" value={positions ? active.length : "…"} />
        <Stat icon={<Users />} label="People with a position" value={positions ? active.reduce((sum, p) => sum + p.holders, 0) : "…"} />
        <Stat icon={<GitBranch />} label="Job families used" value={positions ? new Set(active.map((p) => p.job_family)).size : "…"} />
        <Stat icon={<Archive />} label="Archived" value={positions ? positions.length - active.length : "…"} />
      </div>

      <Card className="overflow-hidden">
        <CardHeader icon={<Plus />} title="Add a position" description="Any title your organization uses. Add as many as you need; rename or archive later without losing history." />
        <CardBody>
          <form className="grid gap-3 md:grid-cols-[minmax(0,2fr)_minmax(0,1.3fr)_110px_auto] md:items-end" onSubmit={(event) => void create(event)}>
            <Field label="Title"><Input required minLength={2} maxLength={120} value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} placeholder="e.g. Shift Supervisor, Chief Risk Officer" /></Field>
            <Field label="Job family"><Select aria-label="Job family" value={draft.job_family} onValueChange={(value) => setDraft({ ...draft, job_family: value as JobFamily })} options={FAMILY_OPTIONS} /></Field>
            <Field label="Level"><Input type="number" min={1} max={20} value={draft.level} onChange={(e) => setDraft({ ...draft, level: e.target.value })} placeholder="1–20" title={LEVEL_HINT} /></Field>
            <Button type="submit" variant="primary" loading={busy} disabled={busy || draft.title.trim().length < 2}>{!busy && <Plus />} Add</Button>
            <Field label="Description (optional)" className="md:col-span-4"><Input maxLength={1000} value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} placeholder="What this person does" /></Field>
          </form>
        </CardBody>
      </Card>

      <Card className="overflow-hidden">
        <CardHeader
          icon={<Briefcase />}
          title="Positions"
          description="Sorted by level, most senior first."
          actions={<div className="flex flex-wrap items-center gap-2">
            <Input className="w-48" icon={<Search />} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search positions" aria-label="Search positions" />
            <Select size="sm" className="w-44" aria-label="Filter by job family" value={family} onValueChange={setFamily} options={[{ value: "", label: "All job families" }, ...FAMILY_OPTIONS]} />
            <Button size="sm" variant={showArchived ? "soft" : "ghost"} onClick={() => setShowArchived(!showArchived)}><Archive /> {showArchived ? "Hide archived" : "Show archived"}</Button>
          </div>}
        />
        {positions === null ? <CardBody><Skeleton className="h-40" /></CardBody> : visible.length === 0 ? (
          <EmptyState icon={<Briefcase />} title={positions.length ? "No positions match" : "No positions yet"} description={positions.length ? "Change the search or filters." : "Add your own above, or start from the library below."} />
        ) : (
          <Table>
            <thead><tr><th>Position</th><th>Job family</th><th className="text-right">Level</th><th className="text-right">Holders</th><th>Status</th><th className="text-right">Actions</th></tr></thead>
            <tbody>{visible.map((p) => (
              <tr key={p.id} className={cn(!p.is_active && "opacity-60")}>
                <td><p className="font-medium text-foreground">{p.title}</p>{p.description && <p className="text-xs text-muted">{p.description}</p>}</td>
                <td><Badge>{JOB_FAMILY_LABELS[p.job_family]}</Badge></td>
                <td className="text-right tabular-nums">{p.level ?? "—"}</td>
                <td className="text-right tabular-nums">{p.holders}</td>
                <td><Badge tone={p.is_active ? "success" : "neutral"} dot>{p.is_active ? "Active" : "Archived"}</Badge></td>
                <td className="text-right">
                  <div className="inline-flex gap-1">
                    <Button size="sm" variant="ghost" onClick={() => setEditing(p)}><Pencil /> Edit</Button>
                    {p.is_active
                      ? <Button size="sm" variant="ghost" onClick={() => void setActive(p, false)}><Archive /> Archive</Button>
                      : <Button size="sm" variant="ghost" onClick={() => void setActive(p, true)}><RotateCcw /> Restore</Button>}
                  </div>
                </td>
              </tr>
            ))}</tbody>
          </Table>
        )}
      </Card>

      <Card className="overflow-hidden">
        <CardHeader
          icon={<BookOpen />}
          title="Position library"
          description={`${library.length} common positions across job families. Adding them only creates titles; it never grants access.`}
          actions={missing.length > 0 ? <Button size="sm" variant="primary" loading={busy} onClick={() => void addFromLibrary(null)}>Add all {missing.length} missing</Button> : <Badge tone="success">All added</Badge>}
        />
        <CardBody className="grid gap-4">
          {FAMILY_OPTIONS.filter((f) => library.some((p) => p.job_family === f.value)).map((f) => (
            <div key={f.value}>
              <p className="mb-2 text-[11px] font-semibold tracking-wide text-muted uppercase">{f.label}</p>
              <div className="flex flex-wrap gap-1.5">
                {library.filter((p) => p.job_family === f.value).map((p) => (
                  <button
                    key={p.title}
                    type="button"
                    disabled={p.added || busy}
                    title={p.description}
                    onClick={() => void addFromLibrary([p.title])}
                    className={cn("inline-flex h-7 items-center gap-1 rounded-full border px-2.5 text-xs font-medium transition-colors", p.added ? "cursor-default border-transparent bg-success-soft text-success-text" : "cursor-pointer border-border bg-surface text-foreground-2 hover:border-primary hover:text-primary")}
                  >
                    {!p.added && <Plus className="size-3" />}{p.title}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </CardBody>
      </Card>

      <EditPositionDialog position={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); void load(); }} />
    </div>
  );
}

function EditPositionDialog({ position, onClose, onSaved }: { position: Position | null; onClose: () => void; onSaved: () => void }) {
  const { toast } = useFeedback();
  const [form, setForm] = useState({ title: "", job_family: "other" as JobFamily, level: "", description: "" });
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (position) setForm({ title: position.title, job_family: position.job_family, level: position.level ? String(position.level) : "", description: position.description });
  }, [position]);

  async function save() {
    if (!position) return;
    setBusy(true);
    try {
      await api.updatePosition(position.id, {
        title: form.title.trim(), job_family: form.job_family, description: form.description.trim(),
        ...(form.level ? { level: Number(form.level) } : { clear_level: true }),
      });
      toast("success", "Position saved", "Its holders and their history are unchanged; history keeps the old title.");
      onSaved();
    } catch (err) { toast("error", "Could not save the position", describeError(err)); }
    finally { setBusy(false); }
  }

  return (
    <Dialog
      open={position !== null}
      onOpenChange={(open) => { if (!open) onClose(); }}
      title={position ? `Edit ${position.title}` : "Edit position"}
      description="Renaming updates everyone who holds this position. Past assignments keep the title they had."
      footer={<><Button onClick={onClose}>Cancel</Button><Button variant="primary" loading={busy} disabled={busy || form.title.trim().length < 2} onClick={() => void save()}>Save</Button></>}
    >
      <div className="grid gap-3">
        <Field label="Title"><Input maxLength={120} value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></Field>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Job family"><Select aria-label="Job family" value={form.job_family} onValueChange={(value) => setForm({ ...form, job_family: value as JobFamily })} options={FAMILY_OPTIONS} /></Field>
          <Field label="Level" hint={LEVEL_HINT}><Input type="number" min={1} max={20} value={form.level} onChange={(e) => setForm({ ...form, level: e.target.value })} /></Field>
        </div>
        <Field label="Description"><Textarea rows={3} maxLength={1000} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></Field>
      </div>
    </Dialog>
  );
}

/** Reporting lines as a tree. Admins change a person's position and manager here; loops are refused by the server. */
export function OrgChartSection() {
  const [people, setPeople] = useState<OrgChartPerson[] | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<OrgChartPerson | null>(null);

  const load = useCallback(async () => {
    try {
      const [chart, list] = await Promise.all([api.orgChart(), api.positions()]);
      setPeople(chart.people);
      setPositions(list.positions);
      setError(null);
    } catch (err) { setError(describeError(err)); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);

  const children = useMemo(() => {
    const map = new Map<string | null, OrgChartPerson[]>();
    for (const person of people ?? []) {
      const key = person.reports_to_id;
      map.set(key, [...(map.get(key) ?? []), person]);
    }
    return map;
  }, [people]);
  const roots = children.get(null) ?? [];
  const q = query.trim().toLowerCase();
  const matches = (p: OrgChartPerson) => !q || `${p.email} ${p.position_title ?? ""} ${p.departments.join(" ")}`.toLowerCase().includes(q);
  const subtreeMatches = (p: OrgChartPerson): boolean => matches(p) || (children.get(p.user_id) ?? []).some(subtreeMatches);
  const withPosition = (people ?? []).filter((p) => p.position_id).length;
  const withManager = (people ?? []).filter((p) => p.reports_to_id).length;

  function toggle(id: string) {
    setCollapsed((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  }

  function renderNode(person: OrgChartPerson, depth: number): React.ReactNode {
    if (!subtreeMatches(person)) return null;
    const reports = children.get(person.user_id) ?? [];
    const open = !collapsed.has(person.user_id) || Boolean(q);
    return (
      <li key={person.user_id} className="relative">
        <div className={cn("group flex min-w-0 items-center gap-2.5 rounded-lg py-1.5 pr-2 transition-colors hover:bg-surface-2", q && matches(person) && "bg-primary-soft/60")} style={{ paddingLeft: depth * 22 + 4 }}>
          {reports.length > 0 ? (
            <button type="button" aria-label={open ? `Collapse ${person.email}` : `Expand ${person.email}`} onClick={() => toggle(person.user_id)} className="grid size-5 shrink-0 cursor-pointer place-items-center rounded text-muted hover:text-foreground">
              {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
            </button>
          ) : <span className="size-5 shrink-0" />}
          <span className="grid size-8 shrink-0 place-items-center rounded-full bg-primary-soft text-[11px] font-semibold text-primary">{initials(person.email)}</span>
          <span className="min-w-0 flex-1">
            <span className="flex flex-wrap items-center gap-1.5">
              <span className="truncate text-[13px] font-medium text-foreground">{person.email}</span>
              {person.position_title ? <Badge tone="primary">{person.position_title}</Badge> : <Badge>No position</Badge>}
              {reports.length > 0 && <span className="text-[11px] text-muted">{reports.length} direct report{reports.length === 1 ? "" : "s"}</span>}
            </span>
            <span className="block truncate text-[11px] text-muted">{person.departments.length ? person.departments.join(" · ") : "No department"} · software role: {person.role === "user" ? "employee" : person.role}</span>
          </span>
          <Button size="sm" variant="ghost" className="opacity-70 group-hover:opacity-100" onClick={() => setEditing(person)}><Pencil /> Edit</Button>
        </div>
        {reports.length > 0 && open && <ul className="relative">
          <span aria-hidden className="absolute top-0 bottom-2 w-px bg-border" style={{ left: depth * 22 + 14 }} />
          {reports.map((child) => renderNode(child, depth + 1))}
        </ul>}
      </li>
    );
  }

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-5">
      {error && <Alert tone="danger">{error}</Alert>}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat icon={<Users />} label="People" value={people ? people.length : "…"} />
        <Stat icon={<Briefcase />} label="With a position" value={people ? `${withPosition} / ${people.length}` : "…"} />
        <Stat icon={<GitBranch />} label="With a manager" value={people ? `${withManager} / ${people.length}` : "…"} />
        <Stat icon={<UserRound />} label="Top of the chart" value={people ? roots.length : "…"} hint="People who report to no one" />
      </div>
      <Card className="overflow-hidden">
        <CardHeader
          icon={<GitBranch />}
          title="Organization chart"
          description="Who reports to whom. Separate from access: changing a manager or position never changes permissions."
          actions={<Input className="w-56" icon={<Search />} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Find a person or position" aria-label="Find a person or position" />}
        />
        {people === null ? <CardBody><Skeleton className="h-48" /></CardBody> : people.length === 0 ? <EmptyState icon={<Users />} title="No people yet" description="Invite people first." /> : (
          <CardBody className="overflow-x-auto"><ul className="grid gap-0.5" aria-label="Organization chart">{roots.map((root) => renderNode(root, 0))}</ul></CardBody>
        )}
      </Card>
      <AssignDialog person={editing} people={people ?? []} positions={positions} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); void load(); }} />
    </div>
  );
}

function AssignDialog({ person, people, positions, onClose, onSaved }: {
  person: OrgChartPerson | null; people: OrgChartPerson[]; positions: Position[]; onClose: () => void; onSaved: () => void;
}) {
  const { toast } = useFeedback();
  const [positionId, setPositionId] = useState("");
  const [managerId, setManagerId] = useState("");
  const [history, setHistory] = useState<OrgHistoryEntry[] | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!person) return;
    setPositionId(person.position_id ?? "");
    setManagerId(person.reports_to_id ?? "");
    setHistory(null);
    api.orgHistory(person.user_id).then((r) => setHistory(r.entries)).catch(() => setHistory([]));
  }, [person]);

  // A person cannot report to themselves or to anyone below them; the server enforces the same rule.
  const below = useMemo(() => {
    const out = new Set<string>();
    if (!person) return out;
    const stack = [person.user_id];
    while (stack.length) {
      const id = stack.pop()!;
      for (const p of people) if (p.reports_to_id === id && !out.has(p.user_id)) { out.add(p.user_id); stack.push(p.user_id); }
    }
    return out;
  }, [person, people]);

  async function save() {
    if (!person) return;
    setBusy(true);
    try {
      await api.assignOrg(person.user_id, { position_id: positionId || null, reports_to_id: managerId || null });
      toast("success", "Organization updated", `${person.email}'s position and manager were saved. Access did not change.`);
      onSaved();
    } catch (err) { toast("error", "Could not save", describeError(err)); }
    finally { setBusy(false); }
  }

  const positionOptions = [{ value: "", label: "No position" }, ...positions.filter((p) => p.is_active || p.id === person?.position_id).map((p) => ({ value: p.id, label: p.is_active ? p.title : `${p.title} (archived)` }))];
  const managerOptions = [{ value: "", label: "Reports to no one" }, ...people.filter((p) => p.user_id !== person?.user_id && !below.has(p.user_id)).map((p) => ({ value: p.user_id, label: p.position_title ? `${p.email} · ${p.position_title}` : p.email }))];

  return (
    <Dialog
      open={person !== null}
      onOpenChange={(open) => { if (!open) onClose(); }}
      title={person ? `Position and manager · ${person.email}` : "Position and manager"}
      description="This changes the organization chart only. Software role, custom roles and departments are managed in Employees."
      footer={<><Button onClick={onClose}>Cancel</Button><Button variant="primary" loading={busy} disabled={busy} onClick={() => void save()}>Save</Button></>}
    >
      <div className="grid gap-4">
        <Field label="Position"><Select aria-label="Position" value={positionId} onValueChange={setPositionId} options={positionOptions} /></Field>
        <Field label="Reports to" hint="People who report to this person are not offered, so the chart cannot loop."><Select aria-label="Reports to" value={managerId} onValueChange={setManagerId} options={managerOptions} /></Field>
        <div>
          <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-foreground-2"><History className="size-3.5" /> History</p>
          {history === null ? <Skeleton className="h-12" /> : history.length === 0 ? <p className="text-xs text-muted">No changes recorded yet.</p> : (
            <ol className="grid gap-1.5 border-l border-border pl-3">
              {history.map((h, index) => (
                <li key={index} className="text-xs text-foreground-2">
                  <span className="font-medium text-foreground">{h.position_title ?? "No position"}</span>{h.reports_to_email ? ` · reports to ${h.reports_to_email}` : ""}
                  <span className="block text-muted">{formatDate(h.started_at)}{h.ended_at ? ` – ${formatDate(h.ended_at)}` : " – now"}</span>
                </li>
              ))}
            </ol>
          )}
        </div>
      </div>
    </Dialog>
  );
}

/** How much each position (or one named person) may approve. Separate from software roles and job titles. */
export function ApprovalLimitsSection() {
  const { toast } = useFeedback();
  const [limits, setLimits] = useState<ApprovalLimit[] | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [people, setPeople] = useState<OrgChartPerson[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState({ category: "purchase" as "purchase" | "expense", subject: "position" as "position" | "person", position: "", person: "", amount: "", currency: regionalSettings().currency ?? "" });
  const [editing, setEditing] = useState<{ limit: ApprovalLimit; amount: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const [l, p, c] = await Promise.all([api.approvalLimits(), api.positions(), api.orgChart()]);
      setLimits(l.limits);
      setPositions(p.positions.filter((x) => x.is_active));
      setPeople(c.people);
      setError(null);
    } catch (err) { setError(describeError(err)); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await api.createApprovalLimit({
        category: draft.category, max_amount: draft.amount, currency: draft.currency,
        ...(draft.subject === "position" ? { position_id: draft.position } : { user_id: draft.person }),
      });
      toast("success", "Approval limit saved", "It applies to new approvals immediately.");
      setDraft({ ...draft, amount: "", position: "", person: "" });
      await load();
    } catch (err) { toast("error", "Could not save the limit", describeError(err)); }
    finally { setBusy(false); }
  }

  async function update(limit: ApprovalLimit, input: { max_amount?: string; is_active?: boolean }) {
    try {
      const result = await api.updateApprovalLimit(limit.id, input);
      setLimits(result.limits);
      setEditing(null);
      toast("success", input.is_active === false ? "Approval limit removed" : "Approval limit changed");
    } catch (err) { toast("error", "Could not change the limit", describeError(err)); }
  }

  const ready = draft.amount !== "" && Number(draft.amount) >= 0 && draft.currency.length === 3 && (draft.subject === "position" ? Boolean(draft.position) : Boolean(draft.person));
  const holders = (positionId: string | null) => people.filter((p) => p.position_id === positionId).length;

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-5">
      <Alert tone="info">Approval authority is separate from access and job titles. Nobody, administrators included, can approve a purchase request or expense claim until a limit covers it, and nobody can approve their own request. A personal limit overrides the limit of the person&apos;s position.</Alert>
      {error && <Alert tone="danger">{error}</Alert>}
      <Card className="overflow-hidden">
        <CardHeader icon={<Plus />} title="Set an approval limit" description="Give a position (everyone who holds it) or one person a maximum amount they may approve." />
        <CardBody>
          <form className="grid gap-3 md:grid-cols-2 xl:grid-cols-[150px_150px_minmax(0,1fr)_140px_90px_auto] xl:items-end" onSubmit={(event) => void create(event)}>
            <Field label="Request type"><Select aria-label="Request type" value={draft.category} onValueChange={(value) => setDraft({ ...draft, category: value as "purchase" | "expense" })} options={[{ value: "purchase", label: "Purchase requests" }, { value: "expense", label: "Expense claims" }]} /></Field>
            <Field label="Applies to"><Select aria-label="Applies to" value={draft.subject} onValueChange={(value) => setDraft({ ...draft, subject: value as "position" | "person" })} options={[{ value: "position", label: "A position" }, { value: "person", label: "One person" }]} /></Field>
            {draft.subject === "position"
              ? <Field label="Position"><Select aria-label="Position for the limit" placeholder="Choose a position" value={draft.position} onValueChange={(value) => setDraft({ ...draft, position: value })} options={positions.map((p) => ({ value: p.id, label: p.title }))} /></Field>
              : <Field label="Person"><Select aria-label="Person for the limit" placeholder="Choose a person" value={draft.person} onValueChange={(value) => setDraft({ ...draft, person: value })} options={people.map((p) => ({ value: p.user_id, label: p.position_title ? `${p.email} · ${p.position_title}` : p.email }))} /></Field>}
            <Field label="Up to"><Input inputMode="decimal" value={draft.amount} onChange={(e) => setDraft({ ...draft, amount: e.target.value.replace(/[^0-9.]/g, "") })} placeholder="50000" /></Field>
            <Field label="Currency"><Input maxLength={3} value={draft.currency} onChange={(e) => setDraft({ ...draft, currency: e.target.value.toUpperCase().replace(/[^A-Z]/g, "") })} placeholder="PKR" /></Field>
            <Button type="submit" variant="primary" loading={busy} disabled={busy || !ready}>{!busy && <Plus />} Save limit</Button>
          </form>
        </CardBody>
      </Card>
      <Card className="overflow-hidden">
        <CardHeader icon={<Briefcase />} title="Approval limits" description="Highest first. Removing a limit keeps it in the audit history." />
        {limits === null ? <CardBody><Skeleton className="h-32" /></CardBody> : limits.length === 0 ? (
          <EmptyState icon={<Briefcase />} title="No approval limits yet" description="Until you add one, purchase requests and expense claims cannot be approved by anyone." />
        ) : (
          <Table>
            <thead><tr><th>Type</th><th>Applies to</th><th className="text-right">Up to</th><th className="text-right">Actions</th></tr></thead>
            <tbody>{limits.map((limit) => (
              <tr key={limit.id}>
                <td><Badge>{limit.category === "purchase" ? "Purchase requests" : "Expense claims"}</Badge></td>
                <td>{limit.position_title
                  ? <><p className="font-medium text-foreground">{limit.position_title}</p><p className="text-xs text-muted">Position · {holders(limit.position_id)} holder{holders(limit.position_id) === 1 ? "" : "s"}</p></>
                  : <><p className="font-medium text-foreground">{limit.user_email}</p><p className="text-xs text-muted">Personal limit (overrides their position)</p></>}</td>
                <td className="text-right font-medium tabular-nums">{formatMoney(Number(limit.max_amount), limit.currency)}</td>
                <td className="text-right">
                  <div className="inline-flex gap-1">
                    <Button size="sm" variant="ghost" onClick={() => setEditing({ limit, amount: String(Number(limit.max_amount)) })}><Pencil /> Change</Button>
                    <Button size="sm" variant="danger-ghost" onClick={() => void update(limit, { is_active: false })}><Archive /> Remove</Button>
                  </div>
                </td>
              </tr>
            ))}</tbody>
          </Table>
        )}
      </Card>
      <ApprovalChainEditor positions={positions} people={people} />
      <Dialog
        open={editing !== null}
        onOpenChange={(open) => { if (!open) setEditing(null); }}
        title="Change approval limit"
        description={editing ? `${editing.limit.position_title ?? editing.limit.user_email} · ${editing.limit.category === "purchase" ? "purchase requests" : "expense claims"}` : undefined}
        footer={<><Button onClick={() => setEditing(null)}>Cancel</Button><Button variant="primary" disabled={!editing || editing.amount === ""} onClick={() => editing && void update(editing.limit, { max_amount: editing.amount })}>Save</Button></>}
      >
        {editing && <Field label={`Up to (${editing.limit.currency})`}><Input inputMode="decimal" value={editing.amount} onChange={(e) => setEditing({ ...editing, amount: e.target.value.replace(/[^0-9.]/g, "") })} /></Field>}
      </Dialog>
    </div>
  );
}

/** Ordered approval steps per kind of financial request; applies to requests submitted afterwards. */
function ApprovalChainEditor({ positions, people }: { positions: Position[]; people: OrgChartPerson[] }) {
  const { toast } = useFeedback();
  const [category, setCategory] = useState<"purchase" | "expense">("purchase");
  const [steps, setSteps] = useState<ApprovalChainStep[] | null>(null);
  const [draft, setDraft] = useState({ label: "", kind: "manager" as "manager" | "position" | "person", position: "", person: "", minAmount: "", parallel: false, mode: "all" as "all" | "any" });
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setSteps((await api.approvalChain()).steps); } catch (err) { toast("error", "Could not load the approval chain", describeError(err)); }
  }, [toast]);
  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);

  async function run(action: () => Promise<{ steps: ApprovalChainStep[] }>, done?: string) {
    setBusy(true);
    try { setSteps((await action()).steps); if (done) toast("success", done); }
    catch (err) { toast("error", "Could not change the chain", describeError(err)); }
    finally { setBusy(false); }
  }

  async function add(event: React.FormEvent) {
    event.preventDefault();
    await run(() => api.addApprovalChainStep({
      category, label: draft.label.trim(), approver_kind: draft.kind,
      position_id: draft.kind === "position" ? draft.position : null, user_id: draft.kind === "person" ? draft.person : null,
      min_amount: draft.minAmount || null, parallel_with_previous: draft.parallel && visible.length > 0, stage_mode: draft.mode,
    }), "Step added");
    setDraft({ ...draft, label: "", minAmount: "", parallel: false });
  }

  const visible = (steps ?? []).filter((step) => step.category === category);
  // Stage number and size for each step: a step that runs with the previous one shares its stage.
  const stageOf: number[] = [];
  visible.forEach((step, index) => { stageOf.push(index > 0 && step.parallel_with_previous ? stageOf[index - 1] : (stageOf[index - 1] ?? 0) + 1); });
  const stageSize = (stage: number) => stageOf.filter((s) => s === stage).length;
  const lastStage = stageOf[stageOf.length - 1];
  const approverText = (step: ApprovalChainStep) => step.approver_kind === "manager" ? "The requester's manager" : step.approver_kind === "position" ? `Any ${step.position_title}` : step.user_email;
  const ready = draft.label.trim().length >= 2 && (draft.kind === "manager" || (draft.kind === "position" ? Boolean(draft.position) : Boolean(draft.person)));

  return (
    <Card className="overflow-hidden">
      <CardHeader
        icon={<ListOrdered />}
        title="Approval chain"
        description="Stages run in order; steps linked into one stage are decided at the same time, by all of their approvers or any one. Any rejection rejects the request. One person never decides two steps, and final approvers also need a limit covering the amount. Changes apply to requests submitted afterwards."
        actions={<Select size="sm" className="w-44" aria-label="Chain for" value={category} onValueChange={(value) => setCategory(value as "purchase" | "expense")} options={[{ value: "purchase", label: "Purchase requests" }, { value: "expense", label: "Expense claims" }]} />}
      />
      <CardBody className="grid gap-4">
        {steps === null ? <Skeleton className="h-20" /> : visible.length === 0 ? (
          <p className="text-xs text-muted">No chain: a single approver whose limit covers the amount approves these requests.</p>
        ) : (
          <ol className="grid gap-2">
            {visible.map((step, index) => {
              const stage = stageOf[index], startsStage = index === 0 || !step.parallel_with_previous, shared = stageSize(stage) > 1;
              return (
              <li key={step.id} className={cn("grid gap-1.5", startsStage && index > 0 && "mt-1")}>
                {startsStage && shared && (
                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
                    <span className="font-semibold text-foreground-2">Stage {stage} · decided together</span>
                    <Select size="sm" className="w-48" aria-label={`Stage ${stage} needs`} value={step.stage_mode} disabled={busy}
                      onValueChange={(value) => void run(() => api.updateApprovalChainStep(step.id, { stage_mode: value as "all" | "any" }), "Stage updated")}
                      options={[{ value: "all", label: "All must approve" }, { value: "any", label: "Any one approves" }]} />
                  </div>
                )}
              <div className={cn("flex flex-wrap items-center gap-3 rounded-lg border border-border px-3 py-2", shared && "border-l-4 border-l-primary/60", !startsStage && "ml-4")}>
                <span className="grid size-7 shrink-0 place-items-center rounded-full bg-primary-soft text-xs font-semibold text-primary" title={`Stage ${stage}`}>{stage}</span>
                <span className="min-w-0 flex-1">
                  <span className="block text-[13px] font-medium text-foreground">{step.label}{stage === lastStage && <span className="text-xs font-normal text-muted"> · final</span>}</span>
                  <span className="block text-xs text-muted">{approverText(step)}{step.min_amount ? ` · only from ${formatMoney(Number(step.min_amount))}` : " · always"}</span>
                </span>
                <span className="inline-flex gap-1">
                  {index > 0 && <Button size="icon-sm" variant="ghost" disabled={busy}
                    aria-label={step.parallel_with_previous ? `Run ${step.label} after the previous step` : `Run ${step.label} together with the previous step`}
                    title={step.parallel_with_previous ? "Run after the previous step" : "Run together with the previous step"}
                    onClick={() => void run(() => api.updateApprovalChainStep(step.id, { parallel_with_previous: !step.parallel_with_previous }), "Stage updated")}>
                    {step.parallel_with_previous ? <Unlink /> : <Link2 />}
                  </Button>}
                  <Button size="icon-sm" variant="ghost" aria-label={`Move ${step.label} up`} disabled={busy || index === 0} onClick={() => void run(() => api.moveApprovalChainStep(step.id, -1))}><ArrowUp /></Button>
                  <Button size="icon-sm" variant="ghost" aria-label={`Move ${step.label} down`} disabled={busy || index === visible.length - 1} onClick={() => void run(() => api.moveApprovalChainStep(step.id, 1))}><ArrowDown /></Button>
                  <Button size="icon-sm" variant="danger-ghost" aria-label={`Remove ${step.label}`} disabled={busy} onClick={() => void run(() => api.removeApprovalChainStep(step.id), "Step removed")}><Trash2 /></Button>
                </span>
              </div>
              </li>
              );
            })}
          </ol>
        )}
        <form className="grid gap-3 rounded-lg border border-dashed border-border-strong p-3 md:grid-cols-2 xl:grid-cols-[minmax(0,1fr)_170px_minmax(0,1fr)_130px_auto] xl:items-end" onSubmit={(event) => void add(event)}>
          <Field label="Step name"><Input maxLength={80} value={draft.label} onChange={(e) => setDraft({ ...draft, label: e.target.value })} placeholder="e.g. Finance check" /></Field>
          <Field label="Approved by"><Select aria-label="Approved by" value={draft.kind} onValueChange={(value) => setDraft({ ...draft, kind: value as "manager" | "position" | "person" })} options={[{ value: "manager", label: "Requester's manager" }, { value: "position", label: "A position" }, { value: "person", label: "One person" }]} /></Field>
          {draft.kind === "position" ? <Field label="Position"><Select aria-label="Step position" placeholder="Choose a position" value={draft.position} onValueChange={(value) => setDraft({ ...draft, position: value })} options={positions.map((p) => ({ value: p.id, label: p.title }))} /></Field>
            : draft.kind === "person" ? <Field label="Person"><Select aria-label="Step person" placeholder="Choose a person" value={draft.person} onValueChange={(value) => setDraft({ ...draft, person: value })} options={people.map((p) => ({ value: p.user_id, label: p.position_title ? `${p.email} · ${p.position_title}` : p.email }))} /></Field>
            : <div className="hidden xl:block" />}
          <Field label="Only from amount"><Input inputMode="decimal" value={draft.minAmount} onChange={(e) => setDraft({ ...draft, minAmount: e.target.value.replace(/[^0-9.]/g, "") })} placeholder="Always" /></Field>
          {visible.length > 0 && (
            <div className="flex flex-wrap items-center gap-3 md:col-span-2 xl:col-span-5">
              <label className="inline-flex items-center gap-2 text-[13px] text-foreground-2">
                <input type="checkbox" className="size-4 accent-[var(--brand)]" checked={draft.parallel} onChange={(e) => setDraft({ ...draft, parallel: e.target.checked })} />
                Decide together with the last step (same stage)
              </label>
              {draft.parallel && <Select size="sm" className="w-48" aria-label="New stage needs" value={draft.mode} onValueChange={(value) => setDraft({ ...draft, mode: value as "all" | "any" })}
                options={[{ value: "all", label: "All must approve" }, { value: "any", label: "Any one approves" }]} />}
            </div>
          )}
          <Button type="submit" variant="primary" loading={busy} disabled={busy || !ready}>{!busy && <Plus />} Add step</Button>
        </form>
      </CardBody>
    </Card>
  );
}
