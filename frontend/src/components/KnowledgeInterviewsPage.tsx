import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import type { InterviewOptions, InterviewSession, InterviewTemplate, User } from "../types";
import { useFeedback } from "./Feedback";
import { Alert, Button, PageHeader, SelectInput } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

const STATUS: Record<InterviewSession["status"], string> = {
  in_progress: "In progress",
  submitted: "Awaiting review",
  changes_requested: "Changes requested",
  approved: "Approved",
  withdrawn: "Withdrawn",
};

export function KnowledgeInterviewsPage({ user }: { user: User }) {
  const { toast } = useFeedback();
  const [options, setOptions] = useState<InterviewOptions | null>(null);
  const [sessions, setSessions] = useState<InterviewSession[]>([]);
  const [selected, setSelected] = useState<InterviewSession | null>(null);
  const [templateId, setTemplateId] = useState("");
  const [departmentId, setDepartmentId] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [sopTitle, setSopTitle] = useState("");
  const [rationale, setRationale] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showTemplateForm, setShowTemplateForm] = useState(false);
  const [newTemplateTitle, setNewTemplateTitle] = useState("");
  const [newTemplateDescription, setNewTemplateDescription] = useState("");
  const [newTemplatePrompts, setNewTemplatePrompts] = useState("");

  const load = useCallback(async () => {
    try {
      const [nextOptions, result] = await Promise.all([
        api.knowledgeInterviewOptions(),
        api.knowledgeInterviewSessions(),
      ]);
      setOptions(nextOptions);
      setSessions(result.sessions);
      setTemplateId((current) => nextOptions.templates.some((template) => template.id === current)
        ? current : nextOptions.templates[0]?.id || "");
      setDepartmentId((current) => current || nextOptions.departments[0]?.id || "");
      setError("");
    } catch (err) {
      setError(describeError(err));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);

  function activate(session: InterviewSession) {
    setSelected(session);
    setAnswers(session.answers ?? {});
    setSopTitle(session.sop_title ?? "");
    setRationale("");
    setError("");
  }

  async function open(id: string) {
    setBusy(true);
    try { activate(await api.knowledgeInterviewSession(id)); }
    catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  function replaceSession(session: InterviewSession) {
    setSessions((current) => [session, ...current.filter((item) => item.id !== session.id)]);
    activate(session);
  }

  async function start() {
    if (!templateId) return;
    setBusy(true); setError("");
    try {
      replaceSession(await api.startKnowledgeInterview(templateId, departmentId || null));
      toast("success", "Interview started", "Your answers are private to you until you submit the SOP for department review.");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function save() {
    if (!selected) return;
    setBusy(true); setError("");
    try {
      const saved = await api.saveKnowledgeInterview(selected.id, answers, sopTitle.trim() || undefined);
      replaceSession(saved);
      toast("success", "Progress saved", "You can return to this interview from your saved sessions.");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function submit() {
    if (!selected) return;
    setBusy(true); setError("");
    try {
      await api.saveKnowledgeInterview(selected.id, answers, sopTitle.trim() || undefined);
      const submitted = await api.submitKnowledgeInterview(selected.id);
      replaceSession(submitted);
      toast("success", "SOP sent for review", "A reviewer in the selected department must approve it before it is marked complete.");
      await load();
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function review(decision: "approved" | "changes_requested") {
    if (!selected || rationale.trim().length < 3) return;
    setBusy(true); setError("");
    try {
      const result = await api.reviewKnowledgeInterview(selected.id, decision, rationale.trim());
      replaceSession(result);
      toast("success", decision === "approved" ? "SOP approved" : "Changes requested", "The decision and rationale were recorded in review history.");
      await load();
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function withdraw() {
    if (!selected) return;
    setBusy(true); setError("");
    try {
      replaceSession(await api.withdrawKnowledgeInterview(selected.id));
      toast("info", "Interview withdrawn", "The saved answers remain in your history and are no longer in the review queue.");
      await load();
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function createTemplate(event: FormEvent) {
    event.preventDefault();
    const prompts = newTemplatePrompts.split("\n").map((prompt) => prompt.trim()).filter(Boolean);
    if (!newTemplateTitle.trim() || !newTemplateDescription.trim() || prompts.length === 0) return;
    const slug = newTemplateTitle.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 80);
    if (prompts.length > 20) return setError("A template can include up to 20 questions.");
    setBusy(true); setError("");
    try {
      const created = await api.createKnowledgeInterviewTemplate({
        slug,
        title: newTemplateTitle.trim(),
        description: newTemplateDescription.trim(),
        questions: prompts.map((prompt, index) => ({ key: `question_${String(index + 1).padStart(2, "0")}`, prompt, required: true })),
      });
      setShowTemplateForm(false);
      setNewTemplateTitle(""); setNewTemplateDescription(""); setNewTemplatePrompts("");
      toast("success", "Interview template added", "New sessions will use this saved question set.");
      await load();
      setTemplateId(created.id);
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function toggleTemplate(template: InterviewTemplate) {
    setBusy(true); setError("");
    try {
      await api.updateKnowledgeInterviewTemplate(template.id, { is_active: !template.is_active });
      await load();
      toast("success", template.is_active ? "Template paused" : "Template activated");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  const missingRequired = selected?.template_questions.some((question) =>
    question.required && !answers[question.key]?.trim(),
  ) ?? true;

  return (
    <div className="page-content knowledge-interviews-page">
      <PageHeader
        title="Knowledge interviews"
        description="Capture work knowledge in the employee's own words, then send a department SOP draft for a separate human review."
        actions={<>{user.role === "admin" && <Button onClick={() =>setShowTemplateForm((open) => !open)}>{showTemplateForm ? "Close template form" : "Add interview template"}</Button>}</>}
      />

      <Alert tone="info" className="mb-4 interview-disclosure">Interview answers are human-authored. InfraSpine formats them into an SOP draft; it does not invent answers or citations.</Alert>
      {error && <Alert tone="danger" className="mb-4">{error}</Alert>}

      {showTemplateForm && options?.can_manage_templates && (
        <form className="card interview-template-form" onSubmit={(event) => void createTemplate(event)}>
          <h2>New reusable interview</h2>
          <label className="field"><span className="field-label">Template title</span><input value={newTemplateTitle} onChange={(event) => setNewTemplateTitle(event.target.value)} maxLength={160} required /></label>
          <label className="field"><span className="field-label">Purpose</span><textarea value={newTemplateDescription} onChange={(event) => setNewTemplateDescription(event.target.value)} maxLength={800} rows={2} required /></label>
          <label className="field"><span className="field-label">Questions, one per line</span><textarea value={newTemplatePrompts} onChange={(event) => setNewTemplatePrompts(event.target.value)} maxLength={6000} rows={6} required placeholder="What outcome should this process achieve?&#10;Which checks must be completed?" /></label>
          <div className="form-actions"><button className="btn btn-primary" disabled={busy}>Save template</button></div>
        </form>
      )}

      <div className="interview-start card">
        <div><h2>Start an interview</h2><p className="muted">Choose a template and department. Private drafts can be saved, but need a department to enter the review queue.</p></div>
        <div className="interview-start-controls">
          <label className="field"><span className="field-label">Template</span><SelectInput value={templateId} onChange={(event) => setTemplateId(event.target.value)} disabled={!options?.templates.length}><option value="">Choose template</option>{options?.templates.map((template) => <option key={template.id} value={template.id}>{template.title}</option>)}</SelectInput></label>
          <label className="field"><span className="field-label">Department</span><SelectInput value={departmentId} onChange={(event) => setDepartmentId(event.target.value)}><option value="">Private draft</option>{options?.departments.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</SelectInput></label>
          <button className="btn btn-primary" onClick={() => void start()} disabled={busy || !templateId}>Start</button>
        </div>
      </div>

      {options?.can_manage_templates && <section className="card interview-templates">
        <h2>Available templates</h2>
        {options.managed_templates.length ? <ul>{options.managed_templates.map((template) => <li key={template.id} className="interview-template-row"><span><strong>{template.title}</strong><span className="muted">{template.description}</span></span><span className="pill pill-sm">{template.is_active ? "Active" : "Paused"}</span>{!template.built_in && <button className="btn btn-sm" disabled={busy} onClick={() => void toggleTemplate(template)}>{template.is_active ? "Pause" : "Activate"}</button>}</li>)}</ul> : <p className="muted">No templates have been saved.</p>}
      </section>}

      <div className="interview-layout">
        <section className="card interview-session-list" aria-label="Saved interviews">
          <h2>Saved interviews and review queue</h2>
          {sessions.length === 0 ? <p className="muted">Your saved interviews and department review items will appear here.</p> : <div className="interview-session-cards">{sessions.map((session) => <button key={session.id} className={`interview-session-card ${selected?.id === session.id ? "selected" : ""}`} onClick={() => void open(session.id)}>
            <span className="interview-session-title">{session.sop_title || session.template_title}</span>
            <span className="muted">{session.department_name || "Private draft"} · {formatDate(session.updated_at)}</span>
            <span className={`pill pill-sm ${session.status === "approved" ? "pill-success" : session.status === "submitted" ? "pill-warning" : ""}`}>{STATUS[session.status]}</span>
          </button>)}</div>}
        </section>

        <section className="card interview-editor" aria-label="Interview editor">
          {!selected ? <div className="empty-state"><h2>Open a saved interview</h2><p className="muted">Start an interview or select one from your saved list. Progress is stored so you can return later.</p></div> : <>
            <div className="interview-editor-heading"><div><span className="eyebrow">{selected.template_title}</span><h2>{selected.sop_title || "New SOP draft"}</h2><p className="muted">{selected.department_name || "Private draft"} · {STATUS[selected.status]} · revision {selected.revision_number}</p></div><button className="btn btn-sm" onClick={() => setSelected(null)}>Close</button></div>
            {selected.can_edit && <>
              <label className="field"><span className="field-label">SOP title</span><input value={sopTitle} onChange={(event) => setSopTitle(event.target.value)} maxLength={180} placeholder="Uses the process name if left blank" /></label>
              <div className="interview-questions">{selected.template_questions.map((question) => <label className="field" key={question.key}><span className="field-label">{question.prompt}{question.required && <span className="required-mark"> · required</span>}</span><textarea value={answers[question.key] ?? ""} onChange={(event) => setAnswers((current) => ({ ...current, [question.key]: event.target.value }))} maxLength={10000} rows={question.key === "steps" ? 5 : 3} /></label>)}</div>
              <div className="form-actions interview-actions"><button className="btn" disabled={busy} onClick={() => void save()}>Save progress</button><button className="btn btn-primary" disabled={busy || missingRequired || !selected.department_group_id} onClick={() => void submit()}>Submit SOP for review</button>{selected.status !== "approved" && <button className="btn btn-quiet" disabled={busy} onClick={() => void withdraw()}>Withdraw</button>}</div>
              {selected.sop_body && <div className="interview-sop-preview"><h3>Draft preview</h3><pre>{selected.sop_body}</pre></div>}
            </>}
            {!selected.can_edit && selected.sop_body && <div className="interview-sop-preview"><h3>{selected.status === "approved" ? "Approved SOP" : "Submitted SOP"}</h3><pre>{selected.sop_body}</pre></div>}
            {selected.can_review && <div className="interview-review-form"><h3>Department review</h3><label className="field"><span className="field-label">Decision rationale</span><textarea value={rationale} onChange={(event) => setRationale(event.target.value)} minLength={3} maxLength={2000} rows={3} placeholder="Explain the decision or changes needed" /></label><div className="form-actions"><button className="btn" disabled={busy || rationale.trim().length < 3} onClick={() => void review("changes_requested")}>Request changes</button><button className="btn btn-primary" disabled={busy || rationale.trim().length < 3} onClick={() => void review("approved")}>Approve SOP</button></div></div>}
            {selected.reviews.length > 0 && <div className="interview-review-history"><h3>Review history</h3>{selected.reviews.map((item) => <article key={item.id}><strong>{item.decision === "approved" ? "Approved" : "Changes requested"}</strong><span className="muted"> by {item.reviewer_email} · revision {item.revision_number} · {formatDate(item.reviewed_at)}</span><p>{item.rationale}</p></article>)}</div>}
            {selected.sop_revisions.length > 1 && <details className="interview-revision-history"><summary>Earlier submitted revisions ({selected.sop_revisions.length - 1})</summary>{selected.sop_revisions.slice(0, -1).map((revision) => <article key={revision.id}><h4>{revision.title} · revision {revision.revision_number}</h4><pre>{revision.body}</pre></article>)}</details>}
          </>}
        </section>
      </div>
    </div>
  );
}
