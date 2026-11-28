import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import type { FormEvent } from "react";
import { BadgeCheck, FileUp, Play, RefreshCw, ShieldCheck, Wallet } from "lucide-react";
import { describeError } from "../api";
import { useFeedback } from "../components/Feedback";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, Input, PageHeader, SelectInput, Skeleton, Stat, Table, Tabs, Textarea } from "../components/ui";
import { financeApi } from "./api";
import type { Batch, BatchSummary, FinanceScope, Invoice, KnowledgeDocument, Payment, PaymentApproval, Spend } from "./api";
import { actionableStep, headline, reasonCounts, shortHash, skippedSummary, spendMonthLabel, spendSeries } from "./financeView.mjs";
import { formatMoneyForLanguage } from "../format";
import { getLanguage, subscribeLanguage } from "../i18n.mjs";

type Tab = "batches" | "exceptions" | "approvals" | "spend";
type Tone = "neutral" | "primary" | "success" | "warning" | "danger";

const STATUS_TONE: Record<string, Tone> = {
  verified: "success", approved: "primary", paid: "success", exception: "danger", rejected: "neutral", pending: "warning",
  completed: "success", failed: "danger", running: "primary", paused: "warning", action: "warning", waiting: "warning",
  awaiting_approval: "warning", dispatched: "primary", verifying: "primary", paying: "primary", open: "neutral",
};

function StatusBadge({ status }: { status: string }) {
  return <Badge tone={STATUS_TONE[status] ?? "neutral"} dot>{status.replace(/_/g, " ")}</Badge>;
}

export function FinancePage() {
  const { toast } = useFeedback();
  const [scopes, setScopes] = useState<FinanceScope[] | null>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [connector, setConnector] = useState("");
  const [group, setGroup] = useState("");
  const [tab, setTab] = useState<Tab>("batches");
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    financeApi.scopes().then((result) => {
      setScopes(result.scopes); setReasons(result.reasons); setConnector(result.connector);
      setGroup((current) => current || result.scopes[0]?.id || "");
    }).catch((err) => setError(describeError(err)));
  }, []);
  const scope = scopes?.find((item) => item.id === group);
  const can = useCallback((permission: string) => !!scope?.permissions.includes(permission), [scope]);
  const refresh = () => setRevision((value) => value + 1);

  return <div className="grid gap-5 pb-8">
    <PageHeader eyebrow="FINANCE · INVOICES" title="Invoice verification & payments"
      description="Verify invoices against purchase orders, goods receipts, the vendor master and duplicates. Payments need an approval of the exact amount, payee and invoices, and are only marked paid after the bank confirms them."
      actions={<Button variant="secondary" onClick={refresh}><RefreshCw /> Refresh</Button>} />
    {error && <Alert tone="danger" title="Finance data could not be loaded">{error}</Alert>}
    {scopes === null && !error && <Skeleton className="h-40 w-full" />}
    {scopes && scopes.length === 0 && <Card><EmptyState icon={<Wallet />} title="No finance access yet"
      description="An administrator grants finance roles per department: clerk, verifier, approver, treasury, viewer." /></Card>}
    {scopes && scopes.length > 0 && <>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Department" className="min-w-64">
          <SelectInput value={group} onChange={(event) => setGroup(event.target.value)} aria-label="Department">
            {scopes.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </SelectInput>
        </Field>
        <div className="flex flex-wrap gap-1.5 pb-1">{scope?.permissions.map((p) => <Badge key={p} tone="primary">{p}</Badge>)}</div>
        <div className="ml-auto pb-1 text-xs text-muted">Payment connector: <b>{connector}</b>{connector.includes("mock") && " (test bank — no real money moves)"}</div>
      </div>
      <Tabs value={tab} onValueChange={(value) => setTab(value as Tab)} items={[
        { value: "batches", label: "Batches" }, { value: "exceptions", label: "Exceptions queue" },
        { value: "approvals", label: "Payment approvals" }, { value: "spend", label: "Spend chart" },
      ]} />
      {tab === "batches" && <BatchesTab group={group} can={can} reasons={reasons} revision={revision} toast={toast} />}
      {tab === "exceptions" && <ExceptionsTab group={group} can={can} revision={revision} toast={toast} />}
      {tab === "approvals" && <ApprovalsTab revision={revision} toast={toast} />}
      {tab === "spend" && <SpendTab group={group} revision={revision} />}
    </>}
  </div>;
}

type ToastFn = ReturnType<typeof useFeedback>["toast"];

function BatchesTab({ group, can, reasons, revision, toast }: { group: string; can: (p: string) => boolean; reasons: Record<string, string>; revision: number; toast: ToastFn }) {
  const [batches, setBatches] = useState<(Batch & { summary: BatchSummary })[] | null>(null);
  const [selected, setSelected] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [local, setLocal] = useState(0);
  useEffect(() => {
    let live = true;
    financeApi.batches(group).then((result) => { if (live) { setBatches(result.batches); setSelected((cur) => cur || result.batches[0]?.id || ""); } })
      .catch((err) => toast("error", "Batches could not be loaded", describeError(err)));
    return () => { live = false; };
  }, [group, revision, local, toast]);
  async function create(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try { const batch = await financeApi.createBatch(group, name.trim(), "PKR"); setName(""); setSelected(batch.id); setLocal((v) => v + 1); }
    catch (err) { toast("error", "Batch could not be created", describeError(err)); }
    finally { setBusy(false); }
  }
  return <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
    <Card>
      <CardHeader title="Batches" description="Newest first" />
      <CardBody className="grid gap-2">
        {can("invoice.ingest") && <form onSubmit={create} className="flex gap-2">
          <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="New batch name" aria-label="New batch name" />
          <Button type="submit" disabled={busy || !name.trim()}>Create</Button>
        </form>}
        {batches === null && <Skeleton className="h-24 w-full" />}
        {batches?.length === 0 && <p className="text-sm text-muted">No batches yet.</p>}
        {batches?.map((batch) => <button key={batch.id} type="button" onClick={() => setSelected(batch.id)}
          className={`grid gap-1 rounded-lg border p-3 text-left transition-colors ${selected === batch.id ? "border-primary bg-primary-soft/40" : "border-border hover:bg-surface-2"}`}>
          <span className="flex items-center justify-between gap-2 text-sm font-medium">{batch.name}<StatusBadge status={batch.status} /></span>
          <span className="text-xs text-muted">{headline(batch.summary)} · round {batch.round}</span>
        </button>)}
      </CardBody>
    </Card>
    {selected ? <BatchDetail id={selected} group={group} can={can} reasons={reasons} revision={revision + local} toast={toast} onChanged={() => setLocal((v) => v + 1)} />
      : <Card><EmptyState icon={<Wallet />} title="Select or create a batch" /></Card>}
  </div>;
}

function BatchDetail({ id, group, can, reasons, revision, toast, onChanged }: {
  id: string; group: string; can: (p: string) => boolean; reasons: Record<string, string>; revision: number; toast: ToastFn; onChanged: () => void;
}) {
  const [data, setData] = useState<{ batch: Batch; summary: BatchSummary; invoices: Invoice[]; payments: Payment[] } | null>(null);
  const [texts, setTexts] = useState("");
  const [knowledge, setKnowledge] = useState<{ documents: KnowledgeDocument[]; folders: string[] } | null>(null);
  const [knowledgeQuery, setKnowledgeQuery] = useState("");
  const [selectedDocs, setSelectedDocs] = useState<string[]>([]);
  const [filter, setFilter] = useState("all");
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => financeApi.batch(id).then(setData).catch((err) => toast("error", "Batch could not be loaded", describeError(err))), [id, toast]);
  useEffect(() => { void load(); }, [load, revision]);
  const permissions = useMemo(() => ["invoice.verify", "funds.transfer"].filter(can), [can]);
  if (!data) return <Skeleton className="h-72 w-full" />;
  const { batch, summary, invoices, payments } = data;
  const step = actionableStep(summary.run, permissions);
  const shown = filter === "all" ? invoices : filter === "remaining" ? invoices.filter((i) => summary.remaining_ids.includes(i.id)) : invoices.filter((i) => i.status === filter);
  async function act(label: string, action: () => Promise<unknown>) {
    setBusy(true);
    try { await action(); toast("success", label); await load(); onChanged(); }
    catch (err) { toast("error", `${label} failed`, describeError(err)); }
    finally { setBusy(false); }
  }
  async function addTexts() {
    const parts = texts.split(/\n-{3,}\n/).map((part) => part.trim()).filter(Boolean);
    if (parts.length) await act("Invoices added", () => financeApi.addInvoices(id, [], parts));
    setTexts("");
  }
  async function loadKnowledge() {
    setBusy(true);
    try { setKnowledge(await financeApi.knowledgeDocuments(undefined, knowledgeQuery.trim() || undefined)); }
    catch (err) { toast("error", "Knowledge documents could not be loaded", describeError(err)); }
    finally { setBusy(false); }
  }
  async function addFromKnowledge() {
    if (!selectedDocs.length) return;
    setBusy(true);
    try {
      const result = await financeApi.addInvoicesFromDocuments(id, selectedDocs);
      const skipped = skippedSummary(result.skipped);
      if (skipped) toast("info", "Invoices added from Knowledge", skipped);
      else toast("success", "Invoices added from Knowledge");
      setSelectedDocs([]);
      await load();
      onChanged();
    } catch (err) { toast("error", "Invoices could not be added from Knowledge", describeError(err)); }
    finally { setBusy(false); }
  }
  return <div className="grid gap-4">
    <div className="grid gap-3 sm:grid-cols-4">
      <Stat label="Result" value={summary.headline} icon={<BadgeCheck />} />
      <Stat label="Remaining (by id)" value={summary.remaining_ids.length} hint="exceptions and pending" />
      <Stat label="Workflow" value={summary.run ? `${summary.run.status} · ${summary.run.phase}` : "not started"} hint={summary.run?.blocked_reason ?? undefined} />
      <Stat label="Payments" value={`${payments.filter((p) => p.status === "verified").length} of ${payments.length} paid`} icon={<ShieldCheck />} />
    </div>
    <Card>
      <CardHeader title={batch.name} description={`Tolerance ${batch.tolerance_pct}% · round ${batch.round} · ${batch.currency}`} actions={<div className="flex flex-wrap gap-2">
        {can("invoice.ingest") && <Button disabled={busy} onClick={() => act("Verification started", () => financeApi.start(id))}><Play /> Start verification</Button>}
        {step && summary.run && <Button variant="primary" disabled={busy} onClick={() => act(step.node_id === "pay" ? "Transfer started" : "Verification taken", () => financeApi.perform(group, summary.run!.id, step.id))}>
          {step.node_id === "pay" ? "Transfer approved payments" : "Verify invoices (as me)"}</Button>}
      </div>} />
      <CardBody className="grid gap-3">
        {summary.run && <div className="flex flex-wrap items-center gap-2 text-xs">{summary.run.steps.map((s) =>
          <span key={s.id} className="flex items-center gap-1 rounded-md border border-border px-2 py-1">{s.node_id}<StatusBadge status={s.status} />{s.verified_at && <BadgeCheck className="size-3 text-success" aria-label="verified" />}</span>)}</div>}
        {can("invoice.ingest") && <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
          <Textarea value={texts} onChange={(event) => setTexts(event.target.value)} rows={3}
            placeholder={"Paste invoice text (Invoice No: …, Vendor: …, PO Number: …, Total: PKR …). Separate invoices with a line of ---"} />
          <div className="grid gap-2">
            <Button variant="secondary" disabled={busy || !texts.trim()} onClick={addTexts}>Add text</Button>
            <label className="inline-flex cursor-pointer items-center justify-center gap-2 rounded-md border border-border px-3 py-1.5 text-xs font-medium hover:bg-surface-2">
              <FileUp className="size-4" /> Upload PDF/DOC
              <input type="file" className="hidden" accept=".pdf,.docx,.doc,.txt,.md,.png,.jpg,.jpeg"
                onChange={(event) => { const file = event.target.files?.[0]; if (file) void act("Invoice uploaded", () => financeApi.upload(id, file)); event.target.value = ""; }} />
            </label>
          </div>
        </div>}
        {can("invoice.ingest") && <div className="grid gap-2 rounded-lg border border-border p-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">From Knowledge documents</span>
            <span className="text-xs text-muted">Only documents you can read. Their text fills invoice fields only — it never chooses the payee.</span>
            <Button variant="secondary" disabled={busy} onClick={loadKnowledge}>Browse documents</Button>
          </div>
          {knowledge && <>
            <div className="flex gap-2">
              <Input value={knowledgeQuery} onChange={(event) => setKnowledgeQuery(event.target.value)} placeholder="Search by filename" aria-label="Search Knowledge documents" />
              <Button variant="secondary" disabled={busy} onClick={loadKnowledge}>Search</Button>
            </div>
            {knowledge.documents.length === 0
              ? <p className="text-sm text-muted">No readable documents found.</p>
              : <div className="grid max-h-48 gap-1 overflow-auto">{knowledge.documents.map((doc) => <label key={doc.id} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={selectedDocs.includes(doc.id)} disabled={busy || !doc.indexed}
                  onChange={(event) => setSelectedDocs((cur) => event.target.checked ? [...cur, doc.id] : cur.filter((item) => item !== doc.id))} />
                <span>{doc.filename}</span>
                {!doc.indexed && <Badge tone="warning">not indexed yet</Badge>}
                {doc.folder && <span className="text-xs text-muted">{doc.folder}</span>}
              </label>)}</div>}
            <div><Button variant="secondary" disabled={busy || selectedDocs.length === 0} onClick={addFromKnowledge}>Add selected invoices</Button></div>
          </>}
        </div>}
        <div className="flex flex-wrap items-center gap-2">
          <Tabs value={filter} onValueChange={setFilter} items={[
            { value: "all", label: "All", count: invoices.length }, { value: "remaining", label: "Remaining", count: summary.remaining_ids.length },
            { value: "verified", label: "Verified" }, { value: "exception", label: "Exceptions" }, { value: "paid", label: "Paid" },
          ]} />
          <div className="flex flex-wrap gap-1">{reasonCounts(invoices.filter((i) => i.status === "exception")).map((r) =>
            <Badge key={r.code} tone="danger" title={reasons[r.code] ?? r.code}>{r.code} · {r.count}</Badge>)}</div>
        </div>
        <Table>
          <thead><tr><th>#</th><th>Invoice</th><th>Vendor</th><th>PO</th><th>Amount</th><th>Status</th><th>Reasons</th></tr></thead>
          <tbody>{shown.map((invoice) => <tr key={invoice.id}>
            <td className="text-muted">{invoice.seq}</td>
            <td><div className="font-medium">{invoice.invoice_number ?? "—"}</div><div className="font-mono text-[10px] text-muted">{invoice.id}</div></td>
            <td>{invoice.vendor_name ?? "—"}</td><td>{invoice.po_number ?? "—"}</td>
            <td className="whitespace-nowrap">{invoice.amount ? `${invoice.currency ?? ""} ${invoice.amount}` : "—"}</td>
            <td><StatusBadge status={invoice.status} /></td>
            <td className="text-xs">{invoice.reasons.map((r) => r.message).join("; ") || "—"}</td>
          </tr>)}</tbody>
        </Table>
      </CardBody>
    </Card>
    {payments.length > 0 && <Card>
      <CardHeader title="Payments" description="Grouped per vendor; payee account comes from the vendor master only." />
      <Table>
        <thead><tr><th>Vendor</th><th>Amount</th><th>Payee account</th><th>Invoices</th><th>Approval hash</th><th>Status</th><th>Bank reference</th></tr></thead>
        <tbody>{payments.map((p) => <tr key={p.id}>
          <td>{p.vendor_name}</td><td className="whitespace-nowrap">{p.currency} {p.amount}</td><td className="font-mono text-xs">{p.payee_account}</td>
          <td>{p.invoice_ids.length}</td><td className="font-mono text-xs" title={p.action_hash ?? ""}>{shortHash(p.action_hash)}</td>
          <td><StatusBadge status={p.status} />{p.failure_code && <div className="text-xs text-danger-text">{p.failure_code}</div>}</td>
          <td className="font-mono text-xs">{p.external_ref ?? "—"}</td>
        </tr>)}</tbody>
      </Table>
    </Card>}
  </div>;
}

function ExceptionsTab({ group, can, revision, toast }: { group: string; can: (p: string) => boolean; revision: number; toast: ToastFn }) {
  const [items, setItems] = useState<Invoice[] | null>(null);
  const [drafts, setDrafts] = useState<Record<string, { invoice_number: string; po_number: string; amount: string; note: string }>>({});
  const [local, setLocal] = useState(0);
  useEffect(() => {
    financeApi.exceptions(group).then((result) => setItems(result.invoices)).catch((err) => toast("error", "Exceptions could not be loaded", describeError(err)));
  }, [group, revision, local, toast]);
  async function resolve(invoice: Invoice, action: "correct" | "reject") {
    const draft = drafts[invoice.id] ?? { invoice_number: "", po_number: "", amount: "", note: "" };
    const fields = action === "correct" ? Object.fromEntries(Object.entries({ invoice_number: draft.invoice_number, po_number: draft.po_number, amount: draft.amount })
      .filter(([, value]) => value.trim()).map(([key, value]) => [key, value.trim()])) : null;
    try { await financeApi.resolve(invoice.id, action, fields, draft.note); toast("success", action === "correct" ? "Sent back for verification" : "Invoice rejected"); setLocal((v) => v + 1); }
    catch (err) { toast("error", "Could not resolve", describeError(err)); }
  }
  if (items === null) return <Skeleton className="h-40 w-full" />;
  if (!items.length) return <Card><EmptyState icon={<BadgeCheck />} title="No exceptions" description="Every checked invoice in this department passed." /></Card>;
  return <Card>
    <CardHeader title={`Exceptions queue (${items.length})`} description="Correct the data (it is verified again by a verifier in the next round) or reject the invoice." />
    <Table>
      <thead><tr><th>Invoice</th><th>Vendor</th><th>Amount</th><th>Reasons</th><th>Resolve</th></tr></thead>
      <tbody>{items.map((invoice) => {
        const draft = drafts[invoice.id] ?? { invoice_number: "", po_number: "", amount: "", note: "" };
        const set = (key: keyof typeof draft, value: string) => setDrafts((cur) => ({ ...cur, [invoice.id]: { ...draft, [key]: value } }));
        return <tr key={invoice.id}>
          <td><div className="font-medium">{invoice.invoice_number ?? "—"}</div><div className="font-mono text-[10px] text-muted">{invoice.id}</div></td>
          <td>{invoice.vendor_name ?? "—"}</td><td className="whitespace-nowrap">{invoice.currency} {invoice.amount ?? "—"}</td>
          <td className="text-xs">{invoice.reasons.map((r) => <div key={r.code}><Badge tone="danger">{r.code}</Badge> {r.message}</div>)}</td>
          <td className="min-w-72"><div className="grid gap-1">
            {can("invoice.ingest") && <div className="grid grid-cols-3 gap-1">
              <Input value={draft.invoice_number} onChange={(e) => set("invoice_number", e.target.value)} placeholder="Invoice no" aria-label="Corrected invoice number" />
              <Input value={draft.po_number} onChange={(e) => set("po_number", e.target.value)} placeholder="PO" aria-label="Corrected PO number" />
              <Input value={draft.amount} onChange={(e) => set("amount", e.target.value)} placeholder="Amount" aria-label="Corrected amount" />
            </div>}
            <Input value={draft.note} onChange={(e) => set("note", e.target.value)} placeholder="Note" aria-label="Resolution note" />
            <div className="flex gap-1">
              {can("invoice.ingest") && <Button size="sm" variant="secondary" onClick={() => resolve(invoice, "correct")}>Correct</Button>}
              {can("invoice.verify") && <Button size="sm" variant="secondary" onClick={() => resolve(invoice, "reject")}>Reject</Button>}
            </div>
          </div></td>
        </tr>;
      })}</tbody>
    </Table>
  </Card>;
}

function ApprovalsTab({ revision, toast }: { revision: number; toast: ToastFn }) {
  const [items, setItems] = useState<PaymentApproval[] | null>(null);
  const [local, setLocal] = useState(0);
  useEffect(() => {
    financeApi.approvals().then((result) => setItems(result.approvals)).catch((err) => toast("error", "Approvals could not be loaded", describeError(err)));
  }, [revision, local, toast]);
  async function decide(item: PaymentApproval, decision: "approve" | "reject") {
    try { await financeApi.decide(item.approval_id, decision, item.action_hash); toast("success", decision === "approve" ? "Payment approved" : "Payment rejected"); setLocal((v) => v + 1); }
    catch (err) { toast("error", "Decision not recorded", describeError(err)); }
  }
  if (items === null) return <Skeleton className="h-40 w-full" />;
  if (!items.length) return <Card><EmptyState icon={<ShieldCheck />} title="Nothing to approve" description="Payments waiting for your payment.approve decision appear here." /></Card>;
  return <div className="grid gap-3">{items.map((item) => <Card key={item.approval_id}>
    <CardHeader title={`${item.currency} ${item.amount} → ${item.vendor_name}`} description={`${item.batch_name} · expires ${new Date(item.expires_at).toLocaleString()}`}
      actions={<div className="flex gap-2">
        <Button variant="primary" disabled={item.own_request} onClick={() => decide(item, "approve")}>Approve this exact payment</Button>
        <Button variant="secondary" disabled={item.own_request} onClick={() => decide(item, "reject")}>Reject</Button>
      </div>} />
    <CardBody className="grid gap-1 text-sm">
      <div>Payee account (vendor master): <span className="font-mono">{item.payee_account}</span></div>
      <div>Invoices ({item.invoice_numbers.length}): {item.invoice_numbers.join(", ")}</div>
      <div className="text-xs text-muted">Action hash <span className="font-mono">{item.action_hash}</span> — your decision applies only to this amount, payee and invoice set; any change needs a new approval.</div>
      {item.own_request && <Alert tone="warning" title="You requested this payment">Another person must decide it.</Alert>}
    </CardBody>
  </Card>)}</div>;
}

function SpendTab({ group, revision }: { group: string; revision: number }) {
  const language = useSyncExternalStore(subscribeLanguage, getLanguage);
  const [spend, setSpend] = useState<Spend | null>(null);
  const [scopeAll, setScopeAll] = useState(true);
  const [currency, setCurrency] = useState("");
  useEffect(() => {
    financeApi.spend(12, scopeAll ? undefined : group).then((result) => { setSpend(result); setCurrency((cur) => cur || result.currencies[0] || ""); }).catch(() => setSpend(null));
  }, [group, scopeAll, revision]);
  const chart = spendSeries(spend, currency);
  const palette = ["bg-primary", "bg-success", "bg-warning", "bg-danger", "bg-foreground-2"];
  return <Card>
    <CardHeader title="Spend by department" description="Verified, approved and paid invoices per month (organisation time zone)." actions={<div className="flex gap-2">
      <SelectInput value={scopeAll ? "all" : "one"} onChange={(e) => setScopeAll(e.target.value === "all")} aria-label="Scope">
        <option value="all">All my departments</option><option value="one">This department</option></SelectInput>
      {spend && spend.currencies.length > 1 && <SelectInput value={currency} onChange={(e) => setCurrency(e.target.value)} aria-label="Currency">
        {spend.currencies.map((c) => <option key={c}>{c}</option>)}</SelectInput>}
    </div>} />
    <CardBody>
      {!spend || !spend.rows.length ? <EmptyState icon={<Wallet />} title="No verified spend yet" /> : <>
        <div className="flex h-56 items-end gap-2" role="img" aria-label="Monthly spend by department">
          {chart.series.map((point) => <div key={point.month} className="flex h-full flex-1 flex-col justify-end" title={`${spendMonthLabel(point.month, language)}: ${formatMoneyForLanguage(point.total, currency || undefined, language)}`}>
            <div className="flex flex-col-reverse overflow-hidden rounded-t" style={{ height: `${chart.max ? (point.total / chart.max) * 100 : 0}%` }}>
              {point.parts.map((part, index) => <div key={part.department} className={palette[index % palette.length]} style={{ height: `${point.total ? (part.amount / point.total) * 100 : 0}%` }} />)}
            </div>
            <div className="mt-1 text-center text-[10px] text-muted">{spendMonthLabel(point.month, language)}</div>
          </div>)}
        </div>
        <div className="mt-3 flex flex-wrap gap-3 text-xs">{chart.departments.map((department, index) =>
          <span key={department} className="flex items-center gap-1"><span className={`size-2.5 rounded-sm ${palette[index % palette.length]}`} />{department}</span>)}</div>
      </>}
    </CardBody>
  </Card>;
}
