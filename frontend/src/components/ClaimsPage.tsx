import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate, timeAgo } from "../format";
import { IconClipboard, Spinner } from "../icons";
import type { Claim, ClaimKind, KnowledgeCategory, RagDocument } from "../types";
import { useFeedback } from "./Feedback";
import { Alert, PageHeader, SelectInput } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

const MAX_CHARS = 4000;

const KIND_LABEL: Record<ClaimKind, string> = {
  proposal: "Proposal",
  correction: "Correction",
};

const CATEGORY_LABEL: Record<KnowledgeCategory, string> = {
  policy: "Policy",
  financial: "Financial",
  legal: "Legal",
  client: "Client",
  operational: "Operational",
  general: "General",
};

const CATEGORIES = Object.keys(CATEGORY_LABEL) as KnowledgeCategory[];

function validate(text: string): string | null {
  const value = text.trim();
  if (!value) return "Write the proposed knowledge or correction.";
  if (value.length > MAX_CHARS) return `Keep it under ${MAX_CHARS} characters (${value.length} now).`;
  return null;
}

function sourceLabel(claim: Claim): string {
  if (!claim.sources.length) return "No sources attached";
  return claim.sources
    .map((source) => {
      if (source.kind === "document") return source.filename;
      if (source.kind === "url") return source.url;
      return source.note;
    })
    .join(" · ");
}

/** Submit and review the status of unverified organizational claims. These never become approved
 * knowledge, RAG sources or personal memory from this page. */
export function ClaimsPage() {
  const { toast, confirm } = useFeedback();
  const [items, setItems] = useState<Claim[] | null>(null);
  const [documents, setDocuments] = useState<RagDocument[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [kind, setKind] = useState<ClaimKind>("proposal");
  const [category, setCategory] = useState<KnowledgeCategory>("general");
  const [statement, setStatement] = useState("");
  const [note, setNote] = useState("");
  const [url, setUrl] = useState("");
  const [documentIds, setDocumentIds] = useState<string[]>([]);
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [claims, docs] = await Promise.all([api.claims(), api.documents()]);
      setItems(claims.items);
      setDocuments(docs.documents);
      setLoadError(null);
    } catch (err) {
      setLoadError(describeError(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useLiveRefresh(load);

  const draftError = validate(statement);

  async function add(event: React.FormEvent) {
    event.preventDefault();
    setTouched(true);
    if (draftError || saving) return;
    setSaving(true);
    try {
      const sources: Array<
        { kind: "document"; document_id: string } | { kind: "url"; url: string } | { kind: "note"; note: string }
      > = documentIds.map((id) => ({ kind: "document", document_id: id }));
      if (url.trim()) sources.push({ kind: "url", url: url.trim() });
      if (note.trim()) sources.push({ kind: "note", note: note.trim() });
      await api.createClaim({ kind, statement: statement.trim(), category, sources });
      toast("success", "Claim submitted", "It stays unverified until a later review. It is not used in answers.");
      setStatement("");
      setNote("");
      setUrl("");
      setDocumentIds([]);
      setTouched(false);
      await load();
    } catch (err) {
      toast("error", "Could not submit", describeError(err));
    } finally {
      setSaving(false);
    }
  }

  async function withdraw(claim: Claim) {
    const ok = await confirm({
      title: "Withdraw this claim?",
      body: "The statement stays on record as withdrawn. It is not published and cannot be edited.",
      confirmLabel: "Withdraw",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await api.withdrawClaim(claim.id);
      toast("success", "Claim withdrawn");
      await load();
    } catch (err) {
      toast("error", "Could not withdraw", describeError(err));
    }
  }

  function toggleDocument(id: string) {
    setDocumentIds((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
  }

  return (
    <>
      <PageHeader
        title="Claims"
        description="Propose organizational knowledge or a correction. Submissions stay unverified: they are not added to approved knowledge, not searchable, and not treated as facts."
      />

      <section className="card memory-add">
        <form onSubmit={(e) => void add(e)} noValidate>
          <div className="claims-row">
            <label className="field">
              <span className="field-label">Type</span>
              <div className="segmented" role="radiogroup" aria-label="Claim type">
                {(["proposal", "correction"] as const).map((value) => (
                  <button
                    key={value}
                    type="button"
                    role="radio"
                    aria-checked={kind === value}
                    className={kind === value ? "segment segment-active" : "segment"}
                    onClick={() => setKind(value)}
                  >
                    {KIND_LABEL[value]}
                  </button>
                ))}
              </div>
            </label>
            <label className="field">
              <span className="field-label">Category</span>
              <SelectInput className="claims-select" value={category} onChange={(e) => setCategory(e.target.value as KnowledgeCategory)}>
                {CATEGORIES.map((value) => (
                  <option key={value} value={value}>
                    {CATEGORY_LABEL[value]}
                  </option>
                ))}
              </SelectInput>
            </label>
          </div>
          <label className="field">
            <span className="field-label">Statement</span>
            <textarea
              className={`memory-input ${touched && draftError ? "input-invalid" : ""}`}
              rows={3}
              maxLength={MAX_CHARS + 50}
              placeholder='For example: "The refund window is 30 days from the invoice date."'
              value={statement}
              onChange={(e) => setStatement(e.target.value)}
              onBlur={() => setTouched(true)}
              aria-invalid={touched && !!draftError}
            />
            <span className={`memory-hint ${touched && draftError && statement ? "memory-hint-error" : ""}`}>
              {touched && draftError && statement ? draftError : `${statement.trim().length}/${MAX_CHARS}`}
            </span>
          </label>
          <label className="field">
            <span className="field-label">Source note (optional)</span>
            <input type="text" value={note} maxLength={500} placeholder="Where this comes from" onChange={(e) => setNote(e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">Source URL (optional)</span>
            <input type="text" value={url} maxLength={1024} placeholder="https://" onChange={(e) => setUrl(e.target.value)} />
          </label>
          {documents.length > 0 && (
            <fieldset className="field">
              <legend className="field-label">Readable documents (optional)</legend>
              <div className="claims-docs">
                {documents.map((doc) => (
                  <label key={doc.id} className="claims-doc">
                    <input
                      type="checkbox"
                      checked={documentIds.includes(doc.id)}
                      onChange={() => toggleDocument(doc.id)}
                    />
                    <span>{doc.filename}</span>
                  </label>
                ))}
              </div>
            </fieldset>
          )}
          <div className="memory-add-actions">
            <p className="muted claims-disclaimer">Your identity is recorded. The claim is not published from this form.</p>
            <button className="btn btn-primary" type="submit" disabled={saving || !!draftError}>
              {saving ? <Spinner size={14} /> : null} Submit claim
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
      ) : items.length === 0 ? (
        <section className="card empty memory-empty">
          <IconClipboard size={20} /> No claims submitted yet.
        </section>
      ) : (
        <section className="card card-flush">
          <ul className="memory-list">
            {items.map((claim) => (
              <li key={claim.id} className="memory-item">
                <div className="memory-body">
                  <div className="memory-meta">
                    <span className="chip chip-sm">{KIND_LABEL[claim.kind]}</span>
                    <span className="chip chip-sm">{CATEGORY_LABEL[claim.category]}</span>
                    <span className={`pill pill-sm ${claim.status === "submitted" ? "pill-warning" : ""}`}>
                      {claim.status === "submitted" ? "Unverified" : "Withdrawn"}
                    </span>
                    <span className="muted" title={formatDate(claim.created_at)}>
                      · submitted {timeAgo(claim.created_at)}
                    </span>
                  </div>
                  <p className="memory-content">{claim.statement}</p>
                  <p className="muted claims-sources">{sourceLabel(claim)}</p>
                </div>
                {claim.status === "submitted" && (
                  <div className="memory-actions">
                    <button className="btn btn-sm" type="button" onClick={() => void withdraw(claim)}>
                      Withdraw
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
