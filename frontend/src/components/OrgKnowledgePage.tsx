import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate, timeAgo } from "../format";
import { IconLayers, Spinner } from "../icons";
import type { DepartmentGroup, KnowledgeCategory, KnowledgeDetail, KnowledgeRevision, KnowledgeVisibility, RagDocument } from "../types";
import { useFeedback } from "./Feedback";
import { Alert, PageHeader, SelectInput } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

const CATEGORY_LABEL: Record<KnowledgeCategory, string> = {
  policy: "Policy",
  financial: "Financial",
  legal: "Legal",
  client: "Client",
  operational: "Operational",
  general: "General",
};

const STATUS_LABEL: Record<KnowledgeRevision["status"], string> = {
  draft: "Draft",
  submitted: "Awaiting approval",
  published: "Published",
  superseded: "Superseded",
  withdrawn: "Withdrawn",
};

const ORPHAN_LABEL: Record<NonNullable<KnowledgeRevision["evidence"][number]["orphan_reason"]>, string> = {
  document_revision_changed: "Source revision changed",
  source_deleted: "Source deleted",
  passage_removed: "Passage removed",
};

/** Inspect and submit unpublished organizational drafts. Publication and review are not available here. */
export function OrgKnowledgePage() {
  const { toast, confirm } = useFeedback();
  const [items, setItems] = useState<KnowledgeDetail["revisions"] | null>(null);
  const [entries, setEntries] = useState<Array<{ id: string; current: KnowledgeRevision }>>([]);
  const [open, setOpen] = useState<KnowledgeDetail | null>(null);
  const [documents, setDocuments] = useState<RagDocument[]>([]);
  const [departments, setDepartments] = useState<DepartmentGroup[]>([]);
  const [role, setRole] = useState<string>("employee");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [statement, setStatement] = useState("");
  const [quote, setQuote] = useState("");
  const [documentId, setDocumentId] = useState("");
  const [category, setCategory] = useState<KnowledgeCategory>("general");
  const [visibility, setVisibility] = useState<KnowledgeVisibility>("private");
  const [departmentId, setDepartmentId] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [listed, docs, deptResponse, me] = await Promise.all([api.knowledge(), api.documents(), api.myDepartments(), api.me()]);
      setEntries(listed.items.map((item) => ({ id: item.id, current: item.current })));
      setItems(listed.items.map((item) => item.current));
      setDocuments(docs.documents.filter((doc) => doc.searchable));
      setDepartments(deptResponse.groups);
      setRole(me.role);
      if (!departmentId && deptResponse.groups.length) setDepartmentId(deptResponse.groups[0].id);
      setLoadError(null);
    } catch (err) {
      setLoadError(describeError(err));
    }
  }, [departmentId]);

  useEffect(() => {
    void load();
  }, [load]);
  useLiveRefresh(load);

  async function add(event: React.FormEvent) {
    event.preventDefault();
    if (!statement.trim() || !quote.trim() || !documentId || saving) return;
    setSaving(true);
    try {
      const created = await api.createKnowledge({
        statement: statement.trim(),
        category,
        visibility,
        ...(visibility === "restricted" ? { scope_group_id: departmentId } : {}),
        evidence: [{ document_id: documentId, quote: quote.trim() }],
      });
      toast("success", "Draft saved", "The draft is private or department scoped until it passes two separate approvals.");
      setStatement("");
      setQuote("");
      setOpen(created);
      await load();
    } catch (err) {
      toast("error", "Could not save draft", describeError(err));
    } finally {
      setSaving(false);
    }
  }

  async function submit(id: string) {
    try {
      setOpen(await api.submitKnowledge(id));
      toast("success", "Submitted for review", "A reviewer and a different publisher must approve it before it can appear in answers.");
      await load();
    } catch (err) {
      toast("error", "Could not submit knowledge", describeError(err));
    }
  }

  async function openEntry(id: string) {
    try {
      setOpen(await api.getKnowledge(id));
    } catch (err) {
      toast("error", "Could not open draft", describeError(err));
    }
  }

  async function withdraw(id: string) {
    const ok = await confirm({
      title: "Withdraw this draft?",
      body: "The statement and its source quotes stay on record. If published, withdrawal removes it from future search results.",
      confirmLabel: "Withdraw",
      tone: "danger",
    });
    if (!ok) return;
    try {
      setOpen(await api.withdrawKnowledge(id));
      toast("success", "Draft withdrawn");
      await load();
    } catch (err) {
      toast("error", "Could not withdraw", describeError(err));
    }
  }

  return (
    <>
      <PageHeader
        title="Org knowledge"
        description="Preserve important decisions and procedures with exact source passages. Private drafts stay personal; department knowledge becomes searchable only after a reviewer and a different publisher approve it."
      />

      <section className="card memory-add">
        <form onSubmit={(e) => void add(e)}>
          <label className="field">
            <span className="field-label">Statement</span>
            <textarea
              className="memory-input"
              rows={3}
              value={statement}
              placeholder="The statement you want recorded as a draft."
              onChange={(e) => setStatement(e.target.value)}
            />
          </label>
          <div className="claims-row">
            <label className="field">
              <span className="field-label">Category</span>
              <SelectInput className="claims-select" value={category} onChange={(e) => setCategory(e.target.value as KnowledgeCategory)}>
                {Object.entries(CATEGORY_LABEL).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </SelectInput>
            </label>
            <label className="field">
              <span className="field-label">Visibility</span>
              <SelectInput className="claims-select" value={visibility} onChange={(e) => setVisibility(e.target.value as KnowledgeVisibility)}>
                <option value="private">Private draft</option>
                <option value="restricted" disabled={departments.length === 0}>Department review</option>
                {role === "admin" && <option value="shared">Shared review</option>}
              </SelectInput>
            </label>
            {visibility === "restricted" && <label className="field">
              <span className="field-label">Department</span>
              <SelectInput className="claims-select" value={departmentId} onChange={(e) => setDepartmentId(e.target.value)}>
                {departments.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
              </SelectInput>
            </label>}
            <label className="field">
              <span className="field-label">Source document</span>
              <SelectInput className="claims-select" value={documentId} onChange={(e) => setDocumentId(e.target.value)}>
                <option value="">Select a ready document</option>
                {documents.map((doc) => (
                  <option key={doc.id} value={doc.id}>
                    {doc.filename}
                  </option>
                ))}
              </SelectInput>
            </label>
          </div>
          <label className="field">
            <span className="field-label">Exact quote from that document</span>
            <textarea
              className="memory-input"
              rows={2}
              value={quote}
              placeholder="Paste a sentence that appears in the indexed document."
              onChange={(e) => setQuote(e.target.value)}
            />
          </label>
          <div className="memory-add-actions">
            <p className="muted claims-disclaimer">A submitted item retains its source ACL. Private drafts are never sent to reviewers.</p>
            <button className="btn btn-primary" type="submit" disabled={saving || !statement.trim() || !quote.trim() || !documentId || (visibility === "restricted" && !departmentId)}>
              {saving ? <Spinner size={14} /> : null} Save draft
            </button>
          </div>
        </form>
      </section>

      {loadError ? (
        <Alert tone="danger" className="mb-4">{loadError}</Alert>
      ) : items === null ? (
        <div className="loading">
          <Spinner size={22} />
        </div>
      ) : entries.length === 0 ? (
        <section className="card empty memory-empty">
          <IconLayers size={20} /> No drafts yet.
        </section>
      ) : (
        <section className="card card-flush">
          <ul className="memory-list">
            {entries.map((entry) => (
              <li key={entry.id} className="memory-item">
                <button className="memory-body knowledge-open" type="button" onClick={() => void openEntry(entry.id)}>
                  <div className="memory-meta">
                    <span className="chip chip-sm">{CATEGORY_LABEL[entry.current.category]}</span>
                    <span className="pill pill-sm pill-warning">{STATUS_LABEL[entry.current.status]}</span>
                    <span className="muted">· revision {entry.current.revision_number}</span>
                    <span className="muted" title={formatDate(entry.current.created_at)}>
                      · {timeAgo(entry.current.created_at)}
                    </span>
                    {entry.current.evidence.some((item) => item.orphan_reason) && (
                      <span className="pill pill-sm pill-error">Stale evidence</span>
                    )}
                  </div>
                  <p className="memory-content">{entry.current.statement}</p>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {open && (
        <section className="card knowledge-detail">
          <header className="memory-meta">
            <strong>Provenance</strong>
            <span className="muted">{open.revisions.at(-1)?.visibility === "restricted" ? "Department scoped" : open.revisions.at(-1)?.visibility === "shared" ? "Shared" : "Private"}</span>
            {open.revisions.at(-1)?.status === "draft" && open.revisions.at(-1)?.visibility !== "private" && (
              <button className="btn btn-sm" type="button" onClick={() => void submit(open.id)}>
                Submit for review
              </button>
            )}
            {["draft", "submitted", "published"].includes(open.revisions.at(-1)?.status ?? "") && (
              <button className="btn btn-sm" type="button" onClick={() => void withdraw(open.id)}>
                Withdraw
              </button>
            )}
          </header>
          {open.revisions.map((revision) => (
            <article key={revision.id} className="knowledge-revision">
              <div className="memory-meta">
                <span className="chip chip-sm">Revision {revision.revision_number}</span>
                <span className="pill pill-sm">{STATUS_LABEL[revision.status]}</span>
              </div>
              <p className="memory-content">{revision.statement}</p>
              <ul className="knowledge-evidence">
                {revision.evidence.map((item) => (
                  <li key={item.id}>
                    <div className="memory-meta">
                      <span className="chip chip-sm">{item.filename}</span>
                      <span className="muted">page {item.page_number}</span>
                      <span className="muted">· revision {item.document_version}</span>
                      <span className="muted">
                        · characters {item.start_offset}–{item.end_offset}
                      </span>
                      {item.orphan_reason && <span className="pill pill-sm pill-error">{ORPHAN_LABEL[item.orphan_reason]}</span>}
                    </div>
                    <p className="knowledge-quote">
                      <span className="muted">{item.prefix}</span>
                      {item.quote}
                      <span className="muted">{item.suffix}</span>
                    </p>
                  </li>
                ))}
              </ul>
            </article>
          ))}
        </section>
      )}
    </>
  );
}
