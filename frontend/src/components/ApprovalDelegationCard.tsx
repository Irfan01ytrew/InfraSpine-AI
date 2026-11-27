import { useCallback, useEffect, useState } from "react";
import { UserRoundCheck, X } from "lucide-react";
import { api, describeError } from "../api";
import { formatDay } from "../format";
import type { ApprovalDelegation, OrgChartPerson, User } from "../types";
import { useFeedback } from "./Feedback";
import { Badge, Button, Card, CardBody, CardHeader, Field, Input, Select } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

function iso(d: Date) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Hand your approval-step decisions to a colleague while you are away (administrators: for anyone). */
export function ApprovalDelegationCard({ user, onChanged }: { user: User; onChanged?: () => void }) {
  const { toast } = useFeedback();
  const isAdmin = user.role === "admin";
  const [delegations, setDelegations] = useState<ApprovalDelegation[]>([]);
  const [people, setPeople] = useState<OrgChartPerson[]>([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const today = iso(new Date());
  const [draft, setDraft] = useState({ delegator: "", delegate: "", start: today, end: iso(new Date(Date.now() + 6 * 86400000)), reason: "" });

  const load = useCallback(async () => {
    try { setDelegations((await api.approvalDelegations()).delegations); } catch { /* the card stays empty */ }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);
  useEffect(() => {
    if (open && people.length === 0) api.orgChart().then((r) => setPeople(r.people)).catch(() => undefined);
  }, [open, people.length]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      setDelegations((await api.createApprovalDelegation({
        delegate_user_id: draft.delegate, starts_on: draft.start, ends_on: draft.end, reason: draft.reason.trim() || null,
        ...(isAdmin && draft.delegator ? { delegator_user_id: draft.delegator } : {}),
      })).delegations);
      toast("success", "Delegation set", "Your colleague can decide your approval steps for these dates.");
      setOpen(false);
      setDraft({ ...draft, delegate: "", reason: "" });
      onChanged?.();
    } catch (err) { toast("error", "Could not set the delegation", describeError(err)); }
    finally { setBusy(false); }
  }

  async function end(delegation: ApprovalDelegation) {
    try {
      setDelegations((await api.endApprovalDelegation(delegation.id)).delegations);
      toast("success", "Delegation ended");
      onChanged?.();
    } catch (err) { toast("error", "Could not end the delegation", describeError(err)); }
  }

  const delegator = isAdmin && draft.delegator ? draft.delegator : user.id;
  const choices = people.filter((p) => p.user_id !== delegator).map((p) => ({ value: p.user_id, label: p.position_title ? `${p.email} · ${p.position_title}` : p.email }));

  return (
    <Card className="overflow-hidden">
      <CardHeader
        icon={<UserRoundCheck />}
        title="Approval delegation"
        description="Away? A colleague decides your approval steps with your approval authority; every decision records both of you."
        actions={!open ? <Button size="sm" onClick={() => setOpen(true)}>Delegate</Button> : undefined}
      />
      <CardBody className="grid gap-3">
        {delegations.length === 0 && !open && <p className="text-xs text-muted">No delegation is set.</p>}
        {delegations.length > 0 && (
          <ul className="grid gap-2" aria-label="Approval delegations">
            {delegations.map((d) => {
              const mine = d.delegator_user_id === user.id, forMe = d.delegate_user_id === user.id;
              return (
                <li key={d.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border px-3 py-2 text-[13px]">
                  <span className="grid min-w-0 gap-0.5">
                    <span className="truncate font-medium text-foreground">
                      {mine ? `You → ${d.delegate_email}` : forMe ? `${d.delegator_email} → you` : `${d.delegator_email} → ${d.delegate_email}`}
                    </span>
                    <span className="text-xs text-muted">{formatDay(d.starts_on)} – {formatDay(d.ends_on)}{d.reason ? ` · ${d.reason}` : ""}</span>
                  </span>
                  <span className="flex items-center gap-1.5">
                    {d.current ? <Badge tone="success" dot>Active</Badge> : <Badge>Upcoming</Badge>}
                    <Button size="sm" variant="ghost" aria-label={`End delegation ${d.delegator_email} to ${d.delegate_email}`} onClick={() => void end(d)}><X /> End</Button>
                  </span>
                </li>
              );
            })}
          </ul>
        )}
        {open && (
          <form className="grid gap-3 border-t border-border pt-3" onSubmit={(event) => void save(event)}>
            {isAdmin && (
              <Field label="Delegating approver">
                <Select aria-label="Delegating approver" placeholder="Me" value={draft.delegator} onValueChange={(value) => setDraft({ ...draft, delegator: value, delegate: value === draft.delegate ? "" : draft.delegate })}
                  options={people.map((p) => ({ value: p.user_id, label: p.email }))} />
              </Field>
            )}
            <Field label="Who decides instead">
              <Select aria-label="Delegate" placeholder="Choose a colleague" value={draft.delegate} onValueChange={(value) => setDraft({ ...draft, delegate: value })} options={choices} />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="From"><Input type="date" min={today} value={draft.start} onChange={(e) => setDraft({ ...draft, start: e.target.value })} /></Field>
              <Field label="To"><Input type="date" min={draft.start} value={draft.end} onChange={(e) => setDraft({ ...draft, end: e.target.value })} /></Field>
            </div>
            <Field label="Reason (optional)"><Input maxLength={200} value={draft.reason} onChange={(e) => setDraft({ ...draft, reason: e.target.value })} placeholder="e.g. Annual leave" /></Field>
            <div className="flex justify-end gap-2">
              <Button onClick={() => setOpen(false)}>Cancel</Button>
              <Button type="submit" variant="primary" loading={busy} disabled={busy || !draft.delegate || !draft.start || draft.end < draft.start}>Set delegation</Button>
            </div>
          </form>
        )}
      </CardBody>
    </Card>
  );
}
