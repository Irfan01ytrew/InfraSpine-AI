import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import type { DepartmentGroup, ManagedUser, WorkspaceSummaryReport } from "../types";
import { useFeedback } from "./Feedback";
import { Archive, Building2, FolderKanban, Network, Plus, RefreshCw, RotateCcw, Save, UserPlus, Users } from "lucide-react";
import { cn } from "../lib/utils";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, Input, PageHeader, Select, Skeleton, Stat, Tabs } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

type WorkspaceKind = "client" | "project";
type WorkspaceMember = { user_id: string; email: string };

export function WorkspacesPage() {
  const { toast, confirm } = useFeedback();
  const [workspaces, setWorkspaces] = useState<DepartmentGroup[]>([]);
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [members, setMembers] = useState<WorkspaceMember[] | null>(null);
  const [kind, setKind] = useState<WorkspaceKind>("client");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [parentId, setParentId] = useState("");
  const [ownerId, setOwnerId] = useState("");
  const [editParentId, setEditParentId] = useState("");
  const [editOwnerId, setEditOwnerId] = useState("");
  const [summary, setSummary] = useState<WorkspaceSummaryReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await api.adminGroups();
      const rows = response.groups.filter((group) =>
        group.source === "local" && (group.kind === "client" || group.kind === "project"));
      const userResponse = await api.adminUsers();
      setWorkspaces(rows);
      setUsers(userResponse.users.filter((user) => user.is_active));
      setSelectedId((current) => rows.some((group) => group.id === current) ? current : rows[0]?.id ?? "");
      setError(null);
    } catch (err) { setError(describeError(err)); }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);

  const loadMembers = useCallback(async () => {
    if (!selectedId) { setMembers([]); return; }
    setMembers(null);
    try { setMembers((await api.adminGroupMembers(selectedId)).members); setError(null); }
    catch (err) { setError(describeError(err)); setMembers([]); }
  }, [selectedId]);

  useEffect(() => { void loadMembers(); }, [loadMembers]);

  const loadSummary = useCallback(async () => {
    if (!selectedId) { setSummary(null); return; }
    try { setSummary(await api.adminWorkspaceSummary(selectedId)); }
    catch (err) { setSummary(null); setError(describeError(err)); }
  }, [selectedId]);

  const selected = workspaces.find((group) => group.id === selectedId);
  const clients = workspaces.filter((group) => group.kind === "client" && group.source === "local");

  useEffect(() => {
    setEditParentId(selected?.parent_id ?? "");
    setEditOwnerId(selected?.owner_id ?? "");
    void loadSummary();
  }, [selectedId, selected?.parent_id, selected?.owner_id, loadSummary]);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    const cleanName = name.trim();
    if (!cleanName || busy) return;
    setBusy(true);
    try {
      const workspace = await api.createAdminGroup(cleanName, kind, {
        ...(kind === "project" ? { parent_id: parentId || null } : {}),
        owner_id: ownerId || null,
      });
      setName("");
      setParentId("");
      setOwnerId("");
      toast("success", `${kind === "client" ? "Client" : "Project"} workspace created`);
      await load();
      setSelectedId(workspace.id);
    } catch (err) { toast("error", "Could not create workspace", describeError(err)); }
    finally { setBusy(false); }
  }

  async function saveWorkspaceDetails(event: React.FormEvent) {
    event.preventDefault();
    if (!selected || busy) return;
    setBusy(true);
    try {
      await api.updateAdminGroup(selected.id, {
        ...(selected.kind === "project" ? { parent_id: editParentId || null } : {}),
        owner_id: editOwnerId || null,
      });
      toast("success", "Workspace details saved");
      await load();
      await loadSummary();
    } catch (err) { toast("error", "Could not save workspace details", describeError(err)); }
    finally { setBusy(false); }
  }

  async function toggleArchive() {
    if (!selected || busy) return;
    setBusy(true);
    const next = selected.lifecycle_status === "archived" ? "active" : "archived";
    try {
      await api.updateAdminGroup(selected.id, { lifecycle_status: next });
      toast("success", next === "archived" ? "Workspace archived" : "Workspace reactivated");
      await load();
      await loadSummary();
    } catch (err) { toast("error", "Could not update workspace lifecycle", describeError(err)); }
    finally { setBusy(false); }
  }

  async function addMember(event: React.FormEvent) {
    event.preventDefault();
    if (!selectedId || !email.trim() || busy) return;
    setBusy(true);
    try {
      await api.addAdminGroupMember(selectedId, email.trim());
      setEmail("");
      toast("success", "Workspace member added", "Access applies only to documents explicitly scoped to this workspace.");
      await loadMembers();
      await load();
    } catch (err) { toast("error", "Could not add workspace member", describeError(err)); }
    finally { setBusy(false); }
  }

  async function removeMember(member: WorkspaceMember) {
    const ok = await confirm({
      title: `Remove ${member.email} from this workspace?`,
      body: "Access to documents scoped to this workspace ends on the next request. The documents and their audit history remain.",
      confirmLabel: "Remove member",
      tone: "danger",
    });
    if (!ok || !selectedId) return;
    setBusy(true);
    try {
      await api.removeAdminGroupMember(selectedId, member.user_id);
      toast("success", "Workspace access removed");
      await loadMembers();
      await load();
    } catch (err) { toast("error", "Could not remove workspace member", describeError(err)); }
    finally { setBusy(false); }
  }

  const parentName = selected?.parent_id ? workspaces.find((group) => group.id === selected.parent_id)?.name : null;
  const ownerEmail = selected?.owner_id ? users.find((user) => user.id === selected.owner_id)?.email : null;
  const projects = workspaces.filter((group) => group.kind === "project");
  const standalone = projects.filter((project) => !project.parent_id || !clients.some((client) => client.id === project.parent_id));
  const userOptions = [{ value: "", label: "Unassigned" }, ...users.map((user) => ({ value: user.id, label: user.email }))];
  const clientOptions = [{ value: "", label: "No parent client" }, ...clients.map((client) => ({ value: client.id, label: client.name }))];
  const archived = selected?.lifecycle_status === "archived";

  function workspaceButton(group: DepartmentGroup, nested = false) {
    const active = group.id === selectedId;
    const Icon = group.kind === "client" ? Building2 : FolderKanban;
    return (
      <button
        key={group.id}
        type="button"
        onClick={() => setSelectedId(group.id)}
        aria-current={active ? "true" : undefined}
        className={cn("flex w-full min-w-0 cursor-pointer items-center gap-2.5 rounded-lg px-3 py-2 text-left outline-none transition-colors hover:bg-surface-2 focus-visible:ring-[3px] focus-visible:ring-ring", nested && "pl-8", active && "bg-primary-soft hover:bg-primary-soft")}
      >
        <Icon className={cn("size-4 shrink-0", active ? "text-primary" : "text-muted")} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] font-medium text-foreground">{group.name}</span>
          <span className="block text-xs text-muted">{group.member_count} member{group.member_count === 1 ? "" : "s"}{group.lifecycle_status === "archived" ? " · archived" : ""}</span>
        </span>
      </button>
    );
  }

  return (
    <>
      <PageHeader
        title="Client & project workspaces"
        description="Separate groups for each client or project. Members see a restricted document only when that document is shared with their workspace."
        actions={<Button onClick={() => void load()}><RefreshCw /> Refresh</Button>}
      />
      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat icon={<Building2 />} label="Clients" value={clients.length} />
        <Stat icon={<FolderKanban />} label="Projects" value={projects.length} />
        <Stat icon={<Users />} label="Memberships" value={workspaces.reduce((total, group) => total + group.member_count, 0)} />
        <Stat icon={<Archive />} label="Archived" value={workspaces.filter((group) => group.lifecycle_status === "archived").length} />
      </div>
      {error && <Alert tone="danger" className="mb-5">{error}</Alert>}

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
        <div className="grid min-w-0 gap-5">
          <Card className="overflow-hidden">
            <CardHeader icon={<Plus />} title="Create a workspace" description="Uses the same live access checks as departments." />
            <CardBody>
              <form className="grid gap-3" onSubmit={(event) => void create(event)}>
                <Tabs value={kind} onValueChange={(value) => { setKind(value as WorkspaceKind); if (value !== "project") setParentId(""); }} items={[{ value: "client", label: "Client" }, { value: "project", label: "Project" }]} />
                <Field label="Name"><Input required minLength={2} maxLength={120} value={name} onChange={(event) => setName(event.target.value)} placeholder={kind === "client" ? "Acme client" : "Bridge replacement project"} /></Field>
                {kind === "project" && <Field label="Client"><Select aria-label="Client" value={parentId} onValueChange={setParentId} options={clientOptions} /></Field>}
                <Field label="Workspace owner"><Select aria-label="Workspace owner" value={ownerId} onValueChange={setOwnerId} options={userOptions} /></Field>
                <Button type="submit" variant="primary" loading={busy} disabled={busy || name.trim().length < 2}>{!busy && <Plus />} Create {kind}</Button>
              </form>
            </CardBody>
          </Card>

          <Card className="overflow-hidden">
            <CardHeader icon={<Network />} title="Workspaces" actions={<Badge>{workspaces.length}</Badge>} />
            {workspaces.length === 0 ? <EmptyState className="py-8" icon={<Network />} title="No workspaces yet" description="Create a client, then add projects under it." /> : (
              <div className="grid max-h-[560px] gap-0.5 overflow-y-auto p-2" role="list" aria-label="Clients and projects">
                {clients.map((client) => (
                  <div role="listitem" key={client.id}>
                    {workspaceButton(client)}
                    {projects.filter((project) => project.parent_id === client.id).map((project) => workspaceButton(project, true))}
                  </div>
                ))}
                {standalone.length > 0 && <p className="px-3 pt-3 pb-1 text-xs font-semibold tracking-wide text-muted uppercase">Standalone projects</p>}
                {standalone.map((project) => <div role="listitem" key={project.id}>{workspaceButton(project)}</div>)}
              </div>
            )}
          </Card>
        </div>

        <div className="grid min-w-0 gap-5">
          {!selected ? (
            <Card><EmptyState icon={<Network />} title="No workspace selected" description="Pick a client or project from the list, or create one." /></Card>
          ) : (
            <>
              <Card className="overflow-hidden" aria-label="Workspace administration">
                <CardHeader
                  icon={selected.kind === "client" ? <Building2 /> : <FolderKanban />}
                  title={selected.name}
                  description={selected.kind === "client" ? "Client workspace" : parentName ? `Project under ${parentName}` : "Standalone project"}
                  actions={<>
                    <Badge tone={archived ? "warning" : "success"} dot>{selected.lifecycle_status ?? "active"}</Badge>
                    <Button size="sm" variant={archived ? "secondary" : "ghost"} disabled={busy} onClick={() => void toggleArchive()}>{archived ? <><RotateCcw /> Reactivate</> : <><Archive /> Archive</>}</Button>
                  </>}
                />
                <CardBody className="grid gap-5">
                  <div className="grid gap-3 sm:grid-cols-3" aria-label="Workspace details">
                    <Detail label="Parent client" value={selected.kind === "client" ? "None (client workspace)" : parentName ?? "None (standalone project)"} />
                    <Detail label="Owner" value={ownerEmail ?? "Unassigned"} />
                    <Detail label="Status" value={selected.lifecycle_status ?? "active"} />
                  </div>
                  <form className="grid gap-3 border-t border-border pt-4 sm:grid-cols-[repeat(2,minmax(0,1fr))_auto] sm:items-end" onSubmit={(event) => void saveWorkspaceDetails(event)}>
                    {selected.kind === "project" ? <Field label="Parent client"><Select aria-label="Parent client" value={editParentId} onValueChange={setEditParentId} options={clientOptions} /></Field> : <div className="hidden sm:block" />}
                    <Field label="Workspace owner"><Select aria-label="Workspace owner for selected workspace" value={editOwnerId} onValueChange={setEditOwnerId} options={userOptions} /></Field>
                    <Button type="submit" variant="primary" disabled={busy}><Save /> Save details</Button>
                  </form>
                </CardBody>
              </Card>

              <section aria-label="Customer workspace summary" className="sr-only">Workspace summary</section>
              {summary ? (
                <dl className="grid grid-cols-2 gap-3 lg:grid-cols-5">
                  <SummaryStat label="Child projects" value={summary.child_project_count} />
                  <SummaryStat label="Members" value={summary.member_count} />
                  <SummaryStat label="Documents" value={summary.total_documents} />
                  <SummaryStat label="Stored size" value={formatBytes(summary.total_bytes)} />
                  <SummaryStat label="Indexed chunks" value={summary.total_chunks} />
                </dl>
              ) : <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">{Array.from({ length: 5 }, (_, index) => <Skeleton key={index} className="h-[72px]" />)}</div>}
              <p className="-mt-3 text-xs text-muted">Document totals include this workspace and its direct child projects.</p>

              <Card className="overflow-hidden">
                <CardHeader icon={<Users />} title={`${selected.kind === "client" ? "Client" : "Project"} members`} description="Adding someone does not share every document; each restricted document must include this workspace." actions={<Badge>{members?.length ?? 0}</Badge>} />
                <CardBody className="grid gap-4">
                  <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => void addMember(event)}>
                    <Field label="Add an existing account by email" className="min-w-0 flex-1"><Input type="email" required value={email} onChange={(event) => setEmail(event.target.value)} placeholder="name@yourcompany.com" list="workspace-member-suggestions" /></Field>
                    <datalist id="workspace-member-suggestions">{users.filter((user) => !members?.some((member) => member.user_id === user.id)).map((user) => <option key={user.id} value={user.email} />)}</datalist>
                    <Button type="submit" variant="primary" disabled={busy || !email.trim()}><UserPlus /> Add member</Button>
                  </form>
                  {members === null ? <Skeleton className="h-24" /> : members.length === 0 ? <p className="text-xs text-muted">No members are assigned to this workspace.</p> : (
                    <ul className="grid">
                      {members.map((member) => (
                        <li className="flex items-center justify-between gap-2 border-t border-border py-2.5 first:border-t-0" key={member.user_id}>
                          <span className="flex min-w-0 items-center gap-2.5"><span className="grid size-7 shrink-0 place-items-center rounded-full bg-primary-soft text-xs font-semibold text-primary uppercase">{member.email.slice(0, 2)}</span><span className="truncate text-[13px]">{member.email}</span></span>
                          <Button size="sm" variant="danger-ghost" disabled={busy} onClick={() => void removeMember(member)}>Remove</Button>
                        </li>
                      ))}
                    </ul>
                  )}
                </CardBody>
              </Card>
            </>
          )}
        </div>
      </div>
    </>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return <div className="min-w-0 rounded-lg bg-surface-2/60 px-3 py-2"><p className="text-xs font-medium text-muted">{label}</p><p className="truncate text-[13px] font-medium text-foreground">{value}</p></div>;
}

function SummaryStat({ label, value }: { label: string; value: number | string }) {
  return <div className="rounded-xl border border-border bg-surface px-4 py-3 shadow-xs"><dt className="text-xs text-muted">{label}</dt><dd className="mt-0.5 text-lg font-semibold tracking-tight text-foreground">{value}</dd></div>;
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}
