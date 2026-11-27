import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import type { DepartmentGroup, EmployeeHandoff, User } from "../types";
import { useFeedback } from "./Feedback";
import { Alert, Button, PageHeader, SelectInput } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

export function HandoffsPage({ user }: { user: User }) {
  const { toast, confirm } = useFeedback();
  const [departments, setDepartments] = useState<DepartmentGroup[]>([]);
  const [items, setItems] = useState<EmployeeHandoff[] | null>(null);
  const [departmentId, setDepartmentId] = useState("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canAcknowledge = user.role === "admin" || user.role === "manager";

  const load = useCallback(async () => {
    try {
      const [departmentList, handoffList] = await Promise.all([api.myDepartments(), api.handoffs()]);
      setDepartments(departmentList.groups);
      setItems(handoffList.items);
      setDepartmentId((current) => current || departmentList.groups[0]?.id || "");
      setError(null);
    } catch (err) { setError(describeError(err)); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!departmentId || !title.trim() || !body.trim()) return;
    setBusy(true);
    try {
      await api.createHandoff({ department_id: departmentId, title: title.trim(), body: body.trim() });
      setTitle(""); setBody("");
      toast("success", "Handoff saved", "The department record is retained even if your account is later deactivated.");
      await load();
    } catch (err) { toast("error", "Could not save handoff", describeError(err)); }
    finally { setBusy(false); }
  }

  async function acknowledge(item: EmployeeHandoff) {
    const ok = await confirm({ title: "Acknowledge this handoff?", body: "The acknowledgement is recorded permanently. The handoff text stays unchanged.", confirmLabel: "Acknowledge" });
    if (!ok) return;
    try { await api.acknowledgeHandoff(item.id); toast("success", "Handoff acknowledged"); await load(); }
    catch (err) { toast("error", "Could not acknowledge", describeError(err)); }
  }

  return (
    <>
      <PageHeader
        title="Knowledge handoffs"
        description="Record procedures, recurring decisions, dependencies and open work so team knowledge survives role changes and offboarding."
        actions={<><Button onClick={() =>void load()}>Refresh</Button></>}
      />
      <Alert tone="info" className="mb-4">Handoffs are durable department records. Only the author, that department's assigned managers and administrators can read them. They are not automatically added to AI answers.</Alert>
      {error && <Alert tone="danger" className="mb-4">{error}</Alert>}
      <section className="card people-section">
        <h2>Capture a handoff</h2>
        {departments.length === 0 ? <p className="muted">You do not belong to a local department yet. Ask an administrator to assign one.</p> : <form onSubmit={(event) => void submit(event)}>
          <div className="people-inline-form">
            <label className="field"><span className="field-label">Department</span><SelectInput value={departmentId} onChange={(event) => setDepartmentId(event.target.value)}>{departments.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</SelectInput></label>
            <label className="field"><span className="field-label">Title</span><input required maxLength={180} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="For example, monthly close process" /></label>
          </div>
          <label className="field"><span className="field-label">Process notes</span><textarea required maxLength={12000} rows={6} value={body} onChange={(event) => setBody(event.target.value)} placeholder="Include steps, important contacts or systems, recurring exceptions, deadlines, and current open work." /></label>
          <button className="btn btn-primary" disabled={busy || !departmentId || !title.trim() || !body.trim()}>Save handoff</button>
        </form>}
      </section>
      {items === null ? <div className="loading">Loading handoffs…</div> : items.length === 0 ? <section className="card empty">No handoffs have been submitted yet.</section> : <div className="handoff-list">{items.map((item) => <article className="card handoff-card" key={item.id}>
        <div className="handoff-head"><div><div className="approval-meta"><span className="chip chip-sm">{item.department_name}</span><span className={`pill pill-sm ${item.status === "acknowledged" ? "pill-success" : "pill-warning"}`}>{item.status}</span></div><h3>{item.title}</h3><p className="muted small">By {item.author_email} · {formatDate(item.created_at)}</p></div>{canAcknowledge && item.status === "submitted" && item.author_email !== user.email && <button className="btn btn-sm" onClick={() => void acknowledge(item)}>Acknowledge</button>}</div>
        <p className="handoff-body">{item.body}</p>
        {item.acknowledged_at && <div className="muted small">Acknowledged by {item.acknowledged_by ?? "a manager"} · {formatDate(item.acknowledged_at)}</div>}
      </article>)}</div>}
    </>
  );
}
