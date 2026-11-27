import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate, formatDay } from "../format";
import type { DocumentObligation, DocumentVersionDiff, DocumentVersionInfo, RagDocument } from "../types";
import { Spinner } from "../icons";
import { SelectInput } from "./ui";

const dateValue = (value: string | null) => value ? new Date(value).toISOString().slice(0, 10) : "";
const dateIso = (value: string) => value ? new Date(value + "T12:00:00").toISOString() : null;

export function DocumentLifecyclePanel({ document }: { document: RagDocument }) {
  const [open, setOpen] = useState(false);
  const [versions, setVersions] = useState<DocumentVersionInfo[]>([]);
  const [items, setItems] = useState<DocumentObligation[]>([]);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState(String(document.version));
  const [diff, setDiff] = useState<DocumentVersionDiff | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [title, setTitle] = useState("");
  const [quote, setQuote] = useState("");
  const [rationale, setRationale] = useState("");
  const [assignee, setAssignee] = useState("");
  const [due, setDue] = useState("");
  const [changeReason, setChangeReason] = useState("");
  const available = [
    { version: document.version, filename: document.filename },
    ...versions.map((row) => ({ version: row.version, filename: row.filename })),
  ].sort((a, b) => a.version - b.version);

  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const result = await Promise.all([api.documentVersions(document.id), api.documentObligations(document.id)]);
      setVersions(result[0]);
      setItems(result[1].obligations);
      const values = [...result[0].map((row) => row.version), document.version].sort((a, b) => a - b);
      if (values.length > 1) {
        setFrom((current) => current || String(values.at(-2)));
        setTo(String(values.at(-1)));
      }
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }, [document.id, document.version, document.filename]);

  useEffect(() => { if (open) void load(); }, [open, load]);

  async function compare() {
    setBusy(true);
    setError("");
    try {
      setDiff(await api.compareDocumentVersions(document.id, Number(from), Number(to)));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.createDocumentObligation(document.id, {
        title,
        rationale,
        source_quote: quote,
        ...(assignee.trim() ? { assigned_to_email: assignee.trim() } : {}),
        ...(due ? { due_at: dateIso(due) ?? undefined } : {}),
      });
      setTitle("");
      setQuote("");
      setRationale("");
      setAssignee("");
      setDue("");
      await load();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function update(item: DocumentObligation, patch: {
    status?: "open" | "acknowledged" | "completed";
    assigned_to_email?: string | null;
    due_at?: string | null;
  }) {
    if (!changeReason.trim()) {
      setError("Add a short reason before changing this obligation.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.updateDocumentObligation(item.id, {
        expected_version: item.version,
        ...patch,
        rationale: changeReason.trim(),
      });
      setChangeReason("");
      await load();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="document-lifecycle" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>Version comparison &amp; obligations</summary>
      {busy && <div className="inline-progress"><Spinner size={14} /> Working…</div>}
      {error && <p className="inline-alert inline-alert-error" role="alert">{error}</p>}
      <section className="lifecycle-section" aria-label="Document version comparison">
        <h4>Compare retained versions</h4>
        {available.length < 2 ? <p className="muted small">A comparison is available after this document has a replacement version.</p> : (
          <div className="lifecycle-compare">
            <label className="field"><span className="field-label">From</span><SelectInput value={from} onChange={(event) => setFrom(event.target.value)}>
              {available.map((item) => <option key={"from-" + item.version} value={item.version}>v{item.version} · {item.filename}</option>)}
            </SelectInput></label>
            <label className="field"><span className="field-label">To</span><SelectInput value={to} onChange={(event) => setTo(event.target.value)}>
              {available.map((item) => <option key={"to-" + item.version} value={item.version}>v{item.version} · {item.filename}</option>)}
            </SelectInput></label>
            <button className="btn btn-secondary" onClick={() => void compare()} disabled={busy || !from || from === to}>Compare text</button>
          </div>
        )}
        {diff && <div className="document-diff" aria-live="polite">
          <p className="muted small">Deterministic text diff: {diff.removed_lines} removed, {diff.added_lines} added. It does not interpret legal effect.</p>
          {diff.truncated && <p className="pill pill-warning">The displayed list is bounded; counts include all extracted changes.</p>}
          {diff.changes.length === 0 ? <p className="empty">No text changes were found.</p> : <ul>
            {diff.changes.map((change, index) => <li key={change.kind + "-" + index} className={"document-diff-" + change.kind}>
              <span>{change.kind === "added" ? "+" : "−"}</span> {change.text}
            </li>)}
          </ul>}
          {diff.obligation_impacts.length > 0 && <section className="lifecycle-section" aria-label="Tracked obligation source quote checks">
            <h4>Source-quote checks for tracked obligations</h4>
            <p className="muted small">The system checks whether each original quote still appears in the selected target version after whitespace normalization. This does not determine legal meaning or whether the duty is unchanged; review the text before updating an obligation.</p>
            {diff.obligation_impacts.map((impact) => <article className="obligation-card" key={impact.id}>
              <div className="obligation-heading"><strong>{impact.title}</strong><span className="pill pill-sm">{impact.status}</span></div>
              <p className={impact.quote_found_in_to_version ? "muted small" : "pill pill-warning"}>
                {impact.quote_found_in_to_version
                  ? `Exact source quote remains in v${diff.to_version}. Review surrounding wording and context.`
                  : `Exact source quote was not found in v${diff.to_version}. Review this obligation against the new text.`}
              </p>
              <blockquote>{impact.source_quote}</blockquote>
            </article>)}
          </section>}
        </div>}
      </section>
      <section className="lifecycle-section" aria-label="Document obligations">
        <h4>Assigned obligations</h4>
        {items.length === 0 ? <p className="muted small">No source-quoted obligations are recorded for this document.</p> : items.map((item) => (
          <article className="obligation-card" key={item.id}>
            <div className="obligation-heading"><strong>{item.title}</strong><span className="pill pill-sm">{item.status}</span></div>
            <p className="muted small">Assigned to {item.assigned_to_email}{item.due_at ? " · Due " + formatDay(item.due_at) : ""} · Source v{item.source_version}, {String(item.source_locator.label || ("page " + item.source_page_number))}</p>
            <blockquote>{item.source_quote}</blockquote>
            <p>{item.rationale}</p>
            {!item.source_current && <p className="pill pill-warning">The source has changed. Review the new version before changing status.</p>}
            {item.can_update_status && <label className="field"><span className="field-label">Reason for this change</span><input value={changeReason} onChange={(event) => setChangeReason(event.target.value)} maxLength={2000} required /></label>}
            {item.can_manage && <div className="obligation-manage">
              <label className="field"><span className="field-label">Reassign to an authorized reader</span><input type="email" value={assignee || item.assigned_to_email} onChange={(event) => setAssignee(event.target.value)} /></label>
              <label className="field"><span className="field-label">Due date</span><input type="date" value={due || dateValue(item.due_at)} onChange={(event) => setDue(event.target.value)} /></label>
              <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void update(item, {
                assigned_to_email: (assignee || item.assigned_to_email).trim() || null,
                due_at: dateIso(due || dateValue(item.due_at)),
              })}>Save assignment &amp; due date</button>
            </div>}
            {item.can_update_status && item.source_current && item.status !== "completed" && <div className="obligation-actions">
              {item.status === "open" && <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void update(item, { status: "acknowledged" })}>Acknowledge</button>}
              <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void update(item, { status: "completed" })}>Complete</button>
              {item.status === "acknowledged" && <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void update(item, { status: "open" })}>Reopen</button>}
            </div>}
            {item.status === "completed" && item.can_update_status && item.source_current && <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void update(item, { status: "open" })}>Reopen</button>}
            <details className="obligation-history"><summary>History ({item.history.length})</summary><ol>{item.history.map((entry) => <li key={entry.id}>
              <strong>{entry.action.replaceAll("_", " ")}</strong> · {formatDate(entry.created_at)}
              {entry.rationale && <p>{entry.rationale}</p>}
            </li>)}</ol></details>
          </article>
        ))}
        {document.can_modify && <form className="obligation-create" onSubmit={(event) => void create(event)}>
          <h4>Record a source-backed obligation</h4>
          <p className="muted small">The quote must match the current indexed version. No AI-generated duty is saved as fact.</p>
          <label className="field"><span className="field-label">Obligation</span><input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={180} required /></label>
          <label className="field"><span className="field-label">Exact source quote</span><textarea value={quote} onChange={(event) => setQuote(event.target.value)} minLength={8} maxLength={1200} required rows={3} /></label>
          <label className="field"><span className="field-label">Tracking rationale</span><input value={rationale} onChange={(event) => setRationale(event.target.value)} minLength={3} maxLength={2000} required /></label>
          <label className="field"><span className="field-label">Assignee email (blank assigns to you)</span><input type="email" value={assignee} onChange={(event) => setAssignee(event.target.value)} maxLength={320} /></label>
          <label className="field"><span className="field-label">Due date (optional)</span><input type="date" value={due} onChange={(event) => setDue(event.target.value)} /></label>
          <button className="btn btn-primary" disabled={busy || !document.searchable}>Create obligation</button>
        </form>}
      </section>
    </details>
  );
}
