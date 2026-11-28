import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import type { DepartmentGroup, UserInvitation } from "../types";
import { useFeedback } from "./Feedback";
import { Alert, Button, PageHeader, SelectInput } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

export function DepartmentManagerPage() {
  const { toast, confirm } = useFeedback();
  const [groups, setGroups] = useState<DepartmentGroup[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [members, setMembers] = useState<Array<{ user_id: string; email: string }> | null>(null);
  const [invitations, setInvitations] = useState<UserInvitation[]>([]);
  const [email, setEmail] = useState("");
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteLink, setInviteLink] = useState<string | null>(null);
  const [inviting, setInviting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await api.managedDepartments();
      setGroups(response.groups);
      setSelectedId((current) => response.groups.some((group) => group.id === current) ? current : response.groups[0]?.id ?? "");
      setError(null);
    } catch (err) { setError(describeError(err)); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);

  const loadMembers = useCallback(async () => {
    if (!selectedId) { setMembers([]); return; }
    try { setMembers((await api.departmentMembers(selectedId)).members); setError(null); }
    catch (err) { setError(describeError(err)); }
  }, [selectedId]);
  useEffect(() => { void loadMembers(); }, [loadMembers]);

  const loadInvitations = useCallback(async () => {
    if (!selectedId) { setInvitations([]); return; }
    try { setInvitations((await api.departmentInvitations(selectedId)).invitations); }
    catch (err) { setError(describeError(err)); }
  }, [selectedId]);
  useEffect(() => { void loadInvitations(); }, [loadInvitations]);

  async function add(event: React.FormEvent) {
    event.preventDefault();
    if (!selectedId || !email.trim()) return;
    try { await api.addDepartmentMember(selectedId, email.trim()); setEmail(""); toast("success", "Employee added to department"); await loadMembers(); }
    catch (err) { toast("error", "Could not add employee", describeError(err)); }
  }

  async function invite(event: React.FormEvent) {
    event.preventDefault();
    if (!selectedId || !inviteEmail.trim()) return;
    setInviting(true);
    try {
      const result = await api.createDepartmentInvitation(selectedId, inviteEmail.trim());
      const base = `${window.location.origin}${window.location.pathname}${window.location.search}`;
      setInviteLink(`${base}#/onboarding?token=${encodeURIComponent(result.token ?? "")}`);
      setInviteEmail("");
      toast("success", "Employee invitation created", "Copy the single-use link and send it using your approved channel.");
      await loadInvitations();
    } catch (err) { toast("error", "Could not invite employee", describeError(err)); }
    finally { setInviting(false); }
  }

  async function resendInvitation(row: UserInvitation) {
    try {
      const result = await api.resendDepartmentInvitation(selectedId, row.id);
      const base = `${window.location.origin}${window.location.pathname}${window.location.search}`;
      setInviteLink(`${base}#/onboarding?token=${encodeURIComponent(result.token ?? "")}`);
      toast("success", "Invitation link renewed", "The previous link is invalid. Copy the replacement using your approved channel.");
      await loadInvitations();
    } catch (err) { toast("error", "Could not resend invitation", describeError(err)); }
  }

  async function revokeInvitation(row: UserInvitation) {
    const ok = await confirm({ title: `Revoke invitation for ${row.email}?`, body: "The single-use link will stop working immediately.", confirmLabel: "Revoke", tone: "danger" });
    if (!ok) return;
    try { await api.revokeDepartmentInvitation(selectedId, row.id); toast("success", "Invitation revoked"); await loadInvitations(); }
    catch (err) { toast("error", "Could not revoke invitation", describeError(err)); }
  }

  async function remove(member: { user_id: string; email: string }) {
    const ok = await confirm({ title: `Remove ${member.email} from this department?`, body: "Their access to department-restricted documents and handoffs ends immediately.", confirmLabel: "Remove", tone: "danger" });
    if (!ok) return;
    try { await api.removeDepartmentMember(selectedId, member.user_id); toast("success", "Employee removed"); await loadMembers(); await load(); }
    catch (err) { toast("error", "Could not remove employee", describeError(err)); }
  }

  return (
    <>
      <PageHeader
        title="My departments"
        description="Manage employee membership only in departments assigned to you. Access changes take effect on the next request."
        actions={<><Button onClick={() =>void load()}>Refresh</Button></>}
      />
      {error && <Alert tone="danger" className="mb-4">{error}</Alert>}
      {inviteLink && <Alert tone="success" className="mb-4 invite-link"><span><strong>One-time employee invitation</strong><br /><a href={inviteLink}>{inviteLink}</a><small className="text-xs">Only this department is assigned. The link is shown once, expires in 72 hours, and is not emailed automatically.</small></span><button className="btn btn-sm" onClick={() => void navigator.clipboard.writeText(inviteLink).then(() => toast("success", "Link copied")).catch(() => toast("error", "Could not copy", "Select and copy the link manually."))}>Copy link</button></Alert>}
      {groups.length === 0 ? <section className="card empty"><strong>No department assignments</strong><span className="muted">Ask an administrator to assign you as a department manager.</span></section> : <>
        <section className="card people-section">
          <label className="field"><span className="field-label">Department</span><SelectInput value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>{groups.map((group) => <option key={group.id} value={group.id}>{group.name} · {group.member_count} members</option>)}</SelectInput></label>
          <form className="people-inline-form" onSubmit={(event) => void add(event)}><label className="field"><span className="field-label">Add an existing employee by email</span><input type="email" required value={email} onChange={(event) => setEmail(event.target.value)} /></label><button className="btn btn-primary" disabled={!email.trim()}>Add employee</button></form>
          <form className="people-inline-form" onSubmit={(event) => void invite(event)}><label className="field"><span className="field-label">Invite a new employee</span><input type="email" required value={inviteEmail} onChange={(event) => setInviteEmail(event.target.value)} /></label><button className="btn" disabled={inviting || !inviteEmail.trim()}>Create invitation link</button><p className="muted">This invitation grants membership in the selected department only. Use an approved channel to hand off the one-time link.</p></form>
        </section>
        <section className="card card-flush"><div className="card-title"><h2>Department members</h2><span className="pill pill-sm">{members?.length ?? 0}</span></div>{members === null ? <div className="loading">Loading members…</div> : members.length === 0 ? <p className="empty">No active employees are assigned.</p> : <ul className="people-member-list">{members.map((member) => <li key={member.user_id}><span>{member.email}</span><button className="btn btn-sm btn-ghost" onClick={() => void remove(member)}>Remove</button></li>)}</ul>}</section>
        <section className="card card-flush"><div className="card-title"><h2>Pending invitations for this department</h2><span className="pill pill-sm">{invitations.length}</span></div>{invitations.length === 0 ? <p className="empty">No pending invitations created by you for this department.</p> : <div className="table-wrap"><table><thead><tr><th>Email</th><th>Expires</th><th>Actions</th></tr></thead><tbody>{invitations.map((row) => <tr key={row.id}><td>{row.email}</td><td>{formatDate(row.expires_at)}</td><td><div className="people-row-actions"><button className="btn btn-sm" onClick={() => void resendInvitation(row)}>Resend link</button><button className="btn btn-sm btn-ghost" onClick={() => void revokeInvitation(row)}>Revoke</button></div></td></tr>)}</tbody></table></div>}</section>
      </>}
    </>
  );
}
