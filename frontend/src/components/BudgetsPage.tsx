import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CalendarRange, Pencil, Plus, RefreshCw, Wallet, XCircle } from "lucide-react";
import { api, describeError } from "../api";
import { formatDay, formatMoney, regionalSettings } from "../format";
import { cn } from "../lib/utils";
import type { Budget, DepartmentGroup, User } from "../types";
import { useFeedback } from "./Feedback";
import { Alert, Badge, Button, Card, CardBody, CardHeader, Dialog, EmptyState, Field, Input, PageHeader, Select, Skeleton, Stat } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

const CATEGORY_LABELS: Record<Budget["category"], string> = { all: "Purchases and expenses", purchase: "Purchase requests", expense: "Expense claims" };

function iso(d: Date) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** First and last day of this month, quarter or year. */
function period(kind: "month" | "quarter" | "year"): [string, string] {
  const now = new Date();
  if (kind === "month") return [iso(new Date(now.getFullYear(), now.getMonth(), 1)), iso(new Date(now.getFullYear(), now.getMonth() + 1, 0))];
  if (kind === "quarter") {
    const q = Math.floor(now.getMonth() / 3) * 3;
    return [iso(new Date(now.getFullYear(), q, 1)), iso(new Date(now.getFullYear(), q + 3, 0))];
  }
  return [iso(new Date(now.getFullYear(), 0, 1)), iso(new Date(now.getFullYear(), 11, 31))];
}

/** Department budgets with spend from approved and in-progress purchase requests and expense claims. */
export function BudgetsPage({ user }: { user: User }) {
  const { toast } = useFeedback();
  const isAdmin = user.role === "admin";
  const [budgets, setBudgets] = useState<Budget[] | null>(null);
  const [departments, setDepartments] = useState<DepartmentGroup[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<{ budget: Budget; amount: string; enforcement: Budget["enforcement"] } | null>(null);
  const [initialStart, initialEnd] = period("quarter");
  const [draft, setDraft] = useState({ name: "", department: "", category: "all" as Budget["category"], start: initialStart, end: initialEnd, amount: "", currency: regionalSettings().currency ?? "", enforcement: "warn" as Budget["enforcement"] });

  const load = useCallback(async () => {
    try {
      setBudgets((await api.budgets()).budgets);
      if (isAdmin) setDepartments((await api.adminGroups()).groups.filter((g) => g.kind === "department" && g.lifecycle_status !== "archived"));
      setError(null);
    } catch (err) { setError(describeError(err)); }
  }, [isAdmin]);
  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      setBudgets((await api.createBudget({ name: draft.name.trim(), department_group_id: draft.department, category: draft.category, period_start: draft.start, period_end: draft.end, amount: draft.amount, currency: draft.currency, enforcement: draft.enforcement })).budgets);
      toast("success", "Budget created", "Approvals in this department now count against it.");
      setDraft({ ...draft, name: "", amount: "" });
    } catch (err) { toast("error", "Could not create the budget", describeError(err)); }
    finally { setBusy(false); }
  }

  async function update(budget: Budget, input: { amount?: string; enforcement?: Budget["enforcement"]; is_active?: boolean }) {
    try {
      setBudgets((await api.updateBudget(budget.id, input)).budgets);
      setEditing(null);
      toast("success", input.is_active === false ? "Budget closed" : "Budget changed");
    } catch (err) { toast("error", "Could not change the budget", describeError(err)); }
  }

  const list = budgets ?? [];
  const currencies = [...new Set(list.map((b) => b.currency))];
  const single = currencies.length === 1 ? currencies[0] : null;
  const sum = (key: "amount" | "approved" | "committed") => list.reduce((total, b) => total + Number(b[key]), 0);
  const over = list.filter((b) => Number(b.approved) > Number(b.amount)).length;
  const ready = draft.name.trim().length >= 2 && draft.department && Number(draft.amount) > 0 && draft.currency.length === 3 && draft.start <= draft.end;

  return (
    <>
      <PageHeader
        title="Budgets & spend"
        description="Department budgets and the spend recorded against them from approved and in-progress purchase requests and expense claims."
        actions={<Button onClick={() => void load()}><RefreshCw /> Refresh</Button>}
      />
      {error && <Alert tone="danger" className="mb-5">{error}</Alert>}
      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat icon={<Wallet />} label="Budgeted" value={budgets === null ? "…" : single ? formatMoney(sum("amount"), single) : list.length ? `${list.length} budgets` : "None yet"} hint={single ? `${list.length} budget${list.length === 1 ? "" : "s"}` : currencies.join(", ") || undefined} />
        <Stat icon={<Wallet />} label="Approved spend" value={budgets === null ? "…" : single ? formatMoney(sum("approved"), single) : "—"} hint={single && sum("amount") ? `${Math.round((sum("approved") / sum("amount")) * 100)}% used` : undefined} />
        <Stat icon={<CalendarRange />} label="Committed (in approval)" value={budgets === null ? "…" : single ? formatMoney(sum("committed"), single) : "—"} />
        <Stat icon={<AlertTriangle />} label="Over budget" value={budgets === null ? "…" : <span className={over ? "text-danger-text" : undefined}>{over}</span>} />
      </div>

      {isAdmin && (
        <Card className="mb-5 overflow-hidden">
          <CardHeader icon={<Plus />} title="New budget" description="Blocking budgets stop a final approval that would exceed them; warning budgets allow it and record the overrun." />
          <CardBody>
            <form className="grid gap-3 md:grid-cols-2 xl:grid-cols-4" onSubmit={(event) => void create(event)}>
              <Field label="Name"><Input maxLength={120} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="e.g. Operations Q4 purchases" /></Field>
              <Field label="Department"><Select aria-label="Budget department" placeholder="Choose a department" value={draft.department} onValueChange={(value) => setDraft({ ...draft, department: value })} options={departments.map((d) => ({ value: d.id, label: d.name }))} /></Field>
              <Field label="Covers"><Select aria-label="Budget covers" value={draft.category} onValueChange={(value) => setDraft({ ...draft, category: value as Budget["category"] })} options={(Object.keys(CATEGORY_LABELS) as Budget["category"][]).map((value) => ({ value, label: CATEGORY_LABELS[value] }))} /></Field>
              <Field label="When over budget"><Select aria-label="When over budget" value={draft.enforcement} onValueChange={(value) => setDraft({ ...draft, enforcement: value as Budget["enforcement"] })} options={[{ value: "warn", label: "Warn and record" }, { value: "block", label: "Block the approval" }]} /></Field>
              <Field label="From"><Input type="date" value={draft.start} onChange={(e) => setDraft({ ...draft, start: e.target.value })} /></Field>
              <Field label="To"><Input type="date" value={draft.end} onChange={(e) => setDraft({ ...draft, end: e.target.value })} /></Field>
              <Field label="Amount"><Input inputMode="decimal" value={draft.amount} onChange={(e) => setDraft({ ...draft, amount: e.target.value.replace(/[^0-9.]/g, "") })} placeholder="500000" /></Field>
              <Field label="Currency"><Input maxLength={3} value={draft.currency} onChange={(e) => setDraft({ ...draft, currency: e.target.value.toUpperCase().replace(/[^A-Z]/g, "") })} placeholder="PKR" /></Field>
              <div className="flex flex-wrap items-center gap-2 md:col-span-2 xl:col-span-4">
                <span className="text-xs text-muted">Period:</span>
                {(["month", "quarter", "year"] as const).map((kind) => (
                  <Button key={kind} size="sm" variant="ghost" onClick={() => { const [start, end] = period(kind); setDraft({ ...draft, start, end }); }}>This {kind}</Button>
                ))}
                <span className="flex-1" />
                <Button type="submit" variant="primary" loading={busy} disabled={busy || !ready}>{!busy && <Plus />} Create budget</Button>
              </div>
            </form>
          </CardBody>
        </Card>
      )}

      {budgets === null ? <div className="grid gap-3 md:grid-cols-2"><Skeleton className="h-36" /><Skeleton className="h-36" /></div> : list.length === 0 ? (
        <Card><EmptyState icon={<Wallet />} title="No budgets yet" description={isAdmin ? "Create a budget for a department above." : "No budgets are set for the departments you manage."} /></Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {list.map((b) => {
            const amount = Number(b.amount), approved = Number(b.approved), committed = Number(b.committed);
            const approvedPct = Math.min(100, (approved / amount) * 100);
            const committedPct = Math.min(100 - approvedPct, (committed / amount) * 100);
            const overrun = approved > amount, tight = !overrun && approved + committed > amount;
            return (
              <Card key={b.id} className="overflow-hidden">
                <CardBody className="grid gap-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-[14px] font-semibold text-foreground">{b.name}</p>
                      <p className="text-xs text-muted">{b.department_name} · {CATEGORY_LABELS[b.category]} · {formatDay(b.period_start)} – {formatDay(b.period_end)}</p>
                    </div>
                    <div className="flex items-center gap-1.5">
                      {overrun ? <Badge tone="danger" dot>Over budget</Badge> : tight ? <Badge tone="warning" dot>Commitments exceed</Badge> : <Badge tone="success" dot>Within budget</Badge>}
                      <Badge>{b.enforcement === "block" ? "Blocks" : "Warns"}</Badge>
                    </div>
                  </div>
                  <div className="h-2.5 overflow-hidden rounded-full bg-surface-2" role="img" aria-label={`${b.name}: ${Math.round((approved / amount) * 100)}% approved, ${Math.round((committed / amount) * 100)}% committed`}>
                    <div className="flex h-full">
                      <div className={cn("h-full", overrun ? "bg-danger" : "bg-primary")} style={{ width: `${approvedPct}%` }} />
                      <div className="h-full bg-warning/70" style={{ width: `${committedPct}%` }} />
                    </div>
                  </div>
                  <dl className="grid grid-cols-3 gap-2 text-xs">
                    <div><dt className="text-muted">Approved</dt><dd className="font-semibold text-foreground tabular-nums">{formatMoney(approved, b.currency)}</dd></div>
                    <div><dt className="text-muted">Committed</dt><dd className="font-semibold text-foreground tabular-nums">{formatMoney(committed, b.currency)}</dd></div>
                    <div><dt className="text-muted">Remaining</dt><dd className={cn("font-semibold tabular-nums", Number(b.remaining) < 0 ? "text-danger-text" : "text-foreground")}>{formatMoney(Number(b.remaining), b.currency)}</dd></div>
                  </dl>
                  <p className="text-xs text-muted">Budget {formatMoney(amount, b.currency)}</p>
                  {isAdmin && (
                    <div className="flex gap-1 border-t border-border pt-2">
                      <Button size="sm" variant="ghost" onClick={() => setEditing({ budget: b, amount: String(amount), enforcement: b.enforcement })}><Pencil /> Change</Button>
                      <Button size="sm" variant="danger-ghost" onClick={() => void update(b, { is_active: false })}><XCircle /> Close</Button>
                    </div>
                  )}
                </CardBody>
              </Card>
            );
          })}
        </div>
      )}

      <Dialog
        open={editing !== null}
        onOpenChange={(open) => { if (!open) setEditing(null); }}
        title={editing ? `Change ${editing.budget.name}` : "Change budget"}
        footer={<><Button onClick={() => setEditing(null)}>Cancel</Button><Button variant="primary" disabled={!editing || !(Number(editing.amount) > 0)} onClick={() => editing && void update(editing.budget, { amount: editing.amount, enforcement: editing.enforcement })}>Save</Button></>}
      >
        {editing && <div className="grid gap-3">
          <Field label={`Amount (${editing.budget.currency})`}><Input inputMode="decimal" value={editing.amount} onChange={(e) => setEditing({ ...editing, amount: e.target.value.replace(/[^0-9.]/g, "") })} /></Field>
          <Field label="When over budget"><Select aria-label="When over budget (edit)" value={editing.enforcement} onValueChange={(value) => setEditing({ ...editing, enforcement: value as Budget["enforcement"] })} options={[{ value: "warn", label: "Warn and record" }, { value: "block", label: "Block the approval" }]} /></Field>
        </div>}
      </Dialog>
    </>
  );
}
