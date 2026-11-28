import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import type { CsvInvitationPreview, CsvInvitationPreviewRow, DepartmentGroup, ManagedUser, PermissionCatalogEntry, PermissionGrant, Position, RoleTemplate, UserInvitation } from "../types";
import { useFeedback } from "./Feedback";
import { RoleTemplatesSection } from "./RoleTemplatesSection";
import { CreateRoleDialog } from "./CreateRoleDialog";
import { ApprovalLimitsSection, OrgChartSection, PositionsSection } from "./OrgStructureSection";
import { permissionLabel } from "./permissionLabels";
import { ArrowRightLeft, Building2, Check, Clock, Copy, Download, FileSpreadsheet, KeyRound, MailPlus, MoreHorizontal, Pencil, Plus, RefreshCw, RotateCw, Search, Send, ShieldCheck, Upload, UserCheck, UserPlus, Users, UserX } from "lucide-react";
import { cn } from "../lib/utils";
import { Alert, Badge, Button, Card, CardBody, CardHeader, Dialog, DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger, EmptyState, Field, Input, PageHeader, Select, Stat, Table, Tabs, Textarea } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

type PeopleTab = "departments" | "employees" | "positions" | "org" | "limits" | "bulk" | "permissions" | "templates";

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MAX_MULTI_INVITES = 200;


const CATEGORIES = ["policy", "financial", "legal", "client", "operational", "general"] as const;

const CREATE_ROLE = "__create_role__";

export function PeoplePage() {
  const { toast, confirm } = useFeedback();
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [groups, setGroups] = useState<DepartmentGroup[]>([]);
  const [invitations, setInvitations] = useState<UserInvitation[]>([]);
  const [grants, setGrants] = useState<PermissionGrant[]>([]);
  const [permissionCatalog, setPermissionCatalog] = useState<PermissionCatalogEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<string>("employee");  // "employee", "manager" or "custom:<role id>"
  const [customRoles, setCustomRoles] = useState<RoleTemplate[]>([]);
  // Which invite form asked for a new role; the created role is selected there.
  const [createRoleFor, setCreateRoleFor] = useState<"one" | "many" | null>(null);
  const [departmentIds, setDepartmentIds] = useState<string[]>([]);
  const [inviteLink, setInviteLink] = useState<string | null>(null);
  const [departmentName, setDepartmentName] = useState("");
  const [grantEmail, setGrantEmail] = useState("");
  const [permission, setPermission] = useState<PermissionGrant["permission"]>("knowledge.review");
  const [category, setCategory] = useState<string>("");
  const [scopeGroupId, setScopeGroupId] = useState<string>("");
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [csvPreview, setCsvPreview] = useState<CsvInvitationPreview | null>(null);
  const [csvBusy, setCsvBusy] = useState(false);
  const [csvError, setCsvError] = useState<string | null>(null);
  const [csvInviteLinks, setCsvInviteLinks] = useState<Record<number, string>>({});
  const [bulkProgress, setBulkProgress] = useState<{ done: number; total: number; failed: number } | null>(null);
  const [tab, setTab] = useState<PeopleTab>("employees");
  const [inviteMode, setInviteMode] = useState<"one" | "many">("one");
  const [accountQuery, setAccountQuery] = useState("");
  const [meId, setMeId] = useState<string | null>(null);
  const [multiEmails, setMultiEmails] = useState("");
  const [multiRole, setMultiRole] = useState<string>("employee");
  // Organization placement on invitations: a position (job title) and a manager. Neither grants access.
  const [positions, setPositions] = useState<Position[]>([]);
  const [placement, setPlacement] = useState({ one: { position: "", manager: "" }, many: { position: "", manager: "" } });
  const [multiDepartmentIds, setMultiDepartmentIds] = useState<string[]>([]);
  const [multiResults, setMultiResults] = useState<{ email: string; link?: string; error?: string }[]>([]);
  const [multiBusy, setMultiBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [userRows, groupRows, inviteRows, grantRows, catalogRows] = await Promise.all([
        api.adminUsers(), api.adminGroups(), api.invitations(), api.permissionGrants(), api.permissionCatalog(),
      ]);
      setUsers(userRows.users);
      setGroups(groupRows.groups);
      setInvitations(inviteRows.invitations);
      setGrants(grantRows.grants);
      setPermissionCatalog(catalogRows.permissions);
      setLoadError(null);
    } catch (err) {
      setLoadError(describeError(err));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);
  useEffect(() => {
    api.roleTemplates().then((rows) => setCustomRoles(rows.templates.filter((row) => row.is_active))).catch(() => setCustomRoles([]));
    api.positions().then((rows) => setPositions(rows.positions.filter((row) => row.is_active))).catch(() => setPositions([]));
  }, [tab]);

  /** Resolve a role choice into the base role and optional custom role sent with an invitation. */
  function resolveRole(choice: string): { role: "manager" | "employee"; roleTemplateId: string | null; label: string; needsDepartment: boolean } {
    const custom = choice.startsWith("custom:") ? customRoles.find((row) => row.id === choice.slice(7)) : undefined;
    if (custom) return { role: custom.base_role, roleTemplateId: custom.id, label: custom.name, needsDepartment: true };
    const base = choice === "manager" ? "manager" : "employee";
    return { role: base, roleTemplateId: null, label: roleLabel(base), needsDepartment: base === "manager" };
  }
  const roleSelectOptions = [
    { value: "employee", label: "Employee" },
    { value: "manager", label: "Manager" },
    ...customRoles.map((row) => ({ value: `custom:${row.id}`, label: `${row.name} · custom role (${roleLabel(row.base_role)})` })),
    { value: CREATE_ROLE, label: "+ Create a new role…" },
  ];
  function chooseRole(target: "one" | "many", value: string) {
    if (value === CREATE_ROLE) { setCreateRoleFor(target); return; }
    if (target === "one") setRole(value); else setMultiRole(value);
  }
  const singleRole = resolveRole(role);
  const multiResolved = resolveRole(multiRole);
  useEffect(() => { api.me().then((me) => setMeId(me.id)).catch(() => setMeId(null)); }, []);
  const departments = groups.filter((group) => group.kind === "department" && group.source === "local");

  function toggleDepartment(id: string) {
    setDepartmentIds((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  }

  async function createDepartment(event: React.FormEvent) {
    event.preventDefault();
    if (!departmentName.trim()) return;
    setBusy(true);
    try {
      await api.createAdminGroup(departmentName.trim(), "department");
      setDepartmentName("");
      toast("success", "Department created");
      await load();
    } catch (err) { toast("error", "Could not create department", describeError(err)); }
    finally { setBusy(false); }
  }

  async function invite(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const result = await api.createInvitation({ email, role: singleRole.role, department_ids: departmentIds, role_template_id: singleRole.roleTemplateId, position_id: placement.one.position || null, reports_to_id: placement.one.manager || null });
      const base = `${window.location.origin}${window.location.pathname}${window.location.search}`;
      setInviteLink(`${base}#/onboarding?token=${encodeURIComponent(result.token ?? "")}`);
      setEmail("");
      setDepartmentIds([]);
      toast("success", "Invitation created", "Copy the single-use link and send it using your approved channel.");
      await load();
    } catch (err) { toast("error", "Could not create invitation", describeError(err)); }
    finally { setBusy(false); }
  }

  async function resendInvitation(row: UserInvitation) {
    setBusy(true);
    try {
      const result = await api.resendInvitation(row.id);
      const base = `${window.location.origin}${window.location.pathname}${window.location.search}`;
      setInviteLink(`${base}#/onboarding?token=${encodeURIComponent(result.token ?? "")}`);
      toast("success", "Invitation link renewed", "The previous link is now invalid. Copy this one-time link and send it using your approved channel.");
      await load();
    } catch (err) { toast("error", "Could not resend invitation", describeError(err)); }
    finally { setBusy(false); }
  }

  async function previewCsv() {
    if (!csvFile) return;
    if (csvFile.size > 1_000_000) {
      setCsvError("CSV preview is limited to 1 MB.");
      setCsvPreview(null);
      return;
    }
    setCsvBusy(true);
    setCsvError(null);
    try {
      const result = await api.previewInvitationCsv(await csvFile.text());
      setCsvPreview(result);
      setCsvInviteLinks({});
    } catch (err) {
      setCsvPreview(null);
      setCsvError(describeError(err));
    } finally { setCsvBusy(false); }
  }

  async function createCsvInvitation(row: CsvInvitationPreviewRow) {
    if (!row.valid || !row.email || !row.role) return;
    setCsvBusy(true);
    try {
      const result = await api.createInvitation({ email: row.email, role: row.role, department_ids: row.department_ids, position_id: row.position_id ?? null, reports_to_id: row.reports_to_id ?? null });
      const base = `${window.location.origin}${window.location.pathname}${window.location.search}`;
      setCsvInviteLinks((current) => ({ ...current, [row.row_number]: `${base}#/onboarding?token=${encodeURIComponent(result.token ?? "")}` }));
      toast("success", `Invitation created for ${row.email}`, "Copy the one-time link and send it using your approved channel.");
      await load();
    } catch (err) { toast("error", `Could not invite ${row.email}`, describeError(err)); }
    finally { setCsvBusy(false); }
  }

  function invitationLink(token: string | null | undefined) {
    const base = `${window.location.origin}${window.location.pathname}${window.location.search}`;
    return `${base}#/onboarding?token=${encodeURIComponent(token ?? "")}`;
  }

  /** Invite every valid preview row (a few at a time) so a whole department list needs one click. */
  async function inviteAllCsvRows() {
    const rows = (csvPreview?.rows ?? []).filter((row) => row.valid && row.email && row.role && !csvInviteLinks[row.row_number]);
    if (!rows.length) return;
    const ok = await confirm({
      title: `Create ${rows.length} invitation${rows.length === 1 ? "" : "s"}?`,
      body: "Each person gets a single-use link that expires in 72 hours. Links are not emailed: download them afterwards and send them through your approved channel.",
      confirmLabel: "Create invitations",
    });
    if (!ok) return;
    setCsvBusy(true);
    setBulkProgress({ done: 0, total: rows.length, failed: 0 });
    const queue = [...rows];
    let failed = 0;
    const worker = async () => {
      for (let row = queue.shift(); row; row = queue.shift()) {
        const rowNumber = row.row_number;
        try {
          const result = await api.createInvitation({ email: row.email!, role: row.role!, department_ids: row.department_ids, position_id: row.position_id ?? null, reports_to_id: row.reports_to_id ?? null });
          const link = invitationLink(result.token);
          setCsvInviteLinks((current) => ({ ...current, [rowNumber]: link }));
        } catch {
          failed += 1;
        }
        setBulkProgress((current) => current && { ...current, done: current.done + 1, failed });
      }
    };
    await Promise.all(Array.from({ length: Math.min(4, rows.length) }, worker));
    setCsvBusy(false);
    const created = rows.length - failed;
    toast(
      failed ? "error" : "success",
      `${created} invitation${created === 1 ? "" : "s"} created`,
      failed
        ? `${failed} could not be created (for example, the email already has an account). Fix those rows and preview again.`
        : "Download the links file and send each person their link.",
    );
    await load();
  }

  const parsedEmails = (() => {
    const tokens = multiEmails.split(/[\s,;]+/).map((item) => item.trim().toLowerCase()).filter(Boolean);
    const unique = [...new Set(tokens)];
    const existing = new Set([...users.map((user) => user.email.toLowerCase()), ...invitations.filter((row) => !row.accepted_at && !row.revoked_at).map((row) => row.email.toLowerCase())]);
    return {
      valid: unique.filter((item) => EMAIL.test(item) && !existing.has(item)),
      invalid: unique.filter((item) => !EMAIL.test(item)),
      existing: unique.filter((item) => EMAIL.test(item) && existing.has(item)),
      duplicates: tokens.length - unique.length,
    };
  })();

  /** Invite a pasted list of addresses with one role and department set, a few at a time. */
  async function inviteMany(event: React.FormEvent) {
    event.preventDefault();
    const emails = parsedEmails.valid.slice(0, MAX_MULTI_INVITES);
    if (!emails.length || (multiResolved.needsDepartment && !multiDepartmentIds.length)) return;
    const ok = await confirm({
      title: `Invite ${emails.length} ${emails.length === 1 ? "person" : "people"} as ${multiResolved.label}?`,
      body: "Each person gets a single-use sign-up link that expires in 72 hours. Links are not emailed: download them afterwards and send them through your approved channel.",
      confirmLabel: "Create invitations",
    });
    if (!ok) return;
    setMultiBusy(true);
    setMultiResults([]);
    const queue = [...emails];
    const results: { email: string; link?: string; error?: string }[] = [];
    const worker = async () => {
      for (let address = queue.shift(); address; address = queue.shift()) {
        try {
          const result = await api.createInvitation({ email: address, role: multiResolved.role, department_ids: multiDepartmentIds, role_template_id: multiResolved.roleTemplateId, position_id: placement.many.position || null, reports_to_id: placement.many.manager || null });
          results.push({ email: address, link: invitationLink(result.token) });
        } catch (err) {
          results.push({ email: address, error: describeError(err) });
        }
        setMultiResults([...results]);
      }
    };
    await Promise.all(Array.from({ length: Math.min(4, emails.length) }, worker));
    setMultiBusy(false);
    const created = results.filter((row) => row.link).length;
    toast(created === emails.length ? "success" : "error", `${created} of ${emails.length} invitations created`, created ? "Download the links file and send each person their link." : undefined);
    if (created) setMultiEmails("");
    await load();
  }

  function downloadCsv(filename: string, rows: string[][]) {
    const escape = (value: string) => (/[",\n]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value);
    const blob = new Blob([rows.map((row) => row.map(escape).join(",")).join("\r\n") + "\r\n"], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  function downloadInvitationLinks() {
    const rows = (csvPreview?.rows ?? []).filter((row) => csvInviteLinks[row.row_number]);
    downloadCsv("invitation-links.csv", [
      ["email", "role", "departments", "invitation_link"],
      ...rows.map((row) => [row.email ?? "", row.role ?? "", row.departments.join(";"), csvInviteLinks[row.row_number]]),
    ]);
  }

  function downloadTemplate() {
    const sample = departments.slice(0, 2).map((group) => group.name);
    downloadCsv("employees-template.csv", [
      ["email", "role", "departments", "position", "reports_to"],
      ["ayesha.khan@yourcompany.com", "employee", sample[0] ?? "Finance", positions[0]?.title ?? "", ""],
      ["bilal.ahmed@yourcompany.com", "manager", sample.join(";") || "Finance;Operations", positions[1]?.title ?? "", ""],
    ]);
  }

  async function revokeInvitation(row: UserInvitation) {
    const ok = await confirm({ title: `Revoke invitation for ${row.email}?`, body: "The invitation link will stop working immediately.", confirmLabel: "Revoke", tone: "danger" });
    if (!ok) return;
    try { await api.revokeInvitation(row.id); toast("success", "Invitation revoked"); await load(); }
    catch (err) { toast("error", "Could not revoke invitation", describeError(err)); }
  }

  const permissionScopes = permissionCatalog.find((item) => item.permission === permission)?.scope_dimensions ?? ["category", "access_group"];
  const orgWidePermission = permissionScopes.length === 0;
  const categoryScoped = permissionScopes.includes("category");
  const departmentScoped = permissionScopes.includes("access_group");

  async function addGrant(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await api.createPermissionGrant({
        email: grantEmail,
        permission,
        category: categoryScoped ? category || null : null,
        scope_group_id: departmentScoped ? scopeGroupId || null : null,
      });
      setGrantEmail("");
      toast("success", "Permission granted");
      await load();
    } catch (err) { toast("error", "Could not grant permission", describeError(err)); }
    finally { setBusy(false); }
  }

  async function revokeGrant(grant: PermissionGrant) {
    const ok = await confirm({ title: "Revoke this permission?", body: "The decision access ends immediately. Existing approval records remain in the audit trail.", confirmLabel: "Revoke", tone: "danger" });
    if (!ok) return;
    try { await api.revokePermissionGrant(grant.id); toast("success", "Permission revoked"); await load(); }
    catch (err) { toast("error", "Could not revoke permission", describeError(err)); }
  }

  async function changeRole(user: ManagedUser, nextRole: "admin" | "manager" | "employee", selected: string[]) {
    const granting = nextRole === "admin" && user.role !== "admin";
    const revoking = user.role === "admin" && nextRole !== "admin";
    if (granting || revoking) {
      const ok = await confirm({
        title: granting ? `Make ${user.email} an administrator?` : `Remove administrator rights from ${user.email}?`,
        body: granting
          ? "Administrators can see every department, manage people, AI providers, company setup and all settings. Their department-manager duties and custom role assignments are removed. This is recorded in the audit log."
          : `They become ${roleLabel(nextRole).toLowerCase()} and are signed out everywhere immediately. This is recorded in the audit log.`,
        confirmLabel: granting ? "Make administrator" : "Remove administrator rights",
        tone: granting ? undefined : "danger",
      });
      if (!ok) return;
    }
    try { await api.updateAdminUser(user.id, nextRole, selected); toast("success", "Account updated"); await load(); }
    catch (err) { toast("error", "Could not update account", describeError(err)); }
  }

  async function toggleActive(user: ManagedUser, knowledgeTransferTo?: string) {
    const action = user.is_active ? "deactivate" : "reactivate";
    const successor = users.find((candidate) => candidate.id === knowledgeTransferTo);
    const body = user.is_active
      ? `Existing sessions are revoked immediately. Scoped and shared company knowledge transfers to ${successor?.email ?? "the selected steward"}; private drafts are not transferred. Documents, conversations and handoffs are retained.`
      : "The account can sign in again with its existing password.";
    const ok = await confirm({ title: `${action[0].toUpperCase()}${action.slice(1)} ${user.email}?`, body, confirmLabel: action, tone: user.is_active ? "danger" : "primary" });
    if (!ok) return;
    try { await api.setUserActive(user.id, !user.is_active, knowledgeTransferTo); toast("success", `Account ${user.is_active ? "deactivated" : "reactivated"}`); await load(); }
    catch (err) { toast("error", "Could not update account", describeError(err)); }
  }

  async function transferKnowledge(user: ManagedUser, successorUserId: string) {
    const successor = users.find((candidate) => candidate.id === successorUserId);
    const ok = await confirm({
      title: `Transfer company knowledge from ${user.email}?`,
      body: `Scoped and shared company knowledge will move to ${successor?.email ?? "the selected steward"}. Private drafts stay with the former account. The steward must be authorized for every department and cited source.`,
      confirmLabel: "Transfer knowledge",
      tone: "primary",
    });
    if (!ok) return;
    try {
      const result = await api.transferUserKnowledge(user.id, successorUserId);
      toast("success", `${result.transferred_entries} company knowledge entr${result.transferred_entries === 1 ? "y" : "ies"} transferred`);
      await load();
    } catch (err) { toast("error", "Could not transfer knowledge", describeError(err)); }
  }

  const pendingInvites = invitations.filter((row) => !row.accepted_at && !row.revoked_at);
  const tabs: { value: PeopleTab; label: string; count?: number }[] = [
    { value: "employees", label: "Employees", count: users.length },
    { value: "departments", label: "Departments", count: departments.length },
    { value: "positions", label: "Positions" },
    { value: "org", label: "Org chart" },
    { value: "limits", label: "Approval limits" },
    { value: "bulk", label: "Bulk import" },
    { value: "permissions", label: "Permissions", count: grants.length },
    { value: "templates", label: "Custom roles", count: customRoles.length },
  ];
  const visibleUsers = users.filter((user) => !accountQuery.trim() || user.email.toLowerCase().includes(accountQuery.trim().toLowerCase()));

  return (
    <>
      <PageHeader
        title="People & access"
        description="Invite employees, organize departments and decide who can see and approve what."
        actions={<Button onClick={() => void load()}><RefreshCw /> Refresh</Button>}
      />
      {loadError && <Alert tone="danger" className="mb-4">{loadError}</Alert>}

      <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Stat icon={<Users />} label="Employees" value={users.filter((user) => user.is_active).length} hint={`${users.filter((user) => !user.is_active).length} inactive`} />
        <Stat icon={<Building2 />} label="Departments" value={departments.length} />
        <Stat icon={<MailPlus />} label="Pending invitations" value={pendingInvites.length} hint="Valid for 72 hours" />
        <Stat icon={<ShieldCheck />} label="Custom roles" value={customRoles.length} />
      </div>

      {inviteLink && (
        <Alert tone="success" className="mb-4" title="Invitation link ready" action={<Button size="sm" onClick={() => void navigator.clipboard.writeText(inviteLink).then(() => toast("success", "Link copied")).catch(() => toast("error", "Could not copy", "Select and copy the link manually."))}><Copy /> Copy link</Button>}>
          <span className="break-all">{inviteLink}</span>
          <span className="mt-1 block text-xs opacity-80">Shown only now. It works once and expires in 72 hours; send it by email or chat.</span>
        </Alert>
      )}

      <Tabs className="mb-5" value={tab} onValueChange={(value) => setTab(value as PeopleTab)} items={tabs} />

      {tab === "employees" && (
        <div className="grid grid-cols-[minmax(0,1fr)] gap-5">
          <Card>
            <CardHeader
              icon={<UserPlus />}
              title="Invite people"
              description="Each person gets a single-use sign-up link (valid 72 hours). The app does not send email: copy or download the links."
              actions={<Tabs value={inviteMode} onValueChange={(value) => setInviteMode(value as "one" | "many")} items={[{ value: "one", label: "One person" }, { value: "many", label: "Several people" }]} />}
            />
            <CardBody>
              {inviteMode === "one" ? (
                <form className="grid gap-4" onSubmit={(event) => void invite(event)}>
                  <div className="grid gap-4 md:grid-cols-2">
                    <Field label="Work email"><Input type="email" required value={email} onChange={(event) => setEmail(event.target.value)} placeholder="ayesha.khan@yourcompany.com" /></Field>
                    <Field label="Role" hint={singleRole.roleTemplateId ? "Custom role: applied in the selected departments when they sign up." : undefined}>
                      <Select aria-label="Role" value={role} onValueChange={(value) => chooseRole("one", value)} options={roleSelectOptions} />
                    </Field>
                  </div>
                  <Field label={`Departments ${singleRole.needsDepartment ? "(required)" : "(optional)"}`}>
                    <DepartmentPicker departments={departments} selected={departmentIds} onToggle={toggleDepartment} />
                  </Field>
                  <PlacementFields value={placement.one} onChange={(value) => setPlacement({ ...placement, one: value })} positions={positions} people={users} />
                  <div><Button type="submit" variant="primary" loading={busy} disabled={busy || !email.trim() || (singleRole.needsDepartment && !departmentIds.length)}><Send /> Create invitation</Button></div>
                </form>
              ) : (
                <form className="grid gap-4" onSubmit={(event) => void inviteMany(event)}>
                  <Field label="Work emails" hint={`Separate with new lines, commas or spaces. Up to ${MAX_MULTI_INVITES} at a time; for a different role per person use Bulk import.`}>
                    <Textarea rows={4} value={multiEmails} onChange={(event) => setMultiEmails(event.target.value)} placeholder={"ayesha.khan@yourcompany.com\nbilal.ahmed@yourcompany.com, sara.ali@yourcompany.com"} />
                  </Field>
                  {multiEmails.trim() && <div className="flex flex-wrap gap-2" role="status">
                    <Badge tone="success" dot>{parsedEmails.valid.length} ready</Badge>
                    {parsedEmails.invalid.length > 0 && <Badge tone="danger" dot title={parsedEmails.invalid.join(", ")}>{parsedEmails.invalid.length} not an email</Badge>}
                    {parsedEmails.existing.length > 0 && <Badge tone="warning" dot title={parsedEmails.existing.join(", ")}>{parsedEmails.existing.length} already invited or registered</Badge>}
                    {parsedEmails.duplicates > 0 && <Badge>{parsedEmails.duplicates} duplicate{parsedEmails.duplicates === 1 ? "" : "s"} ignored</Badge>}
                    {parsedEmails.valid.length > MAX_MULTI_INVITES && <Badge tone="warning">Only the first {MAX_MULTI_INVITES} will be invited</Badge>}
                  </div>}
                  <Field label="Role for everyone" className="md:max-w-sm"><Select aria-label="Role for everyone" value={multiRole} onValueChange={(value) => chooseRole("many", value)} options={roleSelectOptions} /></Field>
                  <PlacementFields value={placement.many} onChange={(value) => setPlacement({ ...placement, many: value })} positions={positions} people={users} label="for everyone" />
                  <Field label={`Departments ${multiResolved.needsDepartment ? "(required)" : "(optional)"}`}>
                    <DepartmentPicker departments={departments} selected={multiDepartmentIds} onToggle={(id) => setMultiDepartmentIds((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id])} />
                  </Field>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button type="submit" variant="primary" loading={multiBusy} disabled={multiBusy || !parsedEmails.valid.length || (multiResolved.needsDepartment && !multiDepartmentIds.length)}><Send /> Invite {Math.min(parsedEmails.valid.length, MAX_MULTI_INVITES) || ""} {parsedEmails.valid.length === 1 ? "person" : "people"}</Button>
                    {multiResults.some((row) => row.link) && <Button onClick={() => downloadCsv("invitation-links.csv", [["email", "role", "invitation_link"], ...multiResults.filter((row) => row.link).map((row) => [row.email, multiResolved.label, row.link!])])}><Download /> Download {multiResults.filter((row) => row.link).length} invitation link{multiResults.filter((row) => row.link).length === 1 ? "" : "s"}</Button>}
                    {multiBusy && <span className="text-xs text-muted" role="status">Creating invitations… {multiResults.length} done</span>}
                  </div>
                  {multiResults.some((row) => row.error) && <Alert tone="danger" title="Some invitations were not created"><ul className="grid gap-0.5">{multiResults.filter((row) => row.error).map((row) => <li key={row.email}><strong>{row.email}</strong>: {row.error}</li>)}</ul></Alert>}
                </form>
              )}
            </CardBody>
          </Card>

          {pendingInvites.length > 0 && (
            <Card>
              <CardHeader icon={<Clock />} title="Waiting to sign up" description="Invitations that have not been used yet." />
              <Table>
                <thead><tr><th>Email</th><th>Role</th><th>Expires</th><th className="text-right">Actions</th></tr></thead>
                <tbody>{pendingInvites.map((row) => (
                  <tr key={row.id}>
                    <td className="font-medium text-foreground">{row.email}</td>
                    <td><Badge tone={row.role_template_id ? "primary" : "neutral"}>{customRoles.find((item) => item.id === row.role_template_id)?.name ?? roleLabel(row.role)}</Badge></td>
                    <td className="text-muted">{formatDate(row.expires_at)}</td>
                    <td className="text-right"><div className="inline-flex gap-1"><Button size="sm" disabled={busy} onClick={() => void resendInvitation(row)}><RotateCw /> New link</Button><Button size="sm" variant="danger-ghost" disabled={busy} onClick={() => void revokeInvitation(row)}>Revoke</Button></div></td>
                  </tr>
                ))}</tbody>
              </Table>
            </Card>
          )}

          <Card>
            <CardHeader
              icon={<Users />}
              title="Accounts"
              description="Change someone's role or departments, or deactivate a leaver. You cannot change your own role; another administrator must."
              actions={<Input icon={<Search />} type="search" value={accountQuery} onChange={(event) => setAccountQuery(event.target.value)} placeholder="Search by email" aria-label="Search accounts" className="h-8 w-56 text-xs" />}
            />
            {visibleUsers.length === 0 ? <EmptyState icon={<Search />} title="No accounts match" /> : (
              <Table>
                <thead><tr><th>Person</th><th>Role</th><th>Departments</th><th>Status</th><th className="text-right">Actions</th></tr></thead>
                <tbody>{visibleUsers.map((user) => (
                  <AccountRow
                    key={user.id}
                    user={user}
                    isSelf={user.id === meId}
                    groups={departments}
                    activeStewards={users.filter((candidate) => candidate.is_active && candidate.id !== user.id)}
                    onSave={changeRole}
                    onToggleActive={toggleActive}
                    onTransferKnowledge={transferKnowledge}
                  />
                ))}</tbody>
              </Table>
            )}
          </Card>
        </div>
      )}

      {tab === "departments" && (
        <Card>
          <CardHeader icon={<Building2 />} title="Departments" description="One department per team (for example Finance, HR, Sales). Restricted documents, agents and reports are limited to a department's members." />
          <CardBody className="grid gap-5">
            <form className="flex flex-col gap-2 sm:flex-row" onSubmit={(event) => void createDepartment(event)}>
              <Input value={departmentName} maxLength={120} placeholder="New department name, e.g. Finance" onChange={(event) => setDepartmentName(event.target.value)} aria-label="Department name" />
              <Button type="submit" variant="primary" disabled={busy || !departmentName.trim()}><Plus /> Add department</Button>
            </form>
            {departments.length ? (
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {departments.map((group) => (
                  <div key={group.id} className="flex items-center gap-3 rounded-lg border border-border p-3">
                    <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary-soft text-primary"><Building2 className="size-[18px]" /></span>
                    <div className="min-w-0">
                      <p className="truncate text-[13px] font-semibold text-foreground">{group.name}</p>
                      <p className="text-xs text-muted">{group.member_count} member{group.member_count === 1 ? "" : "s"}</p>
                    </div>
                  </div>
                ))}
              </div>
            ) : <EmptyState icon={<Building2 />} title="No departments yet" description="Add your first department above, then invite people into it." />}
          </CardBody>
        </Card>
      )}

      {tab === "bulk" && (
        <Card>
          <CardHeader icon={<FileSpreadsheet />} title="Bulk import from a spreadsheet" description="Up to 500 people per file; for larger teams upload several files." actions={<Button size="sm" onClick={downloadTemplate}><Download /> Download template</Button>} />
          <CardBody className="grid gap-5">
            <ol className="grid gap-3 md:grid-cols-3">
              {[
                ["Fill the template", "One row per person: email, role (employee or manager), departments separated by ;, and optionally position (an existing title) and reports_to (a manager's email)."],
                ["Upload and check", "The preview flags mistakes. Nothing is created yet."],
                ["Invite and send links", "Invite all valid rows, then download the links file and send it."],
              ].map(([title, text], index) => (
                <li key={title} className="flex gap-3 rounded-lg border border-border bg-surface-2/50 p-3">
                  <span className="grid size-7 shrink-0 place-items-center rounded-full bg-primary text-xs font-semibold text-primary-foreground">{index + 1}</span>
                  <span><span className="block text-[13px] font-semibold text-foreground">{title}</span><span className="block text-xs text-muted">{text}</span></span>
                </li>
              ))}
            </ol>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
              <label className="flex flex-1 cursor-pointer items-center gap-3 rounded-lg border border-dashed border-border-strong bg-surface-2/50 px-4 py-3 hover:border-primary hover:bg-primary-soft">
                <Upload className="size-4 text-muted" />
                <span className="truncate text-[13px] text-foreground-2">{csvFile ? csvFile.name : "Choose a CSV file…"}</span>
                <input className="sr-only" type="file" accept=".csv,text/csv" onChange={(event) => { setCsvFile(event.target.files?.[0] ?? null); setCsvPreview(null); setCsvError(null); setCsvInviteLinks({}); }} />
              </label>
              <Button variant="primary" loading={csvBusy && !bulkProgress} disabled={csvBusy || !csvFile} onClick={() => void previewCsv()}>Preview rows</Button>
            </div>
            {csvError && <Alert tone="danger">{csvError}</Alert>}
            {csvPreview && <>
              <div className="flex flex-wrap items-center gap-3" role="status">
                <Badge tone="success" dot>{csvPreview.valid_rows} ready</Badge>
                {csvPreview.invalid_rows > 0 && <Badge tone="warning" dot>{csvPreview.invalid_rows} need attention</Badge>}
                <span className="flex-1" />
                {Object.keys(csvInviteLinks).length > 0 && <Button size="sm" onClick={downloadInvitationLinks}><Download /> Download {Object.keys(csvInviteLinks).length} invitation link{Object.keys(csvInviteLinks).length === 1 ? "" : "s"}</Button>}
                <Button size="sm" variant="primary" loading={csvBusy && bulkProgress !== null && bulkProgress.done < bulkProgress.total} disabled={csvBusy || !csvPreview.rows.some((row) => row.valid && !csvInviteLinks[row.row_number])} onClick={() => void inviteAllCsvRows()}><Send /> Invite all valid rows</Button>
              </div>
              {bulkProgress && bulkProgress.done < bulkProgress.total && <p className="text-xs text-muted" role="status">Creating invitations… {bulkProgress.done} of {bulkProgress.total}</p>}
              {csvPreview.rows.length > 0 && (
                <div className="overflow-hidden rounded-lg border border-border">
                  <Table>
                    <thead><tr><th>Row</th><th>Email</th><th>Role</th><th>Departments</th><th>Position</th><th>Reports to</th><th>Check</th><th className="text-right">Action</th></tr></thead>
                    <tbody>{csvPreview.rows.map((row) => (
                      <tr key={row.row_number}>
                        <td className="text-muted">{row.row_number}</td><td className="font-medium text-foreground">{row.email ?? "—"}</td><td>{row.role ?? "—"}</td><td>{row.departments.join(", ") || "—"}</td><td>{row.position_title ?? "—"}</td><td className="text-xs">{row.reports_to_email ?? "—"}</td>
                        <td>{row.valid ? <Badge tone="success" dot>Ready</Badge> : <ul className="grid gap-0.5 text-xs text-danger-text">{row.errors.map((item, index) => <li key={index}>{item}</li>)}</ul>}</td>
                        <td className="text-right">{csvInviteLinks[row.row_number] ? <Badge tone="primary">Invited</Badge> : <Button size="sm" disabled={csvBusy || !row.valid} onClick={() => void createCsvInvitation(row)}>Invite</Button>}</td>
                      </tr>
                    ))}</tbody>
                  </Table>
                </div>
              )}
            </>}
          </CardBody>
        </Card>
      )}

      {tab === "permissions" && (
        <Card>
          <CardHeader icon={<KeyRound />} title="Individual permissions" description="Give one person a specific right without a custom role, for example letting HR invite employees into Finance." />
          <CardBody className="grid gap-5">
            <form className="grid items-end gap-3 md:grid-cols-2 xl:grid-cols-[1.2fr_1.4fr_1fr_1fr_auto]" onSubmit={(event) => void addGrant(event)}>
              <Field label="Person's email"><Input type="email" required value={grantEmail} onChange={(event) => setGrantEmail(event.target.value)} placeholder="name@yourcompany.com" /></Field>
              <Field label="Right"><Select aria-label="Right" value={permission} onValueChange={(value) => setPermission(value as PermissionGrant["permission"])} options={permissionCatalog.map((item) => ({ value: item.permission, label: permissionLabel(item.permission) }))} /></Field>
              <Field label="Category"><Select aria-label="Category" disabled={!categoryScoped} value={categoryScoped ? category : ""} onValueChange={setCategory} options={[{ value: "", label: categoryScoped ? "All categories" : "Not applicable" }, ...CATEGORIES.map((item) => ({ value: item, label: item[0].toUpperCase() + item.slice(1) }))]} /></Field>
              <Field label="Department"><Select aria-label="Department" disabled={!departmentScoped} value={departmentScoped ? scopeGroupId : ""} onValueChange={setScopeGroupId} options={[{ value: "", label: orgWidePermission ? "Whole organization" : "All departments" }, ...groups.map((group) => ({ value: group.id, label: group.name }))]} /></Field>
              <Button type="submit" variant="primary" disabled={busy || !grantEmail.trim() || !permissionCatalog.length}><Plus /> Grant</Button>
            </form>
            {grants.length > 0 ? (
              <div className="overflow-hidden rounded-lg border border-border">
                <Table>
                  <thead><tr><th>Person</th><th>Right</th><th>Applies to</th><th>Granted</th><th className="text-right"></th></tr></thead>
                  <tbody>{grants.map((grant) => {
                    const group = groups.find((item) => item.id === grant.scope_group_id);
                    return (
                      <tr key={grant.id}>
                        <td className="font-medium text-foreground">{grant.email}</td>
                        <td><Badge tone="primary">{permissionLabel(grant.permission)}</Badge></td>
                        <td className="text-foreground-2">{[grant.category ? grant.category : null, group ? group.name : grant.scope_group_id ? "One department" : "All departments"].filter(Boolean).join(" · ")}</td>
                        <td className="text-muted">{formatDate(grant.granted_at)}</td>
                        <td className="text-right"><Button size="sm" variant="danger-ghost" onClick={() => void revokeGrant(grant)}>Revoke</Button></td>
                      </tr>
                    );
                  })}</tbody>
                </Table>
              </div>
            ) : <EmptyState icon={<KeyRound />} title="No individual permissions yet" description="Most teams use Custom roles instead; individual rights are for exceptions." />}
          </CardBody>
        </Card>
      )}

      <CreateRoleDialog
        open={createRoleFor !== null}
        onOpenChange={(open) => { if (!open) setCreateRoleFor(null); }}
        existing={customRoles}
        permissions={permissionCatalog.map((item) => item.permission)}
        onCreated={(created) => {
          setCustomRoles((current) => [...current.filter((row) => row.id !== created.id), created]);
          if (createRoleFor === "many") setMultiRole(`custom:${created.id}`); else setRole(`custom:${created.id}`);
        }}
      />
      {tab === "positions" && <PositionsSection />}
      {tab === "org" && <OrgChartSection />}
      {tab === "limits" && <ApprovalLimitsSection />}
      {tab === "templates" && <RoleTemplatesSection users={users} departments={departments} permissions={permissionCatalog.map((item) => item.permission)} />}
    </>
  );
}

function roleLabel(role: string): string {
  if (role === "admin") return "Administrator";
  if (role === "manager") return "Manager";
  if (role === "employee" || role === "user") return "Employee";
  return role;
}

/** Department multi-select as toggle chips. */
function DepartmentPicker({ departments, selected, onToggle }: { departments: DepartmentGroup[]; selected: string[]; onToggle: (id: string) => void }) {
  if (!departments.length) return <p className="text-xs text-muted">No departments yet. Add one in the Departments tab.</p>;
  return (
    <div className="flex flex-wrap gap-2" role="group">
      {departments.map((group) => {
        const on = selected.includes(group.id);
        return (
          <button
            key={group.id}
            type="button"
            aria-pressed={on}
            onClick={() => onToggle(group.id)}
            className={cn(
              "inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-full border px-3 text-xs font-medium outline-none transition-colors focus-visible:ring-[3px] focus-visible:ring-ring",
              on ? "border-primary bg-primary-soft text-primary" : "border-border bg-surface text-foreground-2 hover:border-border-strong",
            )}
          >
            {on && <Check className="size-3.5" />}{group.name}
          </button>
        );
      })}
    </div>
  );
}

function AccountRow({
  user,
  isSelf,
  groups,
  activeStewards,
  onSave,
  onToggleActive,
  onTransferKnowledge,
}: {
  user: ManagedUser;
  isSelf: boolean;
  groups: DepartmentGroup[];
  activeStewards: ManagedUser[];
  onSave: (user: ManagedUser, role: "admin" | "manager" | "employee", departments: string[]) => void;
  onToggleActive: (user: ManagedUser, knowledgeTransferTo?: string) => void;
  onTransferKnowledge: (user: ManagedUser, successorUserId: string) => void;
}) {
  const currentRole = (user.role === "admin" || user.role === "manager" ? user.role : "employee") as "admin" | "manager" | "employee";
  const [editing, setEditing] = useState(false);
  const [offboarding, setOffboarding] = useState<"deactivate" | "transfer" | null>(null);
  const [role, setRole] = useState(currentRole);
  const [departments, setDepartments] = useState(user.departments.map((department) => department.id));
  const [stewardId, setStewardId] = useState("");
  useEffect(() => {
    setRole(currentRole);
    setDepartments(user.departments.map((department) => department.id));
  }, [user.role, user.departments]);
  useEffect(() => {
    if (!activeStewards.some((candidate) => candidate.id === stewardId)) {
      setStewardId(activeStewards.find((candidate) => candidate.role === "admin")?.id ?? activeStewards[0]?.id ?? "");
    }
  }, [activeStewards, stewardId]);

  return (
    <tr className={cn(!user.is_active && "opacity-70")}>
      <td>
        <div className="flex items-center gap-3">
          <span className="grid size-8 shrink-0 place-items-center rounded-full bg-primary-soft text-xs font-semibold text-primary">{user.email.slice(0, 2).toUpperCase()}</span>
          <div className="min-w-0">
            <p className="truncate font-medium text-foreground">{user.email}</p>
            {isSelf && <p className="text-xs text-muted">This is you</p>}
          </div>
        </div>
      </td>
      <td><Badge tone={user.role === "admin" ? "primary" : "neutral"}>{roleLabel(user.role)}</Badge></td>
      <td><div className="flex flex-wrap gap-1">{user.departments.length ? user.departments.map((item) => <Badge key={item.id}>{item.name}</Badge>) : <span className="text-muted">—</span>}</div></td>
      <td><Badge tone={user.is_active ? "success" : "warning"} dot>{user.is_active ? "Active" : "Inactive"}</Badge></td>
      <td className="text-right">
        <div className="inline-flex items-center gap-1">
          {!isSelf && user.is_active && <Button size="sm" onClick={() => setEditing(true)}><Pencil /> Edit</Button>}
          {!isSelf && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild><Button size="icon-sm" variant="ghost" aria-label={`More actions for ${user.email}`}><MoreHorizontal /></Button></DropdownMenuTrigger>
              <DropdownMenuContent>
                {user.is_active
                  ? <DropdownMenuItem danger disabled={!activeStewards.length} onSelect={() => setOffboarding("deactivate")}><UserX /> Deactivate account</DropdownMenuItem>
                  : <>
                    <DropdownMenuItem onSelect={() => onToggleActive(user)}><UserCheck /> Reactivate account</DropdownMenuItem>
                    <DropdownMenuItem disabled={!activeStewards.length} onSelect={() => setOffboarding("transfer")}><ArrowRightLeft /> Transfer knowledge</DropdownMenuItem>
                  </>}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
        <Dialog
          open={editing}
          onOpenChange={setEditing}
          title={`Edit ${user.email}`}
          description="Role and department changes apply immediately. Removing administrator rights signs the person out everywhere."
          footer={<><Button onClick={() => setEditing(false)}>Cancel</Button><Button variant="primary" disabled={(role === "manager" && !departments.length) || (role === currentRole && role === "admin")} onClick={() => { onSave(user, role, role === "admin" ? [] : departments); setEditing(false); }}>Save changes</Button></>}
        >
          <div className="grid gap-4 text-left">
            <Field label="Role"><Select aria-label={`Role for ${user.email}`} value={role} onValueChange={(value) => setRole(value as "admin" | "manager" | "employee")} options={[{ value: "employee", label: "Employee" }, { value: "manager", label: "Manager" }, { value: "admin", label: "Administrator" }]} /></Field>
            {role !== "admin" && <Field label={`Departments ${role === "manager" ? "(required)" : ""}`}><DepartmentPicker departments={groups} selected={departments} onToggle={(id) => setDepartments((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id])} /></Field>}
            {role === "admin" && <Alert tone="info">Administrators see every department, so department selection does not apply.</Alert>}
          </div>
        </Dialog>
        <Dialog
          open={offboarding !== null}
          onOpenChange={(open) => !open && setOffboarding(null)}
          title={offboarding === "transfer" ? `Transfer knowledge from ${user.email}` : `Deactivate ${user.email}`}
          description="Choose who inherits this person's shared company knowledge. Private drafts are not transferred."
          footer={<><Button onClick={() => setOffboarding(null)}>Cancel</Button><Button variant={offboarding === "transfer" ? "primary" : "danger"} disabled={!stewardId} onClick={() => { if (offboarding === "transfer") onTransferKnowledge(user, stewardId); else onToggleActive(user, stewardId); setOffboarding(null); }}>{offboarding === "transfer" ? "Transfer knowledge" : "Continue"}</Button></>}
        >
          <div className="text-left">
            <Field label="Knowledge goes to"><Select aria-label={`Knowledge steward for ${user.email}`} value={stewardId} onValueChange={setStewardId} placeholder="Choose a person" options={activeStewards.map((candidate) => ({ value: candidate.id, label: `${candidate.email} (${roleLabel(candidate.role)})` }))} /></Field>
          </div>
        </Dialog>
      </td>
    </tr>
  );
}


/** Optional position and manager for an invitation; they place the person in the org chart and grant nothing. */
function PlacementFields({ value, onChange, positions, people, label }: {
  value: { position: string; manager: string };
  onChange: (value: { position: string; manager: string }) => void;
  positions: Position[];
  people: ManagedUser[];
  label?: string;
}) {
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Field label={`Position ${label ?? ""} (optional)`.replace("  ", " ")} hint="Job title only; it does not change access.">
        <Select aria-label={`Position ${label ?? ""}`.trim()} value={value.position} onValueChange={(position) => onChange({ ...value, position })} options={[{ value: "", label: "No position" }, ...positions.map((p) => ({ value: p.id, label: p.title }))]} />
      </Field>
      <Field label={`Reports to ${label ?? ""} (optional)`.replace("  ", " ")}>
        <Select aria-label={`Reports to ${label ?? ""}`.trim()} value={value.manager} onValueChange={(manager) => onChange({ ...value, manager })} options={[{ value: "", label: "No manager" }, ...people.filter((u) => u.is_active).map((u) => ({ value: u.id, label: u.email }))]} />
      </Field>
    </div>
  );
}
