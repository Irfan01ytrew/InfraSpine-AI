import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import type { Meeting, MeetingActionTask, MeetingCitationDraft, MeetingDetail, MeetingSource, MeetingTaskDraft, MeetingTranscriptChunk, User } from "../types";
import { useFeedback } from "./Feedback";
import { AudioLines, CalendarClock, Check, FilePlus2, Gavel, History, ListChecks, Mic, NotebookPen, Plus, RefreshCw, Save, Send, ShieldCheck, Sparkles, Trash2 } from "lucide-react";
import { cn } from "../lib/utils";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, Input, PageHeader, Select, Textarea } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

function chunkLabel(chunk: MeetingTranscriptChunk): string {
  const locator = chunk.locator;
  const label = locator && typeof locator.label === "string" ? locator.label : `Transcript chunk ${chunk.id}`;
  return `${label} · ${chunk.content.slice(0, 90)}`;
}

export function MeetingsPage({ user }: { user: User }) {
  const { toast } = useFeedback();
  const [sources, setSources] = useState<MeetingSource[]>([]);
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [selectedSourceId, setSelectedSourceId] = useState("");
  const [title, setTitle] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<MeetingDetail | null>(null);
  const [transcript, setTranscript] = useState<MeetingTranscriptChunk[]>([]);
  const [minutes, setMinutes] = useState("");
  const [decisions, setDecisions] = useState<MeetingCitationDraft[]>([]);
  const [tasks, setTasks] = useState<MeetingTaskDraft[]>([]);
  const [rationale, setRationale] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const [sourceResult, meetingResult] = await Promise.all([api.meetingSources(), api.meetings()]);
      setSources(sourceResult.sources);
      setMeetings(meetingResult.meetings);
      setSelectedSourceId((current) => current || sourceResult.sources[0]?.id || "");
      setError("");
    } catch (err) {
      setError(describeError(err));
    }
  }, []);

  const openMeeting = useCallback(async (id: string) => {
    setSelectedId(id);
    setDetail(null);
    try {
      const [meeting, sourceTranscript] = await Promise.all([api.meeting(id), api.meetingTranscript(id)]);
      setDetail(meeting);
      setTranscript(sourceTranscript.chunks);
      setMinutes(meeting.revision.minutes_markdown);
      setDecisions(meeting.revision.decisions.map((item) => ({ ...item, evidence_chunk_ids: [...item.evidence_chunk_ids] })));
      setTasks(meeting.revision.proposed_tasks.map((item) => ({ ...item, evidence_chunk_ids: [...item.evidence_chunk_ids] })));
      setRationale("");
      setError("");
    } catch (err) {
      try {
        const meeting = await api.meeting(id);
        setDetail(meeting);
        setTranscript([]);
        setMinutes(meeting.revision.minutes_markdown);
        setDecisions(meeting.revision.decisions);
        setTasks(meeting.revision.proposed_tasks);
        setError(describeError(err));
      } catch (loadErr) {
        setDetail(null);
        setError(describeError(loadErr));
      }
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);

  async function createMeeting(event: React.FormEvent) {
    event.preventDefault();
    if (!selectedSourceId || !title.trim()) return;
    setBusy(true);
    try {
      const meeting = await api.createMeeting({ source_document_id: selectedSourceId, title: title.trim() });
      setTitle("");
      setMeetings((current) => [meeting, ...current]);
      await openMeeting(meeting.id);
      toast("success", "Meeting draft created", "Minutes, decisions and proposed tasks remain private to people who can access the recording.");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  function requestBody() {
    if (!detail) throw new Error("Open a meeting first.");
    return {
      expected_version: detail.current_revision,
      minutes_markdown: minutes,
      decisions,
      proposed_tasks: tasks,
    };
  }

  async function saveDraft(): Promise<MeetingDetail | null> {
    if (!detail) return null;
    setBusy(true); setError("");
    try {
      const saved = await api.saveMeetingDraft(detail.id, requestBody());
      setDetail(saved);
      setMeetings((current) => current.map((item) => item.id === saved.id ? saved : item));
      toast("success", "Draft saved", `Revision ${saved.current_revision} is retained in the meeting history.`);
      return saved;
    } catch (err) { setError(describeError(err)); return null; }
    finally { setBusy(false); }
  }

  async function generateMinutes() {
    if (!detail) return;
    setBusy(true); setError("");
    try {
      const generated = await api.generateMeetingDraft(detail.id, detail.current_revision);
      setDetail(generated);
      setMeetings((current) => current.map((item) => item.id === generated.id ? generated : item));
      setMinutes(generated.revision.minutes_markdown);
      setDecisions(generated.revision.decisions.map((item) => ({ ...item, evidence_chunk_ids: [...item.evidence_chunk_ids] })));
      setTasks(generated.revision.proposed_tasks.map((item) => ({ ...item, evidence_chunk_ids: [...item.evidence_chunk_ids] })));
      toast("success", "AI draft generated", "Check each cited transcript passage before saving or submitting these minutes.");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function submitForReview() {
    const saved = await saveDraft();
    if (!saved) return;
    setBusy(true); setError("");
    try {
      const submitted = await api.submitMeeting(saved.id, saved.current_revision);
      setDetail(submitted);
      setMeetings((current) => current.map((item) => item.id === submitted.id ? submitted : item));
      toast("success", "Sent for review", "A scoped department manager or administrator must review the minutes before tasks are assigned.");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function review(decision: "approved" | "changes_requested") {
    if (!detail || rationale.trim().length < 3) return;
    setBusy(true); setError("");
    try {
      const updated = await api.reviewMeeting(detail.id, decision, rationale.trim());
      setDetail(updated);
      setMeetings((current) => current.map((item) => item.id === updated.id ? updated : item));
      setRationale("");
      toast("success", decision === "approved" ? "Meeting approved" : "Changes requested", decision === "approved" ? "Reviewed action items are now assigned." : "The author can revise and resubmit these minutes.");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function completeTask(task: MeetingActionTask) {
    setBusy(true); setError("");
    try {
      await api.completeMeetingTask(task.id);
      if (selectedId) await openMeeting(selectedId);
      toast("success", "Action item completed");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  function addDecision() {
    setDecisions((current) => [...current, { text: "", evidence_chunk_ids: transcript[0] ? [transcript[0].id] : [] }]);
  }

  function addTask() {
    const assignee = detail?.eligible_assignees.find((candidate) => candidate.id === user.id) ?? detail?.eligible_assignees[0];
    setTasks((current) => [...current, {
      title: "", body: "", assignee_user_id: assignee?.id ?? "", due_at: null,
      evidence_chunk_ids: transcript[0] ? [transcript[0].id] : [],
    }]);
  }

  const editable = Boolean(detail?.can_edit && detail.source_is_current);
  const sourceOptions = sources.map((source) => ({ value: source.id, label: `${source.filename} · ${source.file_format} · ${source.chunk_count} passages` }));
  const passageOptions = transcript.map((chunk) => ({ value: String(chunk.id), label: chunkLabel(chunk) }));

  return (
    <>
      <PageHeader
        title="Meeting minutes"
        description="Turn a processed recording into minutes, decisions and follow-up tasks. Every decision and task cites the transcript, and a reviewer approves before tasks are assigned."
        actions={<Button onClick={() => void load()}><RefreshCw /> Refresh</Button>}
      />
      <Alert tone="info" className="mb-5">Access follows the recording’s live permissions. If the recording changes, its old transcript can no longer be cited; revisions and review history are kept.</Alert>
      {error && <Alert tone="danger" className="mb-5">{error}</Alert>}

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
        <div className="grid min-w-0 gap-5">
          <Card className="overflow-hidden">
            <CardHeader icon={<FilePlus2 />} title="New minutes" description="Start from an audio or video recording that has finished processing." />
            <CardBody>
              {sources.length === 0 ? <EmptyState className="py-6" icon={<Mic />} title="No recordings yet" description="Upload a meeting recording in Knowledge. It appears here once its transcript is ready." /> : (
                <form className="grid gap-3" onSubmit={(event) => void createMeeting(event)}>
                  <Field label="Recording"><Select aria-label="Recording" value={selectedSourceId} onValueChange={setSelectedSourceId} options={sourceOptions} /></Field>
                  <Field label="Meeting title"><Input required maxLength={180} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Quarterly operations review" /></Field>
                  <Button type="submit" variant="primary" loading={busy} disabled={busy || !selectedSourceId || !title.trim()}>{!busy && <Plus />} Create minutes draft</Button>
                </form>
              )}
            </CardBody>
          </Card>

          <Card className="overflow-hidden">
            <CardHeader icon={<CalendarClock />} title="Meetings" actions={<Badge>{meetings.length}</Badge>} />
            {meetings.length === 0 ? <EmptyState className="py-8" icon={<CalendarClock />} title="No meeting records" description="Create minutes from a recording to start." /> : (
              <ul className="grid max-h-[520px] overflow-y-auto p-2">
                {meetings.map((meeting) => (
                  <li key={meeting.id}>
                    <button
                      type="button"
                      onClick={() => void openMeeting(meeting.id)}
                      aria-current={selectedId === meeting.id ? "true" : undefined}
                      className={cn("grid w-full cursor-pointer gap-1 rounded-lg px-3 py-2.5 text-left outline-none transition-colors hover:bg-surface-2 focus-visible:ring-[3px] focus-visible:ring-ring", selectedId === meeting.id && "bg-primary-soft hover:bg-primary-soft")}
                    >
                      <span className="flex items-center justify-between gap-2"><strong className="truncate text-[13px] font-medium text-foreground">{meeting.title}</strong><StatusBadge status={meeting.status} /></span>
                      <span className="truncate text-xs text-muted">{meeting.source_filename} · {meeting.owner_email} · rev {meeting.current_revision}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        <div className="grid min-w-0 gap-5">
          {!(selectedId && detail) ? (
            <Card><EmptyState icon={<NotebookPen />} title="No meeting open" description="Open a meeting from the list, or create new minutes from a recording." /></Card>
          ) : (
            <>
              <Card className="overflow-hidden">
                <CardHeader
                  icon={<NotebookPen />}
                  title={detail.title}
                  description={<>Source: {detail.source_filename} · recording version {detail.source_document_version} · {detail.source_is_current ? "current" : "changed since capture"}</>}
                  actions={<StatusBadge status={detail.status} />}
                />
                <CardBody className="grid gap-4">
                  {!detail.source_is_current && <Alert tone="warning">This recording changed. Its transcript can no longer be cited for edits or review. Start a new record from the current recording.</Alert>}
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-xs text-muted">AI drafts are suggestions. Check every cited passage before submitting.</p>
                    {editable && <Button size="sm" loading={busy} disabled={busy || transcript.length === 0} onClick={() => void generateMinutes()}>{!busy && <Sparkles />} Generate from transcript</Button>}
                  </div>
                  <Field label="Minutes">
                    <Textarea rows={10} maxLength={40000} value={minutes} onChange={(event) => setMinutes(event.target.value)} disabled={!editable} placeholder="What was discussed, context and follow-up. This draft is not published to the knowledge base." />
                  </Field>
                </CardBody>
              </Card>

              <Card className="overflow-hidden">
                <CardHeader icon={<Gavel />} title="Decisions" description="Each decision cites a passage from this recording." actions={editable && <Button size="sm" onClick={addDecision}><Plus /> Add decision</Button>} />
                <CardBody className="grid gap-3">
                  {decisions.length === 0 ? <p className="text-xs text-muted">No decisions recorded.</p> : decisions.map((item, index) => (
                    <div className="grid gap-3 rounded-lg border border-border p-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] md:items-end" key={index}>
                      <Field label={`Decision ${index + 1}`}><Input maxLength={2000} value={item.text} disabled={!editable} onChange={(event) => setDecisions((current) => current.map((row, i) => i === index ? { ...row, text: event.target.value } : row))} /></Field>
                      <Field label="Transcript citation"><Select aria-label={`Citation for decision ${index + 1}`} placeholder="Select a source passage" disabled={!editable} value={item.evidence_chunk_ids[0] !== undefined ? String(item.evidence_chunk_ids[0]) : ""} onValueChange={(value) => setDecisions((current) => current.map((row, i) => i === index ? { ...row, evidence_chunk_ids: value ? [Number(value)] : [] } : row))} options={passageOptions} /></Field>
                      {detail.can_edit && <Button size="icon" variant="danger-ghost" aria-label={`Remove decision ${index + 1}`} onClick={() => setDecisions((current) => current.filter((_, i) => i !== index))}><Trash2 /></Button>}
                    </div>
                  ))}
                </CardBody>
              </Card>

              <Card className="overflow-hidden">
                <CardHeader icon={<ListChecks />} title="Proposed tasks" description="Created only after a manager or administrator other than the author approves this revision." actions={editable && <Button size="sm" onClick={addTask}><Plus /> Add task</Button>} />
                <CardBody className="grid gap-3">
                  {tasks.length === 0 ? <p className="text-xs text-muted">No follow-up tasks proposed.</p> : tasks.map((item, index) => (
                    <div className="grid gap-3 rounded-lg border border-border p-3" key={index}>
                      <div className="flex items-center justify-between gap-2">
                        <Badge tone="primary">Task {index + 1}</Badge>
                        {detail.can_edit && <Button size="icon-sm" variant="danger-ghost" aria-label={`Remove task ${index + 1}`} onClick={() => setTasks((current) => current.filter((_, i) => i !== index))}><Trash2 /></Button>}
                      </div>
                      <div className="grid gap-3 md:grid-cols-2">
                        <Field label="Task title"><Input maxLength={180} value={item.title} disabled={!editable} onChange={(event) => setTasks((current) => current.map((row, i) => i === index ? { ...row, title: event.target.value } : row))} /></Field>
                        <Field label="Assignee"><Select aria-label={`Assignee for task ${index + 1}`} placeholder="Someone who can read the recording" disabled={!editable} value={item.assignee_user_id} onValueChange={(value) => setTasks((current) => current.map((row, i) => i === index ? { ...row, assignee_user_id: value } : row))} options={detail.eligible_assignees.map((assignee) => ({ value: assignee.id, label: assignee.email }))} /></Field>
                        <Field label="Task details" className="md:col-span-2"><Textarea rows={3} maxLength={4000} value={item.body} disabled={!editable} onChange={(event) => setTasks((current) => current.map((row, i) => i === index ? { ...row, body: event.target.value } : row))} /></Field>
                        <Field label="Due date"><Input type="date" value={item.due_at?.slice(0, 10) ?? ""} disabled={!editable} onChange={(event) => setTasks((current) => current.map((row, i) => i === index ? { ...row, due_at: event.target.value ? `${event.target.value}T00:00:00Z` : null } : row))} /></Field>
                        <Field label="Transcript citation"><Select aria-label={`Citation for task ${index + 1}`} placeholder="Select a source passage" disabled={!editable} value={item.evidence_chunk_ids[0] !== undefined ? String(item.evidence_chunk_ids[0]) : ""} onValueChange={(value) => setTasks((current) => current.map((row, i) => i === index ? { ...row, evidence_chunk_ids: value ? [Number(value)] : [] } : row))} options={passageOptions} /></Field>
                      </div>
                    </div>
                  ))}
                </CardBody>
              </Card>

              {editable && (
                <div className="flex flex-wrap justify-end gap-2">
                  <Button disabled={busy} onClick={() => void saveDraft()}><Save /> Save new revision</Button>
                  <Button variant="primary" disabled={busy || minutes.trim().length < 10} onClick={() => void submitForReview()}><Send /> Save and submit for review</Button>
                </div>
              )}

              {detail.can_review && (
                <Card className="overflow-hidden">
                  <CardHeader icon={<ShieldCheck />} title="Review this revision" description="Check the minutes and evidence before tasks are assigned. Authors cannot approve their own submission." />
                  <CardBody className="grid gap-3">
                    <Field label="Review rationale"><Textarea rows={3} maxLength={2000} value={rationale} onChange={(event) => setRationale(event.target.value)} placeholder="Explain your approval or the changes needed" /></Field>
                    <div className="flex flex-wrap justify-end gap-2">
                      <Button disabled={busy || rationale.trim().length < 3} onClick={() => void review("changes_requested")}>Request changes</Button>
                      <Button variant="primary" disabled={busy || rationale.trim().length < 3} onClick={() => void review("approved")}><Check /> Approve minutes and tasks</Button>
                    </div>
                  </CardBody>
                </Card>
              )}

              {detail.action_tasks.length > 0 && (
                <Card className="overflow-hidden">
                  <CardHeader icon={<ListChecks />} title="Approved action items" actions={<Badge>{detail.action_tasks.length}</Badge>} />
                  <CardBody className="grid">
                    {detail.action_tasks.map((task) => (
                      <article className="flex flex-col gap-2 border-t border-border py-3 first:border-t-0 sm:flex-row sm:items-center sm:justify-between" key={task.id}>
                        <div className="grid min-w-0 gap-1">
                          <div className="flex flex-wrap items-center gap-2"><strong className="text-[13px] font-medium [overflow-wrap:anywhere]">{task.title}</strong><Badge tone={task.status === "completed" ? "success" : "warning"} dot>{task.status}</Badge></div>
                          {task.body && <p className="text-[13px] whitespace-pre-wrap text-foreground-2">{task.body}</p>}
                          <small className="text-xs text-muted">For {task.assignee_email} · {task.due_at ? formatDate(task.due_at) : "No due date"} · citations {task.evidence_chunk_ids.join(", ")}</small>
                        </div>
                        {task.status === "open" && task.assignee_user_id === user.id && <Button size="sm" variant="primary" disabled={busy} onClick={() => void completeTask(task)}><Check /> Mark complete</Button>}
                      </article>
                    ))}
                  </CardBody>
                </Card>
              )}

              {detail.reviews.length > 0 && (
                <Card className="overflow-hidden">
                  <CardHeader icon={<History />} title="Review history" />
                  <CardBody className="grid gap-2">
                    {detail.reviews.map((item, index) => (
                      <div className="flex flex-wrap items-start gap-2 text-xs" key={`${item.revision_id}-${index}`}>
                        <Badge tone={item.decision === "approved" ? "success" : "warning"}>{item.decision.replace("_", " ")}</Badge>
                        <span className="min-w-0 flex-1 text-foreground-2">Revision {item.revision_id} · {item.reviewer_email} · {formatDate(item.reviewed_at)}<span className="block text-muted">{item.rationale}</span></span>
                      </div>
                    ))}
                  </CardBody>
                </Card>
              )}

              <Card className="overflow-hidden">
                <CardHeader icon={<AudioLines />} title="Transcript evidence" description="Passages you can cite. The number is the citation ID." actions={<Badge>{transcript.length}</Badge>} />
                {transcript.length === 0 ? <CardBody><p className="text-xs text-muted">The current transcript is unavailable.</p></CardBody> : (
                  <div className="grid max-h-96 overflow-y-auto px-5 py-2">
                    {transcript.map((chunk) => (
                      <article className="grid gap-1 border-t border-border py-3 first:border-t-0" key={chunk.id}>
                        <div className="flex items-center gap-2"><Badge tone="primary">#{chunk.id}</Badge><strong className="truncate text-xs font-medium text-foreground">{chunk.locator && typeof chunk.locator.label === "string" ? chunk.locator.label : `Passage ${chunk.id}`}</strong></div>
                        <p className="text-[13px] text-foreground-2">{chunk.content}</p>
                      </article>
                    ))}
                  </div>
                )}
              </Card>
            </>
          )}
        </div>
      </div>
    </>
  );
}

function StatusBadge({ status }: { status: string }) {
  return <Badge tone={status === "approved" ? "success" : status === "submitted" ? "warning" : status === "changes_requested" ? "danger" : "neutral"} dot>{status.replace("_", " ")}</Badge>;
}
