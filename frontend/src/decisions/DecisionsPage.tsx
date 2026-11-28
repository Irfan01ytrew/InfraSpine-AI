import { useCallback, useEffect, useState } from "react";
import { Clock3, RefreshCw, ScrollText, Search } from "lucide-react";
import { describeError } from "../api";
import { decisionsApi } from "../decisions/api";
import { reviewState, reviewStateLabel, sortDue } from "../decisions/reviewState.mjs";
import type { Decision, DecisionScope, Dependencies, ReviewDigest, ReviewOutcome } from "../decisions/types";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, Input, NativeSelect, PageHeader, Skeleton, Stat, Textarea } from "../components/ui";

const SENSITIVE_KINDS = new Set(["finance", "supplier", "people"]);
const KINDS = ["policy", "finance", "operations", "supplier", "people", "technology", "other"];

function statusTone(status: Decision["status"]): "success" | "neutral" | "warning" | "danger" {
  if (status === "active") return "success";
  if (status === "expired") return "warning";
  if (status === "revoked") return "danger";
  return "neutral";
}

function reviewTone(state: string): "danger" | "warning" | "neutral" {
  if (state === "overdue") return "danger";
  if (state === "due") return "warning";
  return "neutral";
}

function formatDate(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleDateString();
}

function formatDateTime(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

export function DecisionsPage() {
  const [scopes, setScopes] = useState<DecisionScope[]>([]);
  const [scopeId, setScopeId] = useState("");
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [digest, setDigest] = useState<ReviewDigest | null>(null);
  const [selected, setSelected] = useState<Decision | null>(null);
  const [deps, setDeps] = useState<Dependencies | null>(null);
  const [whyQuery, setWhyQuery] = useState("");
  const [whyResults, setWhyResults] = useState<Decision[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);

  // Review form.
  const [outcome, setOutcome] = useState<ReviewOutcome>("confirm");
  const [reason, setReason] = useState("");
  const [nextReviewAt, setNextReviewAt] = useState("");
  const [supersedeTitle, setSupersedeTitle] = useState("");
  const [supersedeRationale, setSupersedeRationale] = useState("");
  const [approvalId, setApprovalId] = useState("");
  const [reviewBusy, setReviewBusy] = useState(false);

  // Record form.
  const [recordTitle, setRecordTitle] = useState("");
  const [recordRationale, setRecordRationale] = useState("");
  const [recordKind, setRecordKind] = useState("operations");
  const [recordReviewAt, setRecordReviewAt] = useState("");
  const [recordApprovalId, setRecordApprovalId] = useState("");
  const [recordBusy, setRecordBusy] = useState(false);

  const loadScope = useCallback(async (id: string) => {
    setLoading(true);
    setError("");
    try {
      const [list, summary] = await Promise.all([
        decisionsApi.list(id, { include_archived: true, limit: 100 }),
        decisionsApi.digest(id),
      ]);
      setDecisions(list.decisions);
      setDigest(summary);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let live = true;
    decisionsApi.scopes()
      .then(({ scopes: found }) => {
        if (!live) return;
        setScopes(found);
        if (found.length) {
          setScopeId(current => current || found[0].id);
        }
      })
      .catch(err => { if (live) setError(describeError(err)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, []);

  useEffect(() => {
    if (scopeId) void loadScope(scopeId);
  }, [scopeId, revision, loadScope]);

  const selectDecision = useCallback(async (decision: Decision) => {
    setSelected(decision);
    setDeps(null);
    setWhyResults(null);
    setDetailLoading(true);
    try {
      const [full, dependencies] = await Promise.all([
        decisionsApi.get(decision.scope_id, decision.id),
        decisionsApi.dependencies(decision.scope_id, decision.id),
      ]);
      setSelected(full);
      setDeps(dependencies);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const runWhy = useCallback(async () => {
    if (!scopeId || !whyQuery.trim()) return;
    try {
      const result = await decisionsApi.why(scopeId, whyQuery.trim());
      setWhyResults(result.decisions);
    } catch (err) {
      setError(describeError(err));
    }
  }, [scopeId, whyQuery]);

  const submitReview = useCallback(async () => {
    if (!selected || !reason.trim()) return;
    setReviewBusy(true);
    setError("");
    setNotice("");
    try {
      const reviewed = await decisionsApi.review(selected.scope_id, selected.id, {
        outcome,
        reason: reason.trim(),
        ...(outcome === "confirm" && nextReviewAt ? { review_at: nextReviewAt } : {}),
        ...(outcome === "supersede"
          ? { title: supersedeTitle.trim(), rationale: supersedeRationale.trim() } : {}),
        ...(approvalId.trim() ? { approval_id: approvalId.trim() } : {}),
      });
      setNotice(`Review recorded: ${reviewed.title} is now ${reviewed.status}.`);
      setReason("");
      setNextReviewAt("");
      setSupersedeTitle("");
      setSupersedeRationale("");
      setApprovalId("");
      setRevision(value => value + 1);
      await selectDecision(reviewed);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setReviewBusy(false);
    }
  }, [selected, reason, outcome, nextReviewAt, supersedeTitle, supersedeRationale, approvalId, selectDecision]);

  const submitRecord = useCallback(async () => {
    if (!scopeId || !recordTitle.trim() || !recordRationale.trim()) return;
    setRecordBusy(true);
    setError("");
    setNotice("");
    try {
      const created = await decisionsApi.create(scopeId, {
        title: recordTitle.trim(),
        rationale: recordRationale.trim(),
        kind: recordKind as Decision["kind"],
        ...(recordReviewAt ? { review_at: recordReviewAt } : {}),
        ...(recordApprovalId.trim() ? { approval_id: recordApprovalId.trim() } : {}),
      });
      setNotice(`Decision recorded: ${created.title}.`);
      setRecordTitle("");
      setRecordRationale("");
      setRecordReviewAt("");
      setRecordApprovalId("");
      setRevision(value => value + 1);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setRecordBusy(false);
    }
  }, [scopeId, recordTitle, recordRationale, recordKind, recordReviewAt, recordApprovalId]);

  const today = new Date().toISOString().slice(0, 10);
  const due = digest ? sortDue(digest.due) : [];
  const sensitiveSelected = selected !== null && SENSITIVE_KINDS.has(selected.kind);
  const needsApproval = selected !== null && sensitiveSelected && outcome !== "confirm";

  return (
    <div className="grid gap-5 pb-8">
      <PageHeader
        eyebrow="ORGANIZATIONAL MEMORY"
        title="Decision memory"
        description="What was decided, why, by whom, and on what evidence — with a review date on every decision. Recording never grants access or moves money; sensitive kinds need an owner approval first."
        actions={<Button variant="secondary" onClick={() => setRevision(value => value + 1)} disabled={loading}><RefreshCw className={loading ? "animate-spin" : ""} /> Refresh</Button>}
      />
      {error && <Alert tone="danger" title="Decision data could not be loaded">{error}</Alert>}
      {notice && <Alert tone="success" title="Done">{notice}</Alert>}

      {loading && !scopes.length && <Skeleton className="h-64 w-full" />}
      {!loading && !scopes.length && !error && (
        <Card><EmptyState icon={<ScrollText />} title="No decision scope is available" description="Decision memory is scope-based. An administrator needs to grant decision.read on a scope before decisions can be shown." /></Card>
      )}

      {scopes.length > 0 && (
        <>
          <label className="grid max-w-lg gap-1 text-xs font-semibold text-foreground-2">
            Decision scope
            <NativeSelect value={scopeId} onChange={event => { setScopeId(event.target.value); setSelected(null); setDeps(null); }}>
              {scopes.map(item => <option key={item.id} value={item.id}>{item.name} · {item.kind}</option>)}
            </NativeSelect>
          </label>

          {digest && (
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <Stat label="Reviews overdue" value={digest.overdue_count} hint="Review date has passed" icon={<Clock3 />} />
              <Stat label="Due in the next 7 days" value={digest.due_soon_count} hint="Review soon" icon={<Clock3 />} />
              <Stat label="Active decisions" value={digest.total_active} hint="In this scope" icon={<ScrollText />} />
              <Stat label="Flagged rationale" value={digest.needs_review_count} hint="Instruction-like wording" icon={<Clock3 />} />
            </div>
          )}

          <Card>
            <CardHeader title="Review queue" description="Active decisions whose review date is due, most overdue first." icon={<Clock3 />} />
            <CardBody className="grid gap-2">
              {due.length ? due.map(item => (
                <div key={item.id} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold">{item.title}</p>
                    <p className="text-xs text-muted">Review {formatDate(item.review_at)} · {item.kind}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <Badge tone={reviewTone(reviewState(item.review_at, today))}>{reviewStateLabel(reviewState(item.review_at, today))}</Badge>
                    <Button size="sm" variant="secondary" onClick={() => void selectDecision(item)}>Review</Button>
                  </div>
                </div>
              )) : <p className="text-sm text-muted">Nothing is due for review in this scope.</p>}
            </CardBody>
          </Card>

          <div className="grid gap-4 xl:grid-cols-2">
            <Card>
              <CardHeader title="Decision timeline" description="Newest first. Superseded and revoked decisions stay readable." icon={<ScrollText />} />
              <CardBody className="grid gap-2">
                {decisions.length ? decisions.map(item => (
                  <button key={item.id} className="module-launch-card text-left" onClick={() => void selectDecision(item)}>
                    <span className="min-w-0">
                      <strong className="truncate">{item.title}</strong>
                      <span>Decided {formatDate(item.decided_at)} · {item.kind} · review {formatDate(item.review_at)}</span>
                    </span>
                    <span className="flex shrink-0 items-center gap-2">
                      {item.needs_review && <Badge tone="warning">Flagged</Badge>}
                      <Badge tone={statusTone(item.status)}>{item.status}</Badge>
                    </span>
                  </button>
                )) : <p className="text-sm text-muted">No decisions recorded in this scope yet.</p>}
              </CardBody>
            </Card>

            <Card>
              <CardHeader title="Record a decision" description="A decision is a record, never a permission. Sensitive kinds (finance, supplier, people) need an owner approval id." icon={<ScrollText />} />
              <CardBody className="grid gap-3">
                <Field label="Title"><Input value={recordTitle} maxLength={200} onChange={event => setRecordTitle(event.target.value)} placeholder="What was decided?" /></Field>
                <Field label="Rationale"><Textarea value={recordRationale} maxLength={4000} rows={4} onChange={event => setRecordRationale(event.target.value)} placeholder="Why this, and why not the alternatives?" /></Field>
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="Kind">
                    <NativeSelect value={recordKind} onChange={event => setRecordKind(event.target.value)}>
                      {KINDS.map(kind => <option key={kind} value={kind}>{kind}</option>)}
                    </NativeSelect>
                  </Field>
                  <Field label="Review date (optional)"><Input type="date" value={recordReviewAt} onChange={event => setRecordReviewAt(event.target.value)} /></Field>
                </div>
                {SENSITIVE_KINDS.has(recordKind) && (
                  <Field label="Owner approval id (required for this kind)" hint="Request one first, have someone else approve it, then paste the id here.">
                    <Input value={recordApprovalId} onChange={event => setRecordApprovalId(event.target.value)} placeholder="Approval UUID" />
                  </Field>
                )}
                <div><Button variant="primary" disabled={recordBusy || !recordTitle.trim() || !recordRationale.trim()} onClick={() => void submitRecord()}>Record decision</Button></div>
              </CardBody>
            </Card>
          </div>

          {selected && (
            <Card>
              <CardHeader
                title={selected.title}
                description={`${selected.kind} · decided ${formatDateTime(selected.decided_at)} · review ${formatDate(selected.review_at)}`}
                icon={<ScrollText />}
              />
              <CardBody className="grid gap-4">
                {detailLoading && <Skeleton className="h-24 w-full" />}
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={statusTone(selected.status)}>{selected.status}</Badge>
                  <Badge tone={reviewTone(reviewState(selected.review_at, today))}>{reviewStateLabel(reviewState(selected.review_at, today))}</Badge>
                  {selected.needs_review && <Badge tone="warning">Rationale flagged for review</Badge>}
                  {selected.hidden_evidence > 0 && <Badge tone="neutral">{selected.hidden_evidence} source{selected.hidden_evidence === 1 ? "" : "s"} hidden</Badge>}
                </div>
                <p className="text-sm leading-relaxed">{selected.rationale}</p>
                {selected.alternatives_considered.length > 0 && (
                  <div>
                    <p className="text-xs font-semibold text-foreground-2">Alternatives considered</p>
                    <ul className="mt-1 list-disc pl-5 text-sm">{selected.alternatives_considered.map(alt => <li key={alt}>{alt}</li>)}</ul>
                  </div>
                )}
                {selected.evidence.length > 0 && (
                  <div>
                    <p className="text-xs font-semibold text-foreground-2">Evidence cited</p>
                    <ul className="mt-1 grid gap-1 text-sm">{selected.evidence.map(item => <li key={`${item.kind}:${item.ref}`}>{item.label} <span className="text-muted">({item.kind})</span></li>)}</ul>
                  </div>
                )}
                {deps && (
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <p className="text-xs font-semibold text-foreground-2">Depends on</p>
                      {deps.depends_on.length ? deps.depends_on.map(item => <button key={item.id} className="mt-1 block text-sm text-primary underline-offset-4 hover:underline" onClick={() => void selectDecision(item)}>{item.title}</button>) : <p className="text-sm text-muted">Nothing.</p>}
                      <p className="mt-3 text-xs font-semibold text-foreground-2">Supersedes</p>
                      {deps.supersedes.length ? deps.supersedes.map(item => <button key={item.id} className="mt-1 block text-sm text-primary underline-offset-4 hover:underline" onClick={() => void selectDecision(item)}>{item.title}</button>) : <p className="text-sm text-muted">Nothing.</p>}
                      {deps.superseded_by && <p className="mt-3 text-sm">Superseded by <button className="text-primary underline-offset-4 hover:underline" onClick={() => void selectDecision(deps.superseded_by!)}>{deps.superseded_by.title}</button></p>}
                    </div>
                    <div>
                      <p className="text-xs font-semibold text-foreground-2">Depended on by</p>
                      {deps.depended_on_by.length ? deps.depended_on_by.map(item => <button key={item.id} className="mt-1 block text-sm text-primary underline-offset-4 hover:underline" onClick={() => void selectDecision(item)}>{item.title}</button>) : <p className="text-sm text-muted">Nothing depends on this decision.</p>}
                      <p className="mt-3 text-xs font-semibold text-foreground-2">Fingerprint</p>
                      <p className="mt-1 break-all font-mono text-xs text-muted">{selected.fingerprint}</p>
                    </div>
                  </div>
                )}
                <div className="grid gap-3 border-t border-border pt-4">
                  <p className="text-xs font-semibold text-foreground-2">Why did we decide this?</p>
                  <div className="flex gap-2">
                    <Input value={whyQuery} maxLength={200} onChange={event => setWhyQuery(event.target.value)} placeholder="Search decisions by subject…" icon={<Search />} />
                    <Button variant="secondary" onClick={() => void runWhy()}>Search</Button>
                  </div>
                  {whyResults !== null && (
                    <div className="grid gap-1">
                      {whyResults.length ? whyResults.map(item => (
                        <button key={item.id} className="text-left text-sm text-primary underline-offset-4 hover:underline" onClick={() => void selectDecision(item)}>{item.title}</button>
                      )) : <p className="text-sm text-muted">No decisions mention that subject.</p>}
                    </div>
                  )}
                </div>
                {selected.status === "active" && (
                  <div className="grid gap-3 border-t border-border pt-4">
                    <p className="text-xs font-semibold text-foreground-2">Review this decision</p>
                    <div className="grid gap-3 sm:grid-cols-2">
                      <Field label="Outcome">
                        <NativeSelect value={outcome} onChange={event => setOutcome(event.target.value as ReviewOutcome)}>
                          <option value="confirm">Confirm — still stands</option>
                          <option value="expire">Expire — no longer applies</option>
                          <option value="supersede">Supersede — replace with a new decision</option>
                          <option value="revoke">Revoke — withdraw it</option>
                        </NativeSelect>
                      </Field>
                      {outcome === "confirm" && (
                        <Field label="Next review date (optional)"><Input type="date" value={nextReviewAt} onChange={event => setNextReviewAt(event.target.value)} /></Field>
                      )}
                    </div>
                    {outcome === "supersede" && (
                      <div className="grid gap-3">
                        <Field label="New title"><Input value={supersedeTitle} maxLength={200} onChange={event => setSupersedeTitle(event.target.value)} /></Field>
                        <Field label="New rationale"><Textarea value={supersedeRationale} maxLength={4000} rows={3} onChange={event => setSupersedeRationale(event.target.value)} /></Field>
                      </div>
                    )}
                    {needsApproval && (
                      <Field label="Owner approval id (required for this kind)" hint="Request an approval for this exact review action first, have someone else approve it, then paste the id here.">
                        <Input value={approvalId} onChange={event => setApprovalId(event.target.value)} placeholder="Approval UUID" />
                      </Field>
                    )}
                    <Field label="Review reason"><Textarea value={reason} maxLength={500} rows={2} onChange={event => setReason(event.target.value)} placeholder="Why this outcome?" /></Field>
                    <div>
                      <Button
                        variant="primary"
                        disabled={reviewBusy || !reason.trim() || (outcome === "supersede" && (!supersedeTitle.trim() || !supersedeRationale.trim()))}
                        onClick={() => void submitReview()}
                      >
                        Submit review
                      </Button>
                    </div>
                  </div>
                )}
              </CardBody>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
