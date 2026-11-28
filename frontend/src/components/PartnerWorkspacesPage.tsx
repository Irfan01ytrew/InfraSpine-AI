import { type FormEvent, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import type { PartnerAdminIdentity, PartnerInvitation, PartnerSource, PartnerWorkspace } from "../types";
import { useFeedback } from "./Feedback";
import { Alert, Button, PageHeader, SelectInput } from "./ui";

export function PartnerWorkspacesPage() {
  const { toast, confirm } = useFeedback();
  const [workspaces, setWorkspaces] = useState<PartnerWorkspace[] | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [invitations, setInvitations] = useState<PartnerInvitation[]>([]);
  const [identities, setIdentities] = useState<PartnerAdminIdentity[]>([]);
  const [sources, setSources] = useState<PartnerSource[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [inviteLink, setInviteLink] = useState<string | null>(null);
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [inviteEmail, setInviteEmail] = useState("");
  const [sourceTitle, setSourceTitle] = useState("");
  const [sourceContent, setSourceContent] = useState("");
  const [sourceFile, setSourceFile] = useState<File | null>(null);

  async function loadWorkspaces() {
    try {
      const result = await api.partnerWorkspaces();
      setWorkspaces(result);
      setSelectedId((current) => current && result.some((workspace) => workspace.id === current) ? current : result[0]?.id ?? "");
      setError(null);
    } catch (err) {
      setError(describeError(err));
      setWorkspaces([]);
    }
  }

  useEffect(() => { void loadWorkspaces(); }, []);

  useEffect(() => {
    if (!selectedId) {
      setInvitations([]); setIdentities([]); setSources([]);
      return;
    }
    let active = true;
    Promise.all([api.partnerInvitations(selectedId), api.partnerIdentities(selectedId), api.partnerSources(selectedId)])
      .then(([inviteRows, identityRows, sourceRows]) => {
        if (!active) return;
        setInvitations(inviteRows); setIdentities(identityRows); setSources(sourceRows); setError(null);
      })
      .catch((err: unknown) => { if (active) setError(describeError(err)); });
    return () => { active = false; };
  }, [selectedId]);

  const selected = workspaces?.find((workspace) => workspace.id === selectedId) ?? null;
  const sourcesProcessing = sources.some((source) => source.status === "active" && (source.index_status === "pending" || source.index_status === "processing"));

  useEffect(() => {
    if (!selectedId || !sourcesProcessing) return;
    let active = true;
    const timer = window.setInterval(() => {
      api.partnerSources(selectedId).then((rows) => { if (active) setSources(rows); }).catch(() => {});
    }, 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [selectedId, sourcesProcessing]);

  async function createWorkspace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true); setError(null);
    try {
      const workspace = await api.createPartnerWorkspace({ slug: slug.trim(), name: name.trim() });
      setWorkspaces((current) => [...(current ?? []), workspace]);
      setSelectedId(workspace.id); setSlug(""); setName(""); setInviteLink(null);
      toast("success", "Partner workspace created");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function toggleWorkspace() {
    if (!selected) return;
    const next = selected.status === "active" ? "suspended" : "active";
    const ok = await confirm({
      title: `${next === "suspended" ? "Suspend" : "Reactivate"} ${selected.name}?`,
      body: next === "suspended" ? "Partner sign-ins and existing sessions will stop working immediately." : "Partner users will be able to sign in again.",
      confirmLabel: next === "suspended" ? "Suspend workspace" : "Reactivate",
      tone: next === "suspended" ? "danger" : "primary",
    });
    if (!ok) return;
    setBusy(true); setError(null);
    try {
      const updated = await api.setPartnerWorkspaceStatus(selected.id, next);
      setWorkspaces((current) => (current ?? []).map((workspace) => workspace.id === updated.id ? updated : workspace));
      toast("success", `Workspace ${next}`);
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function createInvitation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    setBusy(true); setError(null); setInviteLink(null);
    try {
      const invitation = await api.createPartnerInvitation(selected.id, inviteEmail.trim());
      setInvitations((current) => [invitation, ...current]);
      setInviteEmail("");
      if (invitation.token) {
        const base = `${window.location.origin}${window.location.pathname}${window.location.search}`;
        setInviteLink(`${base}#/partner?token=${encodeURIComponent(invitation.token)}`);
      }
      toast("success", "Partner invitation created", "Copy the one-time link and share it through your approved channel.");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function revokeInvitation(invitation: PartnerInvitation) {
    if (!selected) return;
    const ok = await confirm({ title: `Revoke invitation for ${invitation.email}?`, body: "This one-time link will stop working immediately.", confirmLabel: "Revoke", tone: "danger" });
    if (!ok) return;
    setBusy(true); setError(null);
    try {
      await api.revokePartnerInvitation(selected.id, invitation.id);
      setInvitations((current) => current.map((row) => row.id === invitation.id ? { ...row, revoked_at: new Date().toISOString() } : row));
      toast("success", "Partner invitation revoked");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function toggleIdentity(identity: PartnerAdminIdentity) {
    if (!selected) return;
    const active = !identity.is_active;
    const ok = await confirm({
      title: `${active ? "Reactivate" : "Deactivate"} ${identity.email}?`,
      body: active ? "They will be able to sign in again with their workspace password." : "Their current partner sessions will be revoked immediately.",
      confirmLabel: active ? "Reactivate" : "Deactivate", tone: active ? "primary" : "danger",
    });
    if (!ok) return;
    setBusy(true); setError(null);
    try {
      const updated = await api.setPartnerIdentityActive(selected.id, identity.id, active);
      setIdentities((current) => current.map((row) => row.id === updated.id ? updated : row));
      toast("success", `Partner access ${active ? "reactivated" : "deactivated"}`);
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function createSource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    setBusy(true); setError(null);
    try {
      const source = await api.createPartnerSource(selected.id, { title: sourceTitle.trim(), content: sourceContent.trim() });
      setSources((current) => [source, ...current]); setSourceTitle(""); setSourceContent("");
      toast("success", "Partner source added");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function uploadSourceFile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected || !sourceFile) return;
    const form = event.currentTarget;
    setBusy(true); setError(null);
    try {
      const source = await api.uploadPartnerSource(selected.id, sourceFile);
      setSources((current) => [source, ...current]); setSourceFile(null);
      form.reset();
      toast("success", "Partner source uploaded");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function retrySource(source: PartnerSource) {
    if (!selected) return;
    setBusy(true); setError(null);
    try {
      const updated = await api.retryPartnerSource(selected.id, source.id);
      setSources((current) => current.map((row) => row.id === updated.id ? updated : row));
      toast("success", "Source queued for processing");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function archiveSource(source: PartnerSource) {
    if (!selected) return;
    const ok = await confirm({ title: `Archive ${source.title}?`, body: "Archived source text is removed from partner assistant retrieval.", confirmLabel: "Archive source", tone: "danger" });
    if (!ok) return;
    setBusy(true); setError(null);
    try {
      await api.archivePartnerSource(selected.id, source.id);
      setSources((current) => current.map((row) => row.id === source.id ? { ...row, status: "archived", archived_at: new Date().toISOString() } : row));
      toast("success", "Partner source archived");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  return (
    <>
      <PageHeader
        title="External partner access"
        description="Manage partner identities and the separate text sources available to each workspace."
        actions={<><Button type="button" onClick={() =>void loadWorkspaces()} disabled={busy}>Refresh</Button></>}
      />
      {error && <Alert tone="danger" className="mb-4">{error}</Alert>}
      <section className="card partner-admin-create">
        <div><h2>Create partner workspace</h2><p className="muted">Each workspace has its own external identities and assistant source set.</p></div>
        <form className="partner-admin-workspace-form" onSubmit={(event) => void createWorkspace(event)}>
          <label className="field"><span className="field-label">Workspace name</span><input value={name} onChange={(event) => setName(event.target.value)} minLength={2} maxLength={160} required /></label>
          <label className="field"><span className="field-label">URL slug</span><input value={slug} onChange={(event) => setSlug(event.target.value)} pattern="[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?" minLength={1} maxLength={80} placeholder="acme-partner" required /></label>
          <button className="btn btn-primary" type="submit" disabled={busy || !name.trim() || !slug.trim()}>Create workspace</button>
        </form>
      </section>
      {workspaces === null ? <div className="loading">Loading partner workspaces…</div> : workspaces.length === 0 ? (
        <section className="card empty"><strong>No partner workspaces yet</strong><span className="muted">Create a workspace above to start an external customer or partner boundary.</span></section>
      ) : (
        <>
          <section className="card partner-admin-selector">
            <label className="field"><span className="field-label">Partner workspace</span><SelectInput value={selectedId} onChange={(event) => { setSelectedId(event.target.value); setInviteLink(null); }}>
              {workspaces.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.name} · {workspace.slug} · {workspace.status}</option>)}
            </SelectInput></label>
            {selected && <div className="partner-admin-workspace-state"><span className={`pill ${selected.status === "active" ? "pill-success" : "pill-muted"}`}>{selected.status}</span>
              <button className={`btn ${selected.status === "active" ? "btn-danger" : "btn-primary"}`} type="button" onClick={() => void toggleWorkspace()} disabled={busy}>
                {selected.status === "active" ? "Suspend workspace" : "Reactivate workspace"}
              </button>
            </div>}
          </section>

          {inviteLink && <Alert tone="success" className="mb-4 partner-invite-link"><div><strong>One-time partner invitation link</strong><p className="muted">It expires in 72 hours. Share it through your approved channel; it is shown only after creation.</p><a href={inviteLink}>{inviteLink}</a></div>
            <button className="btn btn-sm" type="button" onClick={() => void navigator.clipboard.writeText(inviteLink).then(() => toast("success", "Invitation link copied")).catch(() => toast("error", "Could not copy", "Select and copy the link manually."))}>Copy link</button></Alert>}

          <div className="partner-admin-grid">
            <section className="card partner-admin-section">
              <div className="card-title"><div><h2>Invitations</h2><p className="muted">Single-use external identity setup links.</p></div><span className="pill pill-sm">{invitations.length}</span></div>
              <form className="partner-admin-inline-form" onSubmit={(event) => void createInvitation(event)}>
                <label className="field"><span className="field-label">Partner email</span><input type="email" value={inviteEmail} onChange={(event) => setInviteEmail(event.target.value)} maxLength={320} required /></label>
                <button className="btn btn-primary" type="submit" disabled={busy || !selected || !inviteEmail.trim()}>Create invitation</button>
              </form>
              {invitations.length === 0 ? <p className="empty">No invitations created for this workspace.</p> : <ul className="partner-admin-list">
                {invitations.map((invitation) => <li key={invitation.id}>
                  <div><strong>{invitation.email}</strong><small className="text-xs">{invitation.accepted_at ? "Accepted" : invitation.revoked_at ? "Revoked" : Date.parse(invitation.expires_at) <= Date.now() ? "Expired" : "Pending"} · expires {formatDate(invitation.expires_at)}</small></div>
                  {!invitation.accepted_at && !invitation.revoked_at && Date.parse(invitation.expires_at) > Date.now() && <button className="btn btn-sm btn-ghost" type="button" onClick={() => void revokeInvitation(invitation)} disabled={busy}>Revoke</button>}
                </li>)}
              </ul>}
            </section>

            <section className="card partner-admin-section">
              <div className="card-title"><div><h2>Partner identities</h2><p className="muted">Deactivation revokes existing partner sessions.</p></div><span className="pill pill-sm">{identities.length}</span></div>
              {identities.length === 0 ? <p className="empty">No partner accounts have accepted invitations.</p> : <ul className="partner-admin-list">
                {identities.map((identity) => <li key={identity.id}><div><strong>{identity.email}</strong><small className="text-xs">{identity.is_active ? "Active" : "Deactivated"} · created {formatDate(identity.created_at)}</small></div>
                  <button className="btn btn-sm btn-ghost" type="button" onClick={() => void toggleIdentity(identity)} disabled={busy}>{identity.is_active ? "Deactivate" : "Reactivate"}</button></li>)}
              </ul>}
            </section>
          </div>

          <section className="card partner-admin-section partner-admin-sources">
            <div className="card-title"><div><h2>Workspace assistant sources</h2><p className="muted">Sources are available only to this partner workspace. Paste text or upload documents.</p></div><span className="pill pill-sm">{sources.filter((source) => source.status === "active").length} active</span></div>
            <form className="partner-admin-source-form" onSubmit={(event) => void createSource(event)}>
              <label className="field"><span className="field-label">Source title</span><input value={sourceTitle} onChange={(event) => setSourceTitle(event.target.value)} minLength={1} maxLength={180} required /></label>
              <label className="field"><span className="field-label">Source text</span><textarea value={sourceContent} onChange={(event) => setSourceContent(event.target.value)} maxLength={50000} rows={5} required /></label>
              <p className="muted small">Maximum 50,000 characters. Source content is not returned in the Admin source list.</p>
              <button className="btn btn-primary" type="submit" disabled={busy || !selected || !sourceTitle.trim() || !sourceContent.trim()}>Add text source</button>
            </form>
            <form className="partner-admin-source-upload" onSubmit={(event) => void uploadSourceFile(event)}>
              <label className="field"><span className="field-label">Import a source file</span><input type="file" accept=".txt,.md,.pdf,.docx,.xlsx,.pptx" onChange={(event) => setSourceFile(event.target.files?.[0] ?? null)} /></label>
              <p className="muted small">PDF, Word, Excel, PowerPoint, or UTF-8 text/Markdown. Text files are limited to 200 KB and 50,000 characters.</p>
              <button className="btn" type="submit" disabled={busy || !selected || !sourceFile}>Import file</button>
            </form>
            {sources.length === 0 ? <p className="empty">No partner sources have been added.</p> : <ul className="partner-admin-list partner-source-list">
              {sources.map((source) => <li key={source.id}><div><strong>{source.title}</strong><small className="text-xs">{source.status} · {source.source_format} · {source.index_status} · SHA-256 {source.content_sha256} · {formatDate(source.created_at)}</small></div>
                {source.status === "active" && <div>{source.index_status === "failed" && <button className="btn btn-sm btn-ghost" type="button" onClick={() => void retrySource(source)} disabled={busy}>Retry processing</button>}
                <button className="btn btn-sm btn-ghost" type="button" onClick={() => void archiveSource(source)} disabled={busy}>Archive</button></div>}</li>)}
            </ul>}
          </section>
        </>
      )}
    </>
  );
}
