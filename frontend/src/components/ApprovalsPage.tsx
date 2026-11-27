import { CheckCircle2, RefreshCw } from "lucide-react";
import { Alert, Button, Card, EmptyState, PageHeader } from "./ui";
import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import type { GovernanceClaim, GovernanceKnowledge, KnowledgeConflict, StaleKnowledge } from "../types";
import { useFeedback } from "./Feedback";
import { PolicyImpactSection } from "./PolicyImpactSection";
import { useLiveRefresh } from "../lib/useLiveRefresh";

export function ApprovalsPage() {
  const { toast } = useFeedback();
  const [claims, setClaims] = useState<GovernanceClaim[] | null>(null);
  const [knowledge, setKnowledge] = useState<GovernanceKnowledge[] | null>(null);
  const [staleKnowledge, setStaleKnowledge] = useState<StaleKnowledge[] | null>(null);
  const [conflicts, setConflicts] = useState<KnowledgeConflict[] | null>(null);
  const [conflictCursor, setConflictCursor] = useState<string | null>(null);
  const [conflictsLoading, setConflictsLoading] = useState(false);
  const [rationales, setRationales] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [claimQueue, knowledgeQueue, staleQueue, conflictQueue] = await Promise.all([
        api.governanceQueue(), api.governanceKnowledgeQueue(), api.staleKnowledgeQueue(), api.knowledgeConflictQueue(),
      ]);
      setClaims(claimQueue.items);
      setKnowledge(knowledgeQueue.items);
      setStaleKnowledge(staleQueue.items);
      setConflicts(conflictQueue.items);
      setConflictCursor(conflictQueue.next_cursor);
      setError(null);
    } catch (err) { setError(describeError(err)); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);

  async function decideClaim(claim: GovernanceClaim, stage: "review" | "publish") {
    const rationale = rationales[claim.id]?.trim() ?? "";
    if (rationale.length < 3) return toast("error", "Add a rationale", "Explain why the decision is supported.");
    setBusyId(claim.id);
    try {
      await api.decideClaim(claim.id, stage, rationale);
      toast("success", stage === "review" ? "Review recorded" : "Publication approval recorded", "This claim remains unverified and is not used in answers.");
      setRationales((current) => ({ ...current, [claim.id]: "" }));
      await load();
    } catch (err) { toast("error", "Decision could not be recorded", describeError(err)); }
    finally { setBusyId(null); }
  }

  async function decideKnowledge(item: GovernanceKnowledge, stage: "review" | "publish") {
    const rationale = rationales[item.id]?.trim() ?? "";
    if (rationale.length < 3) return toast("error", "Add a rationale", "Explain why the decision is supported.");
    setBusyId(item.id);
    try {
      await api.decideKnowledgeRevision(item.id, stage, rationale);
      toast(
        "success",
        stage === "review" ? "Review recorded" : "Knowledge published",
        stage === "publish" ? "The approved statement is now eligible for ACL-filtered RAG search." : "A different person must approve publication.",
      );
      setRationales((current) => ({ ...current, [item.id]: "" }));
      await load();
    } catch (err) { toast("error", "Decision could not be recorded", describeError(err)); }
    finally { setBusyId(null); }
  }

  async function reviewFreshness(item: StaleKnowledge) {
    const rationale = rationales[item.id]?.trim() ?? "";
    if (rationale.length < 3) return toast("error", "Add a rationale", "Confirm what you checked before extending this review date.");
    setBusyId(item.id);
    try {
      const result = await api.reviewKnowledgeFreshness(item.id, rationale);
      toast("success", "Freshness review recorded", `This item is eligible for ACL-filtered search until ${formatDate(result.next_review_due_at)}.`);
      setRationales((current) => ({ ...current, [item.id]: "" }));
      await load();
    } catch (err) { toast("error", "Freshness review could not be recorded", describeError(err)); }
    finally { setBusyId(null); }
  }

  async function decideConflict(item: KnowledgeConflict, decision: "confirmed" | "dismissed") {
    const rationale = rationales[item.id]?.trim() ?? "";
    if (rationale.length < 3) return toast("error", "Add a rationale", "Explain how you compared the two source-backed statements.");
    setBusyId(item.id);
    try {
      const result = await api.decideKnowledgeConflict(item.id, decision, rationale);
      setConflicts((current) => current?.map((candidate) => candidate.id === item.id
        ? { ...candidate, status: result.status, review: {
          reviewer_email: result.reviewer_email, decision: result.status, rationale: result.rationale, decided_at: result.decided_at,
        } }
        : candidate) ?? null);
      toast("success", decision === "confirmed" ? "Conflict confirmed" : "Potential conflict dismissed", "The decision and rationale were recorded in the review history.");
      setRationales((current) => ({ ...current, [item.id]: "" }));
    } catch (err) { toast("error", "Conflict decision could not be recorded", describeError(err)); }
    finally { setBusyId(null); }
  }

  async function loadOlderConflicts() {
    if (!conflictCursor || conflictsLoading) return;
    setConflictsLoading(true);
    try {
      const page = await api.knowledgeConflictQueue(conflictCursor);
      setConflicts((current) => {
        const existing = new Set((current ?? []).map((item) => item.id));
        return [...(current ?? []), ...page.items.filter((item) => !existing.has(item.id))];
      });
      setConflictCursor(page.next_cursor);
    } catch (err) { toast("error", "Older conflict reviews could not be loaded", describeError(err)); }
    finally { setConflictsLoading(false); }
  }

  const hasItems = Boolean(claims?.length || knowledge?.length || staleKnowledge?.length || conflicts?.length || conflictCursor);
  return (
    <>
      <PageHeader
        title="Approvals"
        description="Company knowledge waiting for your review or publication. You only see items in the categories and departments you are allowed to review; the person who publishes must be different from the reviewer."
        actions={<Button onClick={() => void load()}><RefreshCw /> Refresh</Button>}
      />
      <Alert tone="info" className="mb-5">Every decision is kept permanently in the history. Approved statements are hidden from answers again when their yearly review is overdue or their source document changes.</Alert>
      {error ? <div className="banner banner-error">{error}</div> : claims === null || knowledge === null || staleKnowledge === null || conflicts === null ? <div className="loading">Loading approvals…</div> : !hasItems ? (
        <Card><EmptyState icon={<CheckCircle2 />} title="Nothing is waiting for your decision" description="When employees submit knowledge or a published statement needs its yearly review, it appears here. An administrator can give review or publish rights in People & access → Permissions." /></Card>
      ) : (
        <>
          {(conflicts.length > 0 || conflictCursor) && <section className="approval-section">
            <h2>Potentially conflicting knowledge</h2>
            <p className="muted">Automated checks only flag candidates. Compare both cited sources and record a human decision; this does not silently replace or suppress either published statement.</p>
            {conflicts.length === 0 && <p className="muted">No in-scope conflict candidates are in this page.</p>}
            <div className="approval-list">{conflicts.map((item) => <article className="card approval-card" key={item.id}>
              <div className="approval-meta"><span className="chip chip-sm">{item.category}</span><span className={`chip chip-sm ${item.status === "open" ? "" : "chip-muted"}`}>{item.status === "open" ? "Needs review" : item.status === "confirmed" ? "Conflict confirmed" : "Dismissed"}</span><span className="muted">Detected {formatDate(item.detected_at)}</span></div>
              <div className="conflict-compare">
                {[{ label: "Submitted statement", value: item.submitted }, { label: "Existing published statement", value: item.existing }].map(({ label, value }) => <section className="conflict-side" key={value.revision_id}>
                  <h3>{label}</h3><p className="approval-statement">{value.statement}</p><p className="muted small">From {value.author_email} · {value.visibility === "restricted" ? "department-scoped" : "shared"}</p>
                  <ul className="knowledge-evidence">{value.sources.map((source, index) => <li key={`${source.filename}-${source.page_number}-${index}`}><div className="memory-meta"><span className="chip chip-sm">{source.filename}</span><span className="muted">page {source.page_number}</span></div><p className="knowledge-quote">{source.quote}</p></li>)}</ul>
                </section>)}
              </div>
              {item.review && <p className="muted small">Reviewed by {item.review.reviewer_email} · {formatDate(item.review.decided_at)} · {item.review.rationale}</p>}
              {item.status === "open" && <>
                <label className="field"><span className="field-label">Comparison rationale</span><textarea rows={2} value={rationales[item.id] ?? ""} onChange={(event) => setRationales((current) => ({ ...current, [item.id]: event.target.value }))} maxLength={2000} placeholder="Explain whether the statements are inconsistent and which source evidence you checked." /></label>
                <div className="approval-actions"><button className="btn btn-primary" disabled={busyId === item.id || (rationales[item.id]?.trim().length ?? 0) < 3} onClick={() => void decideConflict(item, "confirmed")}>Confirm conflict</button><button className="btn" disabled={busyId === item.id || (rationales[item.id]?.trim().length ?? 0) < 3} onClick={() => void decideConflict(item, "dismissed")}>Dismiss candidate</button></div>
              </>}
            </article>)}</div>
            {conflictCursor && <button className="btn" disabled={conflictsLoading} onClick={() => void loadOlderConflicts()}>{conflictsLoading ? "Loading…" : "Load older conflict reviews"}</button>}
          </section>}
          {staleKnowledge.length > 0 && <section className="approval-section">
            <h2>Knowledge due for freshness review</h2>
            <p className="muted">Overdue statements are withheld from answers until an authorized reviewer checks their cited evidence and records a rationale.</p>
            <div className="approval-list">{staleKnowledge.map((item) => <article className="card approval-card" key={item.id}>
              <div className="approval-meta"><span className="chip chip-sm">{item.category}</span><span className="chip chip-sm">Review overdue</span><span className="muted">From {item.author_email} · due {formatDate(item.review_due_at)}</span></div>
              <p className="approval-statement">{item.statement}</p>
              <ul className="knowledge-evidence">{item.sources.map((source, index) => <li key={`${source.filename}-${source.page_number}-${index}`}><div className="memory-meta"><span className="chip chip-sm">{source.filename}</span><span className="muted">page {source.page_number}</span></div><p className="knowledge-quote">{source.quote}</p></li>)}</ul>
              {item.last_freshness_review && <p className="muted small">Last freshness review by {item.last_freshness_review.reviewer_email} · {formatDate(item.last_freshness_review.reviewed_at)} · {item.last_freshness_review.rationale}</p>}
              <label className="field"><span className="field-label">Freshness review rationale</span><textarea rows={2} value={rationales[item.id] ?? ""} onChange={(event) => setRationales((current) => ({ ...current, [item.id]: event.target.value }))} maxLength={2000} placeholder="Record which source passages you checked and whether the statement remains current." /></label>
              <div className="approval-actions"><button className="btn btn-primary" disabled={busyId === item.id || (rationales[item.id]?.trim().length ?? 0) < 3} onClick={() => void reviewFreshness(item)}>Confirm still current</button></div>
            </article>)}</div>
          </section>}
          {claims.length > 0 && <section className="approval-section">
            <h2>Unverified claims</h2>
            <p className="muted">These proposals and corrections stay outside RAG, even after approval.</p>
            <div className="approval-list">{claims.map((claim) => <article className="card approval-card" key={claim.id}>
              <div className="approval-meta"><span className="chip chip-sm">{claim.kind}</span><span className="chip chip-sm">{claim.category}</span><span className="muted">From {claim.claimant_email} · {formatDate(claim.created_at)}</span></div>
              <p className="approval-statement">{claim.statement}</p>
              <div className="muted small">{claim.sources.length ? claim.sources.map((source) => source.kind === "document" ? source.filename : source.kind === "url" ? source.url : source.note).join(" · ") : "No source attached"}</div>
              {claim.decisions.length > 0 && <ul className="approval-decisions">{claim.decisions.map((decision) => <li key={`${decision.stage}-${decision.decided_at}`}><strong>{decision.stage === "review" ? "Reviewed" : "Publication approved"}</strong> by {decision.approver_email} · {decision.rationale}</li>)}</ul>}
              <DecisionControls allowed={claim.allowed_stages} value={rationales[claim.id] ?? ""} busy={busyId === claim.id} onChange={(value) => setRationales((current) => ({ ...current, [claim.id]: value }))} onDecide={(stage) => void decideClaim(claim, stage)} />
            </article>)}</div>
          </section>}
          {knowledge.length > 0 && <section className="approval-section">
            <h2>Organizational knowledge</h2>
            <p className="muted">Review the statement against source passages. Publishing makes it searchable only for people who can read all cited documents and match its scope.</p>
            <div className="approval-list">{knowledge.map((item) => <article className="card approval-card" key={item.id}>
              <div className="approval-meta"><span className="chip chip-sm">{item.category}</span><span className="chip chip-sm">{item.visibility === "restricted" ? "Department" : "Shared"}</span><span className="muted">From {item.author_email} · {formatDate(item.created_at)}</span></div>
              <p className="approval-statement">{item.statement}</p>
              <ul className="knowledge-evidence">{item.sources.map((source, index) => <li key={`${source.filename}-${source.page_number}-${index}`}><div className="memory-meta"><span className="chip chip-sm">{source.filename}</span><span className="muted">page {source.page_number}</span></div><p className="knowledge-quote">{source.quote}</p></li>)}</ul>
              {item.decisions.length > 0 && <ul className="approval-decisions">{item.decisions.map((decision) => <li key={`${decision.stage}-${decision.decided_at}`}><strong>{decision.stage === "review" ? "Reviewed" : "Publication approved"}</strong> by {decision.approver_email} · {decision.rationale}</li>)}</ul>}
              <DecisionControls allowed={item.allowed_stages} value={rationales[item.id] ?? ""} busy={busyId === item.id} onChange={(value) => setRationales((current) => ({ ...current, [item.id]: value }))} onDecide={(stage) => void decideKnowledge(item, stage)} />
            </article>)}</div>
          </section>}
        </>
      )}
      <PolicyImpactSection />
    </>
  );
}

function DecisionControls({
  allowed, value, busy, onChange, onDecide,
}: {
  allowed: Array<"review" | "publish">;
  value: string;
  busy: boolean;
  onChange: (value: string) => void;
  onDecide: (stage: "review" | "publish") => void;
}) {
  return <>
    <label className="field"><span className="field-label">Decision rationale</span><textarea rows={2} value={value} onChange={(event) => onChange(event.target.value)} maxLength={2000} placeholder="Record the evidence and reasoning for this decision." /></label>
    <div className="approval-actions">{allowed.includes("review") && <button className="btn btn-primary" disabled={busy} onClick={() => onDecide("review")}>Record review</button>}{allowed.includes("publish") && <button className="btn btn-primary" disabled={busy} onClick={() => onDecide("publish")}>Approve and publish</button>}</div>
  </>;
}
