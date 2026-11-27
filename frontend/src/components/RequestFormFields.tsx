import { useState } from "react";
import { Paperclip, X } from "lucide-react";
import { api, describeError } from "../api";
import type { RequestAttachment, RequestFormField } from "../types";
import { useFeedback } from "./Feedback";
import { Button, Field, Input, SelectInput, Textarea } from "./ui";

export type FormAnswers = Record<string, string | boolean | RequestAttachment[]>;
const MAX_FILES = 5;
const ACCEPT = ".pdf,.docx,.xlsx,.pptx,.txt,.csv,.png,.jpg,.jpeg";

function sizeLabel(bytes: number) {
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

/** Uploads files as they are chosen; the request links them when it is submitted. */
function FileField({ id, label, files, onChange, preview }: {
  id: string; label: string; files: RequestAttachment[]; onChange: (next: RequestAttachment[]) => void; preview: boolean;
}) {
  const { toast } = useFeedback();
  const [busy, setBusy] = useState(false);

  async function add(list: FileList | null) {
    if (!list?.length) return;
    setBusy(true);
    const next = [...files];
    for (const file of Array.from(list).slice(0, MAX_FILES - files.length)) {
      try { next.push(await api.uploadRequestAttachment(file)); }
      catch (err) { toast("error", `Could not attach ${file.name}`, describeError(err)); }
    }
    onChange(next);
    setBusy(false);
  }

  async function remove(file: RequestAttachment) {
    onChange(files.filter((f) => f.id !== file.id));
    try { await api.removeRequestAttachment(file.id); } catch { /* unused uploads are cleaned up after a day */ }
  }

  return (
    <div className="grid gap-2">
      {files.length > 0 && (
        <ul className="grid gap-1" aria-label={`${label} files`}>
          {files.map((file) => (
            <li key={file.id} className="flex items-center gap-2 rounded-md border border-border px-2 py-1 text-[13px]">
              <Paperclip className="size-3.5 shrink-0 text-muted" />
              <span className="min-w-0 flex-1 truncate">{file.filename}</span>
              <span className="text-xs text-muted">{sizeLabel(file.size_bytes)}</span>
              <Button size="icon-sm" variant="ghost" aria-label={`Remove ${file.filename}`} onClick={() => void remove(file)}><X /></Button>
            </li>
          ))}
        </ul>
      )}
      {files.length < MAX_FILES && (
        <input id={id} type="file" multiple accept={ACCEPT} aria-label={label} disabled={busy || preview}
          className="block w-full text-[13px] text-foreground-2 file:mr-3 file:rounded-md file:border file:border-border file:bg-surface-2 file:px-3 file:py-1.5 file:text-[13px] file:text-foreground"
          onChange={(e) => { void add(e.target.files); e.target.value = ""; }} />
      )}
      <p className="text-xs text-muted">{busy ? "Uploading…" : `PDF, Office, text, CSV, PNG or JPEG · up to 10 MB each · ${MAX_FILES} files at most`}</p>
    </div>
  );
}

export function slug(label: string) {
  return label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40) || "field";
}

/** The fields shown for these answers: a conditional field appears when its (shown) parent has the expected answer. */
export function visibleFields(fields: RequestFormField[], answers: FormAnswers) {
  const given = new Map<string, string>();
  return fields.filter((field, index) => {
    const rule = field.show_if;
    if (rule && given.get(rule.field) !== rule.equals && given.get(slug(rule.field)) !== rule.equals) return false;
    const key = field.key || `field_${index}`;
    const value = answers[key];
    const answer = field.type === "yes_no" ? (value === true ? "Yes" : "No") : field.type === "choice" && typeof value === "string" ? value : null;
    if (answer !== null) { given.set(key, answer); given.set(slug(field.label), answer); }
    return true;
  });
}

/** Fill-in controls for a custom request form; also used as the live preview in the form builder. */
export function RequestFormFields({ fields, answers, onChange, idPrefix = "form", preview = false }: {
  fields: RequestFormField[]; answers: FormAnswers; onChange: (next: FormAnswers) => void; idPrefix?: string; preview?: boolean;
}) {
  const set = (key: string, value: FormAnswers[string]) => onChange({ ...answers, [key]: value });
  const shown = new Set(visibleFields(fields, answers));
  return (
    <div className="grid gap-3">
      {fields.map((field, index) => {
        if (!shown.has(field)) return null;
        const key = field.key || `field_${index}`;
        const label = `${field.label}${field.required && field.type !== "yes_no" ? " *" : ""}`;
        const value = answers[key];
        if (field.type === "yes_no") {
          return (
            <label key={key} className="flex items-start gap-2 text-[13px] text-foreground-2">
              <input type="checkbox" className="mt-0.5 size-4 accent-[var(--brand)]" checked={value === true} onChange={(e) => set(key, e.target.checked)} />
              <span>{field.label}{field.required && <span className="text-danger-text"> *</span>}{field.help && <span className="block text-xs text-muted">{field.help}</span>}</span>
            </label>
          );
        }
        if (field.type === "file") {
          return (
            <Field key={key} label={label} htmlFor={`${idPrefix}-${key}`} hint={field.help ?? undefined}>
              <FileField id={`${idPrefix}-${key}`} label={field.label} files={Array.isArray(value) ? value : []} onChange={(next) => set(key, next)} preview={preview} />
            </Field>
          );
        }
        const text = typeof value === "string" ? value : "";
        const control = field.type === "long_text" ? <Textarea id={`${idPrefix}-${key}`} aria-label={field.label} rows={3} maxLength={4000} value={text} onChange={(e) => set(key, e.target.value)} />
          : field.type === "choice" ? (
            <SelectInput id={`${idPrefix}-${key}`} aria-label={field.label} value={text} onChange={(e) => set(key, e.target.value)}>
              <option value="">Choose…</option>
              {field.options.map((option) => <option key={option} value={option}>{option}</option>)}
            </SelectInput>
          )
          : <Input id={`${idPrefix}-${key}`} aria-label={field.label} type={field.type === "date" ? "date" : "text"} inputMode={field.type === "number" ? "decimal" : undefined}
              maxLength={field.type === "text" ? 500 : 40} value={text} onChange={(e) => set(key, e.target.value)} />;
        return <Field key={key} label={label} htmlFor={`${idPrefix}-${key}`} hint={field.help ?? undefined}>{control}</Field>;
      })}
    </div>
  );
}

/** Whether every required field has an answer (the server checks types and options too). */
export function formComplete(fields: RequestFormField[], answers: FormAnswers) {
  return visibleFields(fields, answers).every((field) => {
    if (!field.required) return true;
    const value = answers[field.key ?? ""];
    if (field.type === "yes_no") return value === true;
    if (field.type === "file") return Array.isArray(value) && value.length > 0;
    return String(value ?? "").trim() !== "";
  });
}

/** Answers as the API expects them: only shown fields, and files become their attachment ids. */
export function answersForApi(fields: RequestFormField[], answers: FormAnswers): Record<string, string | boolean | string[]> {
  const shown = new Set(visibleFields(fields, answers).map((field) => field.key));
  return Object.fromEntries(Object.entries(answers).filter(([key]) => shown.has(key))
    .map(([key, value]) => [key, Array.isArray(value) ? value.map((file) => file.id) : value]));
}
