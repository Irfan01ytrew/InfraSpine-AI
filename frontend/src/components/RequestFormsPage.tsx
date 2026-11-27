import { useCallback, useEffect, useState } from "react";
import { Archive, ArrowDown, ArrowUp, ClipboardList, Eye, Plus, Save, Trash2, X } from "lucide-react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import { useLiveRefresh } from "../lib/useLiveRefresh";
import type { DepartmentGroup, RequestForm, RequestFormField, ServiceRequestCategory } from "../types";
import { useFeedback } from "./Feedback";
import { RequestFormFields, slug, type FormAnswers } from "./RequestFormFields";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, Input, PageHeader, Select, Skeleton, Textarea } from "./ui";

const CATEGORIES: Array<{ value: ServiceRequestCategory; label: string }> = [
  { value: "it", label: "IT support" }, { value: "hr", label: "HR request" }, { value: "access", label: "Account or access" },
  { value: "facilities", label: "Facilities" }, { value: "purchase", label: "Purchase request" }, { value: "expense", label: "Expense claim" },
  { value: "other", label: "Other" },
];
const TYPES: Array<{ value: RequestFormField["type"]; label: string }> = [
  { value: "text", label: "Short text" }, { value: "long_text", label: "Paragraph" }, { value: "number", label: "Number" },
  { value: "date", label: "Date" }, { value: "choice", label: "Choice" }, { value: "yes_no", label: "Yes / no" },
  { value: "file", label: "File upload" },
];

type Draft = {
  id: string | null; name: string; description: string; category: ServiceRequestCategory; department: string;
  fields: Array<RequestFormField & { optionsText: string }>; audienceDepartments: string[]; audiencePositions: string[];
};
const blankField = (): Draft["fields"][number] => ({ key: null, label: "", type: "text", required: false, options: [], help: null, optionsText: "" });
const blank = (): Draft => ({ id: null, name: "", description: "", category: "other", department: "", fields: [blankField()], audienceDepartments: [], audiencePositions: [] });

/** Pick several departments or positions: chips with remove buttons, plus a dropdown of the rest. */
function MultiPick({ label, options, value, onChange }: { label: string; options: Array<{ value: string; label: string }>; value: string[]; onChange: (next: string[]) => void }) {
  const rest = options.filter((o) => !value.includes(o.value));
  const name = (id: string) => options.find((o) => o.value === id)?.label ?? "Unavailable";
  return (
    <div className="grid gap-1.5">
      {value.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {value.map((id) => (
            <span key={id} className="inline-flex items-center gap-1 rounded-full border border-border bg-surface-2 py-0.5 pl-2.5 pr-1 text-xs text-foreground-2">
              {name(id)}
              <button type="button" className="grid size-4 place-items-center rounded-full hover:bg-surface" aria-label={`Remove ${name(id)} from ${label}`} onClick={() => onChange(value.filter((v) => v !== id))}><X className="size-3" /></button>
            </span>
          ))}
        </div>
      )}
      {rest.length > 0 && <Select aria-label={`Add to ${label}`} placeholder={`Add ${label.toLowerCase()}…`} value="" onValueChange={(v) => onChange([...value, v])} options={rest} />}
    </div>
  );
}

/** Administrators design request forms: typed fields, a fixed category and an optional routing department. */
export function RequestFormsPage() {
  const { toast } = useFeedback();
  const [forms, setForms] = useState<RequestForm[] | null>(null);
  const [departments, setDepartments] = useState<DepartmentGroup[]>([]);
  const [positions, setPositions] = useState<Array<{ id: string; title: string }>>([]);
  const [draft, setDraft] = useState<Draft>(blank);
  const [preview, setPreview] = useState<FormAnswers>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setForms((await api.adminRequestForms()).forms);
      setDepartments((await api.adminGroups()).groups.filter((g) => g.kind === "department" && g.lifecycle_status !== "archived"));
      setPositions((await api.positions()).positions.filter((p) => p.is_active));
      setError(null);
    } catch (err) { setError(describeError(err)); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);

  function edit(form: RequestForm) {
    setDraft({ id: form.id, name: form.name, description: form.description ?? "", category: form.category, department: form.department_group_id ?? "",
      fields: form.fields.map((f) => ({ ...f, optionsText: f.options.join("\n") })),
      audienceDepartments: (form.audience_departments ?? []).map((d) => d.id), audiencePositions: (form.audience_positions ?? []).map((p) => p.id) });
    setPreview({});
  }
  const setField = (index: number, patch: Partial<Draft["fields"][number]>) =>
    setDraft({ ...draft, fields: draft.fields.map((f, i) => (i === index ? { ...f, ...patch } : f)) });
  const moveField = (index: number, by: number) => {
    const next = [...draft.fields];
    [next[index], next[index + by]] = [next[index + by], next[index]];
    setDraft({ ...draft, fields: next });
  };
  const outFields = (): RequestFormField[] => draft.fields.map(({ optionsText, ...f }) => ({
    ...f, label: f.label.trim(), options: f.type === "choice" ? optionsText.split(/[\n,]/).map((o) => o.trim()).filter(Boolean) : [],
    help: f.help?.trim() || null,
  }));

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    const input = { name: draft.name.trim(), description: draft.description.trim() || null, category: draft.category,
      department_group_id: draft.department || null, fields: outFields(),
      audience_department_ids: draft.audienceDepartments, audience_position_ids: draft.audiencePositions };
    try {
      const result = draft.id ? await api.updateRequestForm(draft.id, input) : await api.createRequestForm(input);
      setForms(result.forms);
      toast("success", draft.id ? "Form saved" : "Form created", draft.id ? "Requests already submitted keep their answers." : "Employees can now choose it under Help & requests.");
      const saved = result.forms.find((f) => f.name.toLowerCase() === input.name.toLowerCase());
      if (saved) edit(saved);
    } catch (err) { toast("error", "Could not save the form", describeError(err)); }
    finally { setBusy(false); }
  }

  async function archive(form: RequestForm) {
    try {
      setForms((await api.archiveRequestForm(form.id)).forms);
      if (draft.id === form.id) setDraft(blank());
      toast("success", "Form archived", "It no longer appears for new requests; past requests keep their answers.");
    } catch (err) { toast("error", "Could not archive the form", describeError(err)); }
  }

  const fields = outFields();
  const ready = draft.name.trim().length >= 2 && fields.length > 0 && fields.every((f) => f.label && (f.type !== "choice" || f.options.length >= 2));

  return (
    <>
      <PageHeader title="Request forms" description="Design the forms people use under Help & requests: the fields to fill in, the kind of request, and the department it goes to." />
      {error && <Alert tone="danger" className="mb-5">{error}</Alert>}
      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,320px)_minmax(0,1fr)]">
        <Card className="overflow-hidden">
          <CardHeader icon={<ClipboardList />} title="Forms" actions={<Button size="sm" onClick={() => { setDraft(blank()); setPreview({}); }}><Plus /> New</Button>} />
          <CardBody className="grid gap-2">
            {forms === null ? <Skeleton className="h-24" /> : forms.length === 0 ? <EmptyState icon={<ClipboardList />} title="No forms yet" description="Create one, e.g. “New laptop” or “Travel advance”." /> : forms.map((form) => (
              <div key={form.id} className={`flex items-start justify-between gap-2 rounded-lg border px-3 py-2 ${draft.id === form.id ? "border-primary bg-primary-soft/40" : "border-border"}`}>
                <button type="button" className="min-w-0 flex-1 text-left" onClick={() => edit(form)}>
                  <span className="block truncate text-[13px] font-medium text-foreground">{form.name}</span>
                  <span className="block text-xs text-muted">{CATEGORIES.find((c) => c.value === form.category)?.label} · {form.fields.length} field{form.fields.length === 1 ? "" : "s"}{form.fields.some((f) => f.show_if) ? " (some conditional)" : ""}{(form.audience_departments?.length || form.audience_positions?.length) ? ` · for ${[...(form.audience_departments ?? []), ...(form.audience_positions ?? [])].map((a) => a.name).join(", ")}` : " · everyone"}{form.department_name ? ` · to ${form.department_name}` : ""} · v{form.version}</span>
                  <span className="block text-[11px] text-muted">Updated {formatDate(form.updated_at)}</span>
                </button>
                <Button size="icon-sm" variant="danger-ghost" aria-label={`Archive ${form.name}`} onClick={() => void archive(form)}><Archive /></Button>
              </div>
            ))}
          </CardBody>
        </Card>

        <div className="grid min-w-0 gap-5 lg:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
          <Card className="overflow-hidden">
            <CardHeader icon={<ClipboardList />} title={draft.id ? `Edit “${draft.name || "form"}”` : "New form"} description="Changing fields starts a new version; submitted requests keep the questions and answers they were sent with." />
            <CardBody>
              <form className="grid gap-3" onSubmit={(event) => void save(event)}>
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="Form name"><Input maxLength={120} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="e.g. New laptop" /></Field>
                  <Field label="Kind of request"><Select aria-label="Form category" value={draft.category} onValueChange={(v) => setDraft({ ...draft, category: v as ServiceRequestCategory })} options={CATEGORIES} /></Field>
                </div>
                <Field label="Goes to department" hint="Requests go to this department even if the person is not a member of it. Leave empty to let them choose.">
                  <Select aria-label="Form department" placeholder="Person chooses" value={draft.department} onValueChange={(v) => setDraft({ ...draft, department: v === "__none" ? "" : v })}
                    options={[{ value: "__none", label: "Person chooses" }, ...departments.map((d) => ({ value: d.id, label: d.name }))]} />
                </Field>
                <div className="grid gap-2 rounded-lg border border-border p-3">
                  <p className="text-xs font-semibold text-foreground-2">Who can use this form</p>
                  <p className="text-xs text-muted">{draft.audienceDepartments.length || draft.audiencePositions.length
                    ? "Only members and managers of these departments, and holders of these positions. Administrators always can."
                    : "Everyone. Add departments or positions to limit it."}</p>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <Field label="Departments"><MultiPick label="Departments" value={draft.audienceDepartments} onChange={(next) => setDraft({ ...draft, audienceDepartments: next })} options={departments.map((d) => ({ value: d.id, label: d.name }))} /></Field>
                    <Field label="Positions"><MultiPick label="Positions" value={draft.audiencePositions} onChange={(next) => setDraft({ ...draft, audiencePositions: next })} options={positions.map((p) => ({ value: p.id, label: p.title }))} /></Field>
                  </div>
                </div>
                <Field label="Instructions (optional)"><Textarea rows={2} maxLength={500} value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></Field>
                {(draft.category === "purchase" || draft.category === "expense") && <p className="text-xs text-muted">Purchase and expense forms also ask for the amount and currency, and follow approval limits, chains and budgets.</p>}

                <p className="text-xs font-semibold text-foreground-2">Fields</p>
                <ol className="grid gap-2">
                  {draft.fields.map((field, index) => (
                    <li key={index} className="grid gap-2 rounded-lg border border-border p-3">
                      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_150px]">
                        <Input aria-label={`Field ${index + 1} label`} maxLength={120} value={field.label} onChange={(e) => setField(index, { label: e.target.value })} placeholder="Question or label" />
                        <Select aria-label={`Field ${index + 1} type`} value={field.type} onValueChange={(v) => setField(index, { type: v as RequestFormField["type"] })} options={TYPES} />
                      </div>
                      {field.type === "choice" && <Textarea aria-label={`Field ${index + 1} options`} rows={2} value={field.optionsText} onChange={(e) => setField(index, { optionsText: e.target.value })} placeholder="One option per line" />}
                      <Input aria-label={`Field ${index + 1} help`} maxLength={200} value={field.help ?? ""} onChange={(e) => setField(index, { help: e.target.value })} placeholder="Help text (optional)" />
                      {(() => {
                        // Parents: earlier choice or yes/no fields with a label. A reference is the key, or the label before first save.
                        const parents = draft.fields.slice(0, index).filter((p) => (p.type === "choice" || p.type === "yes_no") && p.label.trim());
                        if (parents.length === 0) return null;
                        const ref = (p: Draft["fields"][number]) => p.key || p.label.trim();
                        const parent = field.show_if ? parents.find((p) => ref(p) === field.show_if!.field || slug(p.label) === slug(field.show_if!.field)) : undefined;
                        const answersOf = (p: Draft["fields"][number]) => p.type === "yes_no" ? ["Yes", "No"] : p.optionsText.split(/[\n,]/).map((o) => o.trim()).filter(Boolean);
                        return (
                          <div className="grid gap-2 sm:grid-cols-2">
                            <Select aria-label={`Field ${index + 1} shown when`} value={parent ? ref(parent) : "__always"}
                              onValueChange={(v) => setField(index, { show_if: v === "__always" ? null : { field: v, equals: answersOf(parents.find((p) => ref(p) === v)!)[0] ?? "" } })}
                              options={[{ value: "__always", label: "Always shown" }, ...parents.map((p) => ({ value: ref(p), label: `Only when “${p.label.trim()}” is…` }))]} />
                            {parent && <Select aria-label={`Field ${index + 1} shown when answer`} value={field.show_if!.equals}
                              onValueChange={(v) => setField(index, { show_if: { field: ref(parent), equals: v } })}
                              options={answersOf(parent).map((a) => ({ value: a, label: a }))} />}
                          </div>
                        );
                      })()}
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <label className="inline-flex items-center gap-2 text-[13px] text-foreground-2">
                          <input type="checkbox" className="size-4 accent-[var(--brand)]" checked={field.required} onChange={(e) => setField(index, { required: e.target.checked })} />
                          {field.type === "yes_no" ? "Must be ticked" : "Required"}
                        </label>
                        <span className="inline-flex gap-1">
                          <Button size="icon-sm" variant="ghost" aria-label={`Move field ${index + 1} up`} disabled={index === 0} onClick={() => moveField(index, -1)}><ArrowUp /></Button>
                          <Button size="icon-sm" variant="ghost" aria-label={`Move field ${index + 1} down`} disabled={index === draft.fields.length - 1} onClick={() => moveField(index, 1)}><ArrowDown /></Button>
                          <Button size="icon-sm" variant="danger-ghost" aria-label={`Remove field ${index + 1}`} disabled={draft.fields.length === 1} onClick={() => setDraft({ ...draft, fields: draft.fields.filter((_, i) => i !== index) })}><Trash2 /></Button>
                        </span>
                      </div>
                    </li>
                  ))}
                </ol>
                <div className="flex flex-wrap justify-between gap-2">
                  <Button disabled={draft.fields.length >= 30} onClick={() => setDraft({ ...draft, fields: [...draft.fields, blankField()] })}><Plus /> Add field</Button>
                  <Button type="submit" variant="primary" loading={busy} disabled={busy || !ready}>{!busy && <Save />} {draft.id ? "Save form" : "Create form"}</Button>
                </div>
              </form>
            </CardBody>
          </Card>

          <Card className="overflow-hidden">
            <CardHeader icon={<Eye />} title="Preview" description="What people see when they choose this form." actions={draft.id ? <Badge>Live</Badge> : <Badge>Draft</Badge>} />
            <CardBody className="grid gap-3">
              <p className="text-[13px] font-medium text-foreground">{draft.name || "Untitled form"}</p>
              {draft.description && <p className="text-xs text-muted">{draft.description}</p>}
              <RequestFormFields idPrefix="preview" preview fields={fields.map((f, i) => ({ ...f, key: f.key || `field_${i}` }))} answers={preview} onChange={setPreview} />
            </CardBody>
          </Card>
        </div>
      </div>
    </>
  );
}
