import { useCallback, useEffect, useState } from "react";
import { CATEGORY_PERMISSIONS, permissionLabel } from "./permissionLabels";
import { api, describeError } from "../api";
import { formatDate, formatDay } from "../format";
import type { DepartmentGroup, ManagedUser, PermissionCatalogEntry, RoleTemplate, RoleTemplateAssignment, RoleTemplateChangeRequest, RoleTemplatePermission } from "../types";
import { useFeedback } from "./Feedback";
import { Ban, Check, ClipboardCheck, MoreHorizontal, Pencil, Plus, RefreshCw, ShieldCheck, Sparkles, UserPlus, Users } from "lucide-react";
import { cn } from "../lib/utils";
import { Badge, Button, Card, CardBody, CardHeader, Dialog, DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger, EmptyState, Field, Input, Select, Table } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

const categories = ["policy", "financial", "legal", "client", "operational", "general"] as const;

function defaultExpiryInput() {
  const value = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000);
  value.setSeconds(0, 0);
  return new Date(value.getTime() - value.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

export function RoleTemplatesSection({ users, departments, permissions }: {
  users: ManagedUser[];
  departments: DepartmentGroup[];
  permissions: PermissionCatalogEntry["permission"][];
}) {
  const { toast, confirm } = useFeedback();
  const [templates, setTemplates] = useState<RoleTemplate[]>([]);
  const [assignments, setAssignments] = useState<RoleTemplateAssignment[]>([]);
  const [changes, setChanges] = useState<RoleTemplateChangeRequest[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [baseRole, setBaseRole] = useState<"employee" | "manager">("employee");
  // Start with no privilege selected so a newly created template is never broader
  // than the administrator explicitly intended.
  const [selectedPermissions, setSelectedPermissions] = useState<RoleTemplatePermission["permission"][]>([]);
  const [selectedCategories, setSelectedCategories] = useState<string[]>([]);
  const [templateId, setTemplateId] = useState("");
  const [email, setEmail] = useState("");
  const [departmentIds, setDepartmentIds] = useState<string[]>([]);
  const [expiresAt, setExpiresAt] = useState(defaultExpiryInput);
  const [editingChangeTemplate, setEditingChangeTemplate] = useState<RoleTemplate | null>(null);
  const [changeDescription, setChangeDescription] = useState("");
  const [changePermissions, setChangePermissions] = useState<RoleTemplatePermission["permission"][]>(["knowledge.review"]);
  const [changeCategories, setChangeCategories] = useState<string[]>(["general"]);
  const [decisionRationale, setDecisionRationale] = useState("");
  const [busy, setBusy] = useState(false);
  // Ready-made roles come from the server, the same library industry starter kits use.
  const [roleLibrary, setRoleLibrary] = useState<{ key: string; name: string; base_role: "employee" | "manager"; description: string; permissions: RoleTemplatePermission[] }[]>([]);
  useEffect(() => { api.roleLibrary().then((rows) => setRoleLibrary(rows.roles)).catch(() => setRoleLibrary([])); }, []);
  const needsCategories = selectedPermissions.some((permission) => CATEGORY_PERMISSIONS.has(permission));
  const changeNeedsCategories = changePermissions.some((permission) => CATEGORY_PERMISSIONS.has(permission));

  const load = useCallback(async () => {
    try {
      const [templateRows, assignmentRows, changeRows] = await Promise.all([api.roleTemplates(), api.roleTemplateAssignments(), api.roleTemplateChanges()]);
      setTemplates(templateRows.templates);
      setAssignments(assignmentRows.assignments);
      setChanges(changeRows.requests);
      setTemplateId((current) => templateRows.templates.some((item) => item.id === current && item.is_active)
        ? current : templateRows.templates.find((item) => item.is_active)?.id ?? "");
    } catch (err) { toast("error", "Could not load role templates", describeError(err)); }
  }, [toast]);

  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);
  const activeTemplate = templates.find((item) => item.id === templateId && item.is_active);
  const matchingUsers = users.filter((user) => user.is_active && user.role === activeTemplate?.base_role);
  const selectedUser = matchingUsers.find((user) => user.email === email);
  const allowedDepartmentIds = new Set(selectedUser?.departments.map((department) => department.id) ?? []);
  const scopedDepartments = departments.filter((department) => allowedDepartmentIds.has(department.id));

  useEffect(() => {
    if (!matchingUsers.some((user) => user.email === email)) setEmail(matchingUsers[0]?.email ?? "");
  }, [matchingUsers, email]);

  function togglePermission(value: RoleTemplatePermission["permission"]) {
    setSelectedPermissions((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]);
  }
  function toggleCategory(value: string) {
    setSelectedCategories((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]);
  }
  function toggleDepartment(value: string) {
    setDepartmentIds((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]);
  }

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (!name.trim() || selectedPermissions.length === 0 || (needsCategories && selectedCategories.length === 0)) return;
    setBusy(true);
    try {
      const specs: RoleTemplatePermission[] = selectedPermissions.map((permission) => ({
        permission,
        categories: (CATEGORY_PERMISSIONS.has(permission) ? selectedCategories : []) as RoleTemplatePermission["categories"],
      }));
      const created = await api.createRoleTemplate({ name, description, base_role: baseRole, permissions: specs });
      setName("");
      setDescription("");
      setTemplateId(created.id);
      toast("success", "Custom role created", "Assign it to people below. To change it later, propose a new version for another administrator to approve.");
      await load();
    } catch (err) { toast("error", "Could not create role template", describeError(err)); }
    finally { setBusy(false); }
  }

  async function assign(event: React.FormEvent) {
    event.preventDefault();
    const expiry = new Date(expiresAt);
    if (!activeTemplate || !email || departmentIds.length === 0 || Number.isNaN(expiry.getTime())) return;
    setBusy(true);
    try {
      const row = await api.assignRoleTemplate(activeTemplate.id, email, departmentIds, expiry.toISOString());
      setDepartmentIds([]);
      setExpiresAt(defaultExpiryInput());
      toast("success", "Custom role assigned", `${row.grant_count} rights granted until ${formatDate(row.expires_at ?? expiry.toISOString())}. Department rights apply only in the selected departments; organization rights (such as reports) apply everywhere.`);
      await load();
    } catch (err) { toast("error", "Could not assign role template", describeError(err)); }
    finally { setBusy(false); }
  }

  async function deactivate(template: RoleTemplate) {
    const ok = await confirm({
      title: `Deactivate ${template.name}?`,
      body: "New assignments will stop. Existing grants remain until their assignment is explicitly revoked.",
      confirmLabel: "Deactivate",
      tone: "danger",
    });
    if (!ok) return;
    try { await api.setRoleTemplateActive(template.id, false); toast("success", "Template deactivated"); await load(); }
    catch (err) { toast("error", "Could not deactivate template", describeError(err)); }
  }

  function startChange(template: RoleTemplate) {
    setEditingChangeTemplate(template);
    setChangeDescription(template.description);
    setChangePermissions(template.permissions.map((spec) => spec.permission));
    setChangeCategories([...new Set(template.permissions.flatMap((spec) => spec.categories))]);
  }

  async function proposeChange(event?: React.FormEvent) {
    event?.preventDefault();
    if (!editingChangeTemplate || !changePermissions.length || (changeNeedsCategories && !changeCategories.length)) return;
    setBusy(true);
    try {
      await api.proposeRoleTemplateChange(editingChangeTemplate.id, {
        description: changeDescription,
        permissions: changePermissions.map((permission) => ({ permission, categories: (CATEGORY_PERMISSIONS.has(permission) ? changeCategories : []) as RoleTemplatePermission["categories"] })),
      });
      toast("success", "Change submitted for independent approval", "The current version remains active until a different administrator approves the proposal.");
      setEditingChangeTemplate(null);
      await load();
    } catch (err) { toast("error", "Could not propose template change", describeError(err)); }
    finally { setBusy(false); }
  }

  async function decideChange(request: RoleTemplateChangeRequest, decision: "approve" | "reject") {
    if (decisionRationale.trim().length < 3) return;
    setBusy(true);
    try {
      const result = await api.decideRoleTemplateChange(request.id, decision, decisionRationale.trim());
      setDecisionRationale("");
      toast("success", decision === "approve" ? "New template version approved" : "Template change rejected",
        decision === "approve" ? `Version ${result.current_version + 1} is active; grants from the previous version were revoked and must be reassigned.` : "The current version remains active.");
      await load();
    } catch (err) { toast("error", "Could not decide template change", describeError(err)); }
    finally { setBusy(false); }
  }

  async function revoke(assignment: RoleTemplateAssignment) {
    const ok = await confirm({
      title: `Revoke ${assignment.template_name} from ${assignment.email}?`,
      body: "All grants issued by this assignment will be revoked immediately. The assignment and audit history remain visible.",
      confirmLabel: "Revoke assignment",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await api.revokeRoleTemplateAssignment(assignment.id);
      toast("success", "Role assignment revoked", "Its scoped governance permissions no longer apply.");
      await load();
    } catch (err) { toast("error", "Could not revoke role assignment", describeError(err)); }
  }

  const existingNames = new Set(templates.map((item) => item.name.toLowerCase()));
  const library = roleLibrary.filter((preset) => !existingNames.has(preset.name.toLowerCase()));

  async function addFromLibrary(preset: (typeof roleLibrary)[number]) {
    setBusy(true);
    try {
      await api.createRoleTemplate({ name: preset.name, description: preset.description, base_role: preset.base_role, permissions: preset.permissions });
      toast("success", `${preset.name} added`, "Assign it to people below, or invite new people with this role.");
      await load();
    } catch (err) { toast("error", `Could not add ${preset.name}`, describeError(err)); }
    finally { setBusy(false); }
  }

  const rightsText = (specs: RoleTemplatePermission[]) => specs.map((spec) => spec.categories.length ? `${permissionLabel(spec.permission)} (${spec.categories.join(", ")})` : permissionLabel(spec.permission));

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-5" aria-labelledby="role-template-heading">
      <Card>
        <CardHeader
          icon={<Sparkles />}
          title={<span id="role-template-heading">Custom roles</span>}
          description="A custom role adds a set of rights on top of an Employee or Manager account, only in the departments you choose. Create as many as your organization needs; one person can hold several."
          actions={<Button size="sm" onClick={() => void load()}><RefreshCw /> Refresh</Button>}
        />
        {library.length > 0 && (
          <CardBody className="border-b border-border">
            <p className="mb-3 text-xs font-semibold tracking-wide text-muted uppercase">Ready-made roles · add with one click</p>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {library.map((preset) => (
                <div key={preset.name} className="flex flex-col gap-2 rounded-lg border border-border p-3">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="text-[13px] font-semibold text-foreground">{preset.name}</p>
                      <p className="text-xs text-muted">{preset.description}</p>
                    </div>
                    <Badge>{preset.base_role === "manager" ? "Managers" : "Employees"}</Badge>
                  </div>
                  <div className="flex flex-wrap gap-1">{rightsText(preset.permissions).map((text) => <Badge key={text} tone="primary">{text}</Badge>)}</div>
                  <Button size="sm" className="mt-auto self-start" disabled={busy} onClick={() => void addFromLibrary(preset)}><Plus /> Add role</Button>
                </div>
              ))}
            </div>
          </CardBody>
        )}
        <CardBody>
          <p className="mb-3 text-xs font-semibold tracking-wide text-muted uppercase">Or build your own</p>
          <form className="grid gap-4" onSubmit={(event) => void create(event)}>
            <div className="grid gap-4 md:grid-cols-3">
              <Field label="Role name"><Input required minLength={2} maxLength={80} value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Procurement Officer" /></Field>
              <Field label="Given to"><Select aria-label="Given to" value={baseRole} onValueChange={(value) => setBaseRole(value as "employee" | "manager")} options={[{ value: "employee", label: "Employees" }, { value: "manager", label: "Managers" }]} /></Field>
              <Field label="Description"><Input maxLength={500} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="What this role is for" /></Field>
            </div>
            <Field label="What can this role do?">
              <Chips options={permissions.map((item) => ({ value: item, label: permissionLabel(item) }))} selected={selectedPermissions} onToggle={(value) => togglePermission(value as RoleTemplatePermission["permission"])} />
            </Field>
            {needsCategories && <Field label="Knowledge categories (for the knowledge rights)"><Chips options={categories.map((item) => ({ value: item, label: item[0].toUpperCase() + item.slice(1) }))} selected={selectedCategories} onToggle={toggleCategory} /></Field>}
            <div><Button type="submit" variant="primary" disabled={busy || !name.trim() || !selectedPermissions.length || (needsCategories && !selectedCategories.length)}><Plus /> Create role</Button></div>
          </form>
        </CardBody>
      </Card>

      <Card>
        <CardHeader icon={<ShieldCheck />} title="Your roles" description="Changing a role creates a new version that another administrator must approve." />
        {templates.length === 0 ? <EmptyState icon={<ShieldCheck />} title="No custom roles yet" description="Add a ready-made role above or build your own." /> : (
          <Table>
            <thead><tr><th>Role</th><th>Given to</th><th>Rights</th><th>Status</th><th className="text-right">Actions</th></tr></thead>
            <tbody>{templates.map((template) => (
              <tr key={template.id}>
                <td><p className="font-medium text-foreground">{template.name}</p><p className="text-xs text-muted">v{template.version}{template.description ? ` · ${template.description}` : ""}</p></td>
                <td>{template.base_role === "manager" ? "Managers" : "Employees"}</td>
                <td><div className="flex flex-wrap gap-1">{rightsText(template.permissions).map((text) => <Badge key={text} tone="primary">{text}</Badge>)}</div></td>
                <td><Badge tone={template.is_active ? "success" : "warning"} dot>{template.is_active ? "Active" : "Inactive"}</Badge></td>
                <td className="text-right">{template.is_active && (
                  <div className="inline-flex gap-1">
                    <Button size="sm" disabled={busy} onClick={() => { setTemplateId(template.id); document.getElementById("assign-role")?.scrollIntoView({ behavior: "smooth" }); }}><UserPlus /> Assign</Button>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild><Button size="icon-sm" variant="ghost" aria-label={`More actions for ${template.name}`}><MoreHorizontal /></Button></DropdownMenuTrigger>
                      <DropdownMenuContent>
                        <DropdownMenuItem onSelect={() => startChange(template)}><Pencil /> Propose a change</DropdownMenuItem>
                        <DropdownMenuItem danger onSelect={() => void deactivate(template)}><Ban /> Deactivate</DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                )}</td>
              </tr>
            ))}</tbody>
          </Table>
        )}
      </Card>

      <Dialog
        open={editingChangeTemplate !== null}
        onOpenChange={(open) => !open && setEditingChangeTemplate(null)}
        title={editingChangeTemplate ? `Propose ${editingChangeTemplate.name} v${editingChangeTemplate.version + 1}` : "Propose a change"}
        description="The current version stays in force until another administrator approves. Approval removes rights given by the old version; reassign people afterwards."
        footer={<><Button onClick={() => setEditingChangeTemplate(null)}>Cancel</Button><Button variant="primary" disabled={busy || !changePermissions.length || (changeNeedsCategories && !changeCategories.length)} onClick={() => void proposeChange()}>Submit for approval</Button></>}
      >
        <div className="grid gap-4">
          <Field label="Description"><Input maxLength={500} value={changeDescription} onChange={(event) => setChangeDescription(event.target.value)} /></Field>
          <Field label="What can this role do?"><Chips options={permissions.map((item) => ({ value: item, label: permissionLabel(item) }))} selected={changePermissions} onToggle={(value) => setChangePermissions((current) => current.includes(value as RoleTemplatePermission["permission"]) ? current.filter((entry) => entry !== value) : [...current, value as RoleTemplatePermission["permission"]])} /></Field>
          {changeNeedsCategories && <Field label="Knowledge categories"><Chips options={categories.map((item) => ({ value: item, label: item[0].toUpperCase() + item.slice(1) }))} selected={changeCategories} onToggle={(value) => setChangeCategories((current) => current.includes(value) ? current.filter((entry) => entry !== value) : [...current, value])} /></Field>}
        </div>
      </Dialog>

      {changes.length > 0 && (
        <Card>
          <CardHeader icon={<ClipboardCheck />} title="Role changes waiting for approval" description="A different administrator approves or rejects each proposed change." />
          <Table>
            <thead><tr><th>Role</th><th>Requested by</th><th>Proposed rights</th><th>Status</th><th className="text-right">Decision</th></tr></thead>
            <tbody>{changes.map((request) => (
              <tr key={request.id}>
                <td className="font-medium text-foreground">{request.template_name} v{request.current_version} → next</td>
                <td>{request.requested_by_email}</td>
                <td><div className="flex flex-wrap gap-1">{rightsText(request.proposed_permissions).map((text) => <Badge key={text} tone="primary">{text}</Badge>)}</div></td>
                <td><Badge tone={request.status === "pending" ? "warning" : request.status === "approved" ? "success" : "neutral"} dot>{request.status}</Badge>{request.decided_by_email && <p className="mt-1 text-xs text-muted">By {request.decided_by_email}: {request.decision_rationale}</p>}</td>
                <td className="text-right">{request.status === "pending" && (
                  <div className="ml-auto grid max-w-xs gap-2">
                    <Input minLength={3} maxLength={2000} value={decisionRationale} onChange={(event) => setDecisionRationale(event.target.value)} placeholder="Reason for your decision" aria-label="Decision reason" className="h-8 text-xs" />
                    <div className="flex justify-end gap-1"><Button size="sm" variant="primary" disabled={busy || decisionRationale.trim().length < 3} onClick={() => void decideChange(request, "approve")}>Approve</Button><Button size="sm" variant="danger-ghost" disabled={busy || decisionRationale.trim().length < 3} onClick={() => void decideChange(request, "reject")}>Reject</Button></div>
                  </div>
                )}</td>
              </tr>
            ))}</tbody>
          </Table>
        </Card>
      )}

      <Card id="assign-role">
        <CardHeader icon={<UserPlus />} title="Assign a role" description="The person must already have the role's base role (Employee or Manager) and belong to the departments you pick. Rights end automatically on the expiry date (at most one year)." />
        <CardBody>
          <form className="grid gap-4" onSubmit={(event) => void assign(event)}>
            <div className="grid gap-4 md:grid-cols-3">
              <Field label="Role"><Select aria-label="Role to assign" value={templateId} onValueChange={(value) => { setTemplateId(value); setEmail(""); setDepartmentIds([]); }} placeholder="Choose a role" options={templates.filter((item) => item.is_active).map((item) => ({ value: item.id, label: `${item.name} (${item.base_role === "manager" ? "Managers" : "Employees"})` }))} /></Field>
              <Field label="Person" hint={activeTemplate && !matchingUsers.length ? `No active ${activeTemplate.base_role} accounts yet.` : undefined}><Select aria-label="Person" value={email} onValueChange={(value) => { setEmail(value); setDepartmentIds([]); }} placeholder="Choose a person" options={matchingUsers.map((user) => ({ value: user.email, label: user.email }))} /></Field>
              <Field label="Rights end on"><Input type="datetime-local" required value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} /></Field>
            </div>
            <Field label="In departments (required)">
              {selectedUser && scopedDepartments.length === 0
                ? <p className="text-xs text-muted">This person is not in any department yet. Add them to one in the Employees tab first.</p>
                : <Chips options={scopedDepartments.map((group) => ({ value: group.id, label: group.name }))} selected={departmentIds} onToggle={toggleDepartment} />}
            </Field>
            <div><Button type="submit" variant="primary" disabled={busy || !activeTemplate || !email || !departmentIds.length || !expiresAt}><UserPlus /> Assign role</Button></div>
          </form>
        </CardBody>
      </Card>

      <Card>
        <CardHeader icon={<Users />} title="Who has which role" />
        {assignments.length === 0 ? <EmptyState icon={<Users />} title="No roles assigned yet" /> : (
          <Table>
            <thead><tr><th>Person</th><th>Role</th><th>Departments</th><th>Status</th><th>Rights</th><th className="text-right"></th></tr></thead>
            <tbody>{assignments.map((assignment) => {
              const expired = Boolean(assignment.expires_at && Date.parse(assignment.expires_at) <= Date.now());
              return (
                <tr key={assignment.id}>
                  <td className="font-medium text-foreground">{assignment.email}</td>
                  <td><Badge tone="primary">{assignment.template_name} v{assignment.template_version}</Badge></td>
                  <td><div className="flex flex-wrap gap-1">{assignment.department_ids.map((id) => <Badge key={id}>{departments.find((group) => group.id === id)?.name ?? "Department"}</Badge>)}</div></td>
                  <td><Badge tone={expired ? "warning" : "success"} dot>{expired ? "Expired" : "Active"}</Badge><p className="mt-1 text-xs text-muted">{assignment.expires_at ? `Until ${formatDay(assignment.expires_at)}` : "No expiry"}</p></td>
                  <td>{assignment.grant_count}</td>
                  <td className="text-right"><Button size="sm" variant="danger-ghost" disabled={busy} onClick={() => void revoke(assignment)}>Revoke</Button></td>
                </tr>
              );
            })}</tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}

/** Multi-select as toggle chips. */
export function Chips({ options, selected, onToggle }: { options: { value: string; label: string }[]; selected: string[]; onToggle: (value: string) => void }) {
  if (!options.length) return <p className="text-xs text-muted">Nothing to choose yet.</p>;
  return (
    <div className="flex flex-wrap gap-2" role="group">
      {options.map((option) => {
        const on = selected.includes(option.value);
        return (
          <button key={option.value} type="button" aria-pressed={on} onClick={() => onToggle(option.value)}
            className={cn("inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-full border px-3 text-xs font-medium outline-none transition-colors focus-visible:ring-[3px] focus-visible:ring-ring",
              on ? "border-primary bg-primary-soft text-primary" : "border-border bg-surface text-foreground-2 hover:border-border-strong")}>
            {on && <Check className="size-3.5" />}{option.label}
          </button>
        );
      })}
    </div>
  );
}
