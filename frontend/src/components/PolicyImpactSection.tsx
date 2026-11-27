import { CheckCircle2, FileText, GitBranch, RefreshCw } from "lucide-react";
import { Button, Card, CardHeader, EmptyState, SelectInput } from "./ui";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import { reassignableAssignees } from "../policyImpact.mjs";
import type { PolicyCandidate, PolicyException, PolicyProcess, PolicyProcessAssignee } from "../types";
import { useFeedback } from "./Feedback";
import { useLiveRefresh } from "../lib/useLiveRefresh";

export function PolicyImpactSection() {
  const { toast } = useFeedback();
  const [candidates, setCandidates] = useState<PolicyCandidate[]>([]);
  const [processes, setProcesses] = useState<PolicyProcess[]>([]);
  const [processCursor, setProcessCursor] = useState<string | null>(null);
  const [processPageLoading, setProcessPageLoading] = useState(false);
  const [exceptions, setExceptions] = useState<PolicyException[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [createPolicy, setCreatePolicy] = useState("");
  const [linkChoice, setLinkChoice] = useState<Record<string, string>>({});
  const [assignees, setAssignees] = useState<Record<string, PolicyProcessAssignee[]>>({});
  const [selectedAssignee, setSelectedAssignee] = useState<Record<string, string>>({});
  const [rationales, setRationales] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [candidateResult, processResult, exceptionResult] = await Promise.all([
        api.policyProcessCandidates(), api.policyProcesses(), api.policyExceptions(),
      ]);
      setCandidates(candidateResult.items);
      setProcesses(processResult.items);
      setProcessCursor(processResult.next_cursor);
      setExceptions(exceptionResult.items);
      setError(null);
    } catch (err) { setError(describeError(err)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);

  async function loadMoreProcesses() {
    if (!processCursor || processPageLoading) return;
    setProcessPageLoading(true);
    try {
      const result = await api.policyProcesses(processCursor);
      setProcesses((current) => [...current, ...result.items]);
      setProcessCursor(result.next_cursor);
      setError(null);
    } catch (err) { setError(describeError(err)); }
    finally { setProcessPageLoading(false); }
  }

  async function createProcess(event: FormEvent) {
    event.preventDefault();
    if (!name.trim() || !createPolicy) return;
    setBusy("create");
    try {
      await api.createPolicyProcess({ name: name.trim(), description: description.trim(), policy_entry_id: createPolicy });
      setName(""); setDescription(""); setCreatePolicy("");
      toast("success", "Process impact map created", "The process is linked to the selected published policy and its current sources.");
      await load();
    } catch (err) { toast("error", "Process could not be created", describeError(err)); }
    finally { setBusy(null); }
  }

  async function loadAssignees(process: PolicyProcess, entryId: string) {
    const key = `${process.id}:${entryId}`;
    try {
      const result = await api.policyProcessAssignees(process.id, entryId);
      setAssignees((current) => ({ ...current, [key]: result.items }));
      if (result.items.length && !selectedAssignee[key]) {
        setSelectedAssignee((current) => ({ ...current, [key]: result.items[0].user_id }));
      }
    } catch (err) { toast("error", "Assignees could not be loaded", describeError(err)); }
  }

  async function linkPolicy(process: PolicyProcess) {
    const entryId = linkChoice[process.id];
    if (!entryId) return;
    setBusy(process.id);
    try {
      await api.linkPolicyToProcess(process.id, entryId);
      toast("success", "Policy linked", "Future submissions for this policy will appear in the process impact view.");
      setLinkChoice((current) => ({ ...current, [process.id]: "" }));
      await load();
    } catch (err) { toast("error", "Policy could not be linked", describeError(err)); }
    finally { setBusy(null); }
  }

  async function unlinkPolicy(processId: string, entryId: string) {
    setBusy(`${processId}:${entryId}`);
    try {
      await api.unlinkPolicyFromProcess(processId, entryId);
      toast("success", "Policy relationship deactivated", "Existing exception history is retained.");
      await load();
    } catch (err) { toast("error", "Policy could not be unlinked", describeError(err)); }
    finally { setBusy(null); }
  }

  async function assignException(process: PolicyProcess, entryId: string) {
    const key = `${process.id}:${entryId}`;
    const rationale = rationales[key]?.trim() ?? "";
    if (!selectedAssignee[key] || rationale.length < 3) return;
    setBusy(key);
    try {
      await api.createPolicyException(process.id, { entry_id: entryId, assigned_to: selectedAssignee[key], rationale });
      toast("success", "Policy action assigned", "The assignee can acknowledge and resolve it; the assignment history is retained.");
      setRationales((current) => ({ ...current, [key]: "" }));
      await load();
    } catch (err) { toast("error", "Policy action could not be assigned", describeError(err)); }
    finally { setBusy(null); }
  }

  async function exceptionAction(item: PolicyException, action: "acknowledge" | "resolve" | "reassign") {
    const rationale = rationales[item.id]?.trim() ?? "";
    if (rationale.length < 3) return toast("error", "Add a rationale", "Record why this exception changed status or owner.");
    const nextAssignee = selectedAssignee[item.id];
    if (action === "reassign" && !nextAssignee) return;
    setBusy(item.id);
    try {
      await api.actOnPolicyException(item.id, {
        action, rationale, ...(action === "reassign" ? { assigned_to: nextAssignee } : {}),
      });
      toast("success", action === "reassign" ? "Policy action reassigned" : `Policy action ${action}d`, "The change was recorded in its append-only history.");
      setRationales((current) => ({ ...current, [item.id]: "" }));
      await load();
    } catch (err) { toast("error", "Policy action could not be updated", describeError(err)); }
    finally { setBusy(null); }
  }

  async function loadReassignableAssignees(item: PolicyException) {
    setBusy(item.id);
    try {
      const result = await api.policyProcessAssignees(item.process_id, item.entry_id);
      const options = reassignableAssignees(result.items, item.assigned_to.user_id);
      setAssignees((current) => ({ ...current, [item.id]: options }));
      setSelectedAssignee((current) => ({ ...current, [item.id]: options[0]?.user_id ?? "" }));
    } catch (err) { toast("error", "Assignees could not be loaded", describeError(err)); }
    finally { setBusy(null); }
  }

  if (loading) return <Card className="mt-8"><CardHeader title="Policy impact and exceptions" /><div className="loading">Loading policy impact…</div></Card>;
  return <section className="mt-8">
    <Card className="mb-4">
      <CardHeader
        icon={<GitBranch />}
        title="Policy impact and exceptions"
        description="Link a published policy to the business processes it affects, so that when the policy changes the right owners are told what to update. Exceptions assigned to you are listed below."
        actions={<Button size="sm" onClick={() => void load()}><RefreshCw /> Refresh</Button>}
      />
    </Card>
    {error && <div className="banner banner-error">{error}</div>}
    {candidates.length > 0 && <form className="card form-grid" onSubmit={(event) => void createProcess(event)}>
      <h3>Create process impact map</h3>
      <label className="field"><span className="field-label">Process name</span><input value={name} onChange={(event) => setName(event.target.value)} maxLength={180} required placeholder="e.g. Invoice approval" /></label>
      <label className="field"><span className="field-label">Published policy</span><SelectInput value={createPolicy} onChange={(event) => setCreatePolicy(event.target.value)} required><option value="">Select a policy you can review</option>{candidates.map((item) => <option value={item.entry_id} key={item.entry_id}>{item.category} · {item.statement.slice(0, 110)}</option>)}</SelectInput></label>
      <label className="field"><span className="field-label">Process description</span><textarea rows={2} value={description} onChange={(event) => setDescription(event.target.value)} maxLength={2000} placeholder="Describe the operational area affected by this policy." /></label>
      <button className="btn btn-primary" disabled={busy === "create" || !createPolicy || !name.trim()}>{busy === "create" ? "Creating…" : "Create impact map"}</button>
    </form>}
    {processes.length > 0 ? <div className="approval-list">{processes.map((process) => {
      const linked = new Set(process.policies.map((policy) => policy.entry_id));
      const compatible = candidates.filter((candidate) => !linked.has(candidate.entry_id) &&
        (process.scope_group_id === null ? candidate.scope_group_id === null : candidate.scope_group_id === null || candidate.scope_group_id === process.scope_group_id));
      return <article className="card approval-card" key={process.id}>
        <div className="approval-meta"><span className="chip chip-sm">{process.scope_group_id ? "Department process" : "Shared process"}</span><span className="muted">Owner {process.owner_email} · created {formatDate(process.created_at)}</span></div>
        <h3>{process.name}</h3>{process.description && <p className="muted">{process.description}</p>}
        {process.policies.map((policy) => <section className="conflict-side" key={policy.entry_id}>
          <div className="approval-meta"><span className="chip chip-sm">{policy.category}</span><span className="muted">Published revision {policy.published_revision_number}</span></div>
          <p className="approval-statement">{policy.published_statement}</p>
          <ul className="knowledge-evidence">{policy.published_sources.map((source, index) => <li key={`${source.filename}-${index}`}><span className="chip chip-sm">{source.filename} · page {source.page_number}</span><p className="knowledge-quote">{source.quote}</p></li>)}</ul>
          {policy.pending_statement && <div className="banner banner-warning"><strong>Pending change · revision {policy.pending_revision_number}</strong><p>{policy.pending_statement}</p>{policy.pending_sources.map((source, index) => <p className="muted small" key={`${source.filename}-${index}`}>{source.filename} · page {source.page_number}: {source.quote}</p>)}</div>}
          {policy.can_manage && <>
            <div className="approval-actions"><button className="btn" disabled={busy === `${process.id}:${policy.entry_id}`} onClick={() => void unlinkPolicy(process.id, policy.entry_id)}>Unlink policy</button><button className="btn" onClick={() => void loadAssignees(process, policy.entry_id)}>Load in-scope assignees</button></div>
            {assignees[`${process.id}:${policy.entry_id}`] && <div className="card form-grid">
              <label className="field"><span className="field-label">Assign an implementation exception</span><SelectInput value={selectedAssignee[`${process.id}:${policy.entry_id}`] ?? ""} onChange={(event) => setSelectedAssignee((current) => ({ ...current, [`${process.id}:${policy.entry_id}`]: event.target.value }))}><option value="">Select active assignee</option>{assignees[`${process.id}:${policy.entry_id}`].map((person) => <option key={person.user_id} value={person.user_id}>{person.email} · {person.role}</option>)}</SelectInput></label>
              <label className="field"><span className="field-label">Assignment rationale</span><textarea rows={2} maxLength={2000} value={rationales[`${process.id}:${policy.entry_id}`] ?? ""} onChange={(event) => setRationales((current) => ({ ...current, [`${process.id}:${policy.entry_id}`]: event.target.value }))} placeholder="Describe the concrete exception or remediation required." /></label>
              <button className="btn btn-primary" disabled={busy === `${process.id}:${policy.entry_id}` || (rationales[`${process.id}:${policy.entry_id}`]?.trim().length ?? 0) < 3 || !selectedAssignee[`${process.id}:${policy.entry_id}`]} onClick={() => void assignException(process, policy.entry_id)}>Assign exception</button>
            </div>}
          </>}
        </section>)}
        {compatible.length > 0 && process.policies.some((policy) => policy.can_manage) && <div className="approval-actions"><SelectInput aria-label={`Link another policy to ${process.name}`} value={linkChoice[process.id] ?? ""} onChange={(event) => setLinkChoice((current) => ({ ...current, [process.id]: event.target.value }))}><option value="">Link another readable policy</option>{compatible.map((item) => <option value={item.entry_id} key={item.entry_id}>{item.category} · {item.statement.slice(0, 110)}</option>)}</SelectInput><button className="btn" disabled={busy === process.id || !linkChoice[process.id]} onClick={() => void linkPolicy(process)}>Link policy</button></div>}
      </article>;
    })}</div> : candidates.length === 0 ? <Card><EmptyState icon={<FileText />} title="No published policies yet" description="Once a policy statement is approved and published in Approvals, you can map it to the processes it affects here." /></Card> : <p className="muted">No process impact maps yet. Create one from a published policy you can review.</p>}
    {processCursor && <button className="btn" type="button" disabled={processPageLoading} onClick={() => void loadMoreProcesses()}>{processPageLoading ? "Loading process maps…" : "Load more process maps"}</button>}
    <h3 className="mt-6 mb-2 text-sm font-semibold text-foreground">Policy actions assigned to you</h3>
    {exceptions.length === 0 ? <Card><EmptyState icon={<CheckCircle2 />} title="No policy actions assigned to you" description="If a policy change needs work in a process you own, it will appear here with a due date." /></Card> : <div className="approval-list">{exceptions.map((item) => <article className="card approval-card" key={item.id}>
      <div className="approval-meta"><span className="chip chip-sm">{item.status}</span><span className="muted">{item.process_name} · due {item.due_at ? formatDate(item.due_at) : "not set"}</span></div>
      <p className="approval-statement">{item.policy_statement}</p><p>{item.rationale}</p><p className="muted small">Assigned to {item.assigned_to.email}; created by {item.created_by_email}</p>
      <ul className="approval-decisions">{item.events.map((event, index) => <li key={`${event.action}-${index}`}><strong>{event.action}</strong> by {event.actor_email} · {event.from_status ?? "new"} → {event.to_status}; assigned to {event.to_assignee_email} · {event.rationale}</li>)}</ul>
      {(item.can_acknowledge || item.can_resolve || item.can_reassign) && <>
        {item.can_reassign && <button className="btn" disabled={busy === item.id} onClick={() => void loadReassignableAssignees(item)}>{busy === item.id ? "Loading assignees…" : "Load assignees for reassignment"}</button>}
        {assignees[item.id] && item.can_reassign && (assignees[item.id].length > 0 ? <label className="field"><span className="field-label">New assignee</span><SelectInput value={selectedAssignee[item.id] ?? ""} onChange={(event) => setSelectedAssignee((current) => ({ ...current, [item.id]: event.target.value }))}><option value="">Select a different in-scope assignee</option>{assignees[item.id].map((person) => <option key={person.user_id} value={person.user_id}>{person.email}</option>)}</SelectInput></label> : <p className="muted">No other active in-scope assignee is available.</p>)}
        <label className="field"><span className="field-label">Action rationale</span><textarea rows={2} maxLength={2000} value={rationales[item.id] ?? ""} onChange={(event) => setRationales((current) => ({ ...current, [item.id]: event.target.value }))} placeholder="Explain acknowledgement, resolution or reassignment." /></label>
        <div className="approval-actions">{item.can_acknowledge && <button className="btn" disabled={busy === item.id || (rationales[item.id]?.trim().length ?? 0) < 3} onClick={() => void exceptionAction(item, "acknowledge")}>Acknowledge</button>}{item.can_resolve && <button className="btn btn-primary" disabled={busy === item.id || (rationales[item.id]?.trim().length ?? 0) < 3} onClick={() => void exceptionAction(item, "resolve")}>Resolve action</button>}{item.can_reassign && assignees[item.id]?.length > 0 && <button className="btn" disabled={busy === item.id || (rationales[item.id]?.trim().length ?? 0) < 3 || !selectedAssignee[item.id]} onClick={() => void exceptionAction(item, "reassign")}>Reassign</button>}</div>
      </>}
    </article>)}</div>}
  </section>;
}
