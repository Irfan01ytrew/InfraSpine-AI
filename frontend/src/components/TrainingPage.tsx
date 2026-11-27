import { useEffect, useState } from "react";
import { api, describeError } from "../api";
import { citationPlace, formatDate, formatDay, mediaPreviewHref } from "../format";
import { useFeedback } from "./Feedback";
import { Markdown } from "./Markdown";
import { AlarmClock, BookOpen, Check, CheckCircle2, ClipboardCheck, GraduationCap, Printer, RefreshCw, Repeat, Sparkles, Users } from "lucide-react";
import { cn } from "../lib/utils";
import type {
  DepartmentGroup,
  TrainingAssignment,
  TrainingPlan,
  TrainingPracticeHistoryItem,
  TrainingPracticeResult,
  TrainingPracticeScenario,
  TrainingQuizAttemptSummary,
  TrainingProgressItem,
  TrainingQuizResult,
  TrainingReminder,
  User,
} from "../types";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, Input, PageHeader, SelectInput, Stat, Textarea } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

export function TrainingPage({ user }: { user: User }) {
  const { toast } = useFeedback();
  const [topic, setTopic] = useState("");
  const [plan, setPlan] = useState<TrainingPlan | null>(null);
  const [history, setHistory] = useState<TrainingProgressItem[]>([]);
  const [historyError, setHistoryError] = useState("");
  const [busy, setBusy] = useState(false);
  const [quizAnswers, setQuizAnswers] = useState<number[]>([]);
  const [quizResult, setQuizResult] = useState<TrainingQuizResult | null>(null);
  const [quizAttempts, setQuizAttempts] = useState<TrainingQuizAttemptSummary[]>([]);
  const [practiceHistory, setPracticeHistory] = useState<TrainingPracticeHistoryItem[]>([]);
  const [practiceScenario, setPracticeScenario] = useState<TrainingPracticeScenario | null>(null);
  const [practiceAnswers, setPracticeAnswers] = useState<number[]>([]);
  const [practiceResult, setPracticeResult] = useState<TrainingPracticeResult | null>(null);
  const [practiceError, setPracticeError] = useState("");
  const [departments, setDepartments] = useState<DepartmentGroup[]>([]);
  const [departmentId, setDepartmentId] = useState("");
  const [employees, setEmployees] = useState<Array<{ user_id: string; email: string }>>([]);
  const [selectedEmployees, setSelectedEmployees] = useState<string[]>([]);
  const [dueDate, setDueDate] = useState("");
  const [assignments, setAssignments] = useState<TrainingAssignment[]>([]);
  const [assignmentError, setAssignmentError] = useState("");
  const [reminders, setReminders] = useState<TrainingReminder[]>([]);
  const [reminderError, setReminderError] = useState("");
  const canAssign = user.role === "admin" || user.role === "manager" || Boolean(user.capabilities?.includes("training.manage"));
  const isManager = user.role === "manager";

  async function refreshHistory() {
    try {
      setHistory((await api.trainingHistory()).items);
      setHistoryError("");
    } catch (err) {
      setHistoryError(describeError(err));
    }
  }

  async function refreshReminders() {
    try {
      setReminders((await api.trainingReminders()).reminders);
      setReminderError("");
    } catch (err) {
      setReminderError(describeError(err));
    }
  }

  useEffect(() => {
    void refreshHistory();
    void refreshReminders();
  }, []);
  useLiveRefresh(() => Promise.all([refreshHistory(), refreshReminders()]));

  useEffect(() => {
    if (!canAssign) return;
    void (async () => {
      try {
        // Departments this account may assign to: all (admin), managed ones, or those in its training.manage scope.
        const groups: DepartmentGroup[] = (await api.trainingDepartments()).departments.map((group) => ({
          id: group.id, name: group.name, kind: "department", source: "local", external_id: null, member_count: 0, created_at: "",
        }));
        setDepartments(groups);
        setDepartmentId((current) => groups.some((group) => group.id === current) ? current : groups[0]?.id ?? "");
        const lists = await Promise.all(groups.map((group) => api.trainingAssignments(group.id)));
        setAssignments(lists.flatMap((list) => list.assignments));
        setAssignmentError("");
      } catch (err) {
        setAssignmentError(describeError(err));
      }
    })();
  }, [canAssign, isManager]);

  useEffect(() => {
    if (!canAssign || !departmentId) {
      setEmployees([]);
      setSelectedEmployees([]);
      return;
    }
    void (async () => {
      try {
        const members = (await api.trainingAssignmentCandidates(departmentId)).employees;
        setEmployees(members);
        setSelectedEmployees((current) => current.filter((id) => members.some((member) => member.user_id === id)));
      } catch (err) {
        setAssignmentError(describeError(err));
      }
    })();
  }, [canAssign, departmentId, isManager]);

  async function reloadAssignments() {
    if (!departments.length) return;
    try {
      const lists = await Promise.all(departments.map((group) => api.trainingAssignments(group.id)));
      setAssignments(lists.flatMap((list) => list.assignments));
      setAssignmentError("");
    } catch (err) {
      setAssignmentError(describeError(err));
    }
  }

  function showPlan(nextPlan: TrainingPlan) {
    setPlan(nextPlan);
    setQuizAnswers(Array(nextPlan.quiz.length).fill(-1));
    setQuizResult(null);
    setQuizAttempts([]);
    setPracticeHistory([]);
    setPracticeScenario(null);
    setPracticeAnswers([]);
    setPracticeResult(null);
    setPracticeError("");
  }

  async function openTraining(id: string) {
    setBusy(true);
    try {
      const nextPlan = await api.trainingPlan(id);
      showPlan(nextPlan);
      if (nextPlan.id) {
        setQuizAttempts((await api.trainingQuizAttempts(nextPlan.id)).attempts);
        try {
          setPracticeHistory((await api.trainingPracticeHistory(nextPlan.id)).scenarios);
        } catch (err) {
          setPracticeError(describeError(err));
        }
      }
    } catch (err) {
      toast("error", "Training plan could not be opened", describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function openReminder(reminder: TrainingReminder) {
    try {
      const read = await api.readTrainingReminder(reminder.id);
      setReminders((items) => items.map((item) => item.id === reminder.id ? read : item));
      await openTraining(reminder.course_id);
    } catch (err) {
      toast("error", "Training reminder could not be opened", describeError(err));
    }
  }

  async function createPlan(event: React.FormEvent) {
    event.preventDefault();
    const cleanTopic = topic.trim();
    if (cleanTopic.length < 2 || busy) return;
    setBusy(true);
    setPlan(null);
    try {
      const created = await api.trainingDocuments(cleanTopic);
      showPlan(created);
      await refreshHistory();
    } catch (err) {
      toast("error", "Training material could not be created", describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function submitQuiz(event: React.FormEvent) {
    event.preventDefault();
    if (!plan?.id || quizAnswers.some((answer) => answer < 0) || busy) return;
    setBusy(true);
    try {
      const result = await api.submitTrainingQuiz(plan.id, quizAnswers);
      setQuizResult(result);
      setQuizAttempts((attempts) => [result, ...attempts].slice(0, 50));
      toast(result.passed ? "success" : "info", result.passed ? "Quiz passed" : "Keep learning", `${result.score} of ${result.max_score} correct`);
    } catch (err) {
      toast("error", "Quiz could not be submitted", describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function createPractice() {
    if (!plan?.id || !plan.quiz.length || busy) return;
    setBusy(true);
    setPracticeError("");
    setPracticeResult(null);
    try {
      const scenario = await api.createTrainingPractice(plan.id);
      setPracticeScenario(scenario);
      setPracticeAnswers(Array(scenario.questions.length).fill(-1));
      try {
        setPracticeHistory((await api.trainingPracticeHistory(plan.id)).scenarios);
      } catch (err) {
        setPracticeError(`Practice is ready, but its history could not refresh: ${describeError(err)}`);
      }
    } catch (err) {
      setPracticeError(describeError(err));
      toast("error", "Practice could not be prepared", describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function submitPractice(event: React.FormEvent) {
    event.preventDefault();
    if (!plan?.id || !practiceScenario || practiceAnswers.some((answer) => answer < 0) || busy) return;
    setBusy(true);
    setPracticeError("");
    try {
      const result = await api.submitTrainingPractice(plan.id, practiceScenario.scenario_id, practiceAnswers);
      setPracticeResult(result);
      try {
        setPracticeHistory((await api.trainingPracticeHistory(plan.id)).scenarios);
      } catch (err) {
        setPracticeError(`Your result was saved, but its history could not refresh: ${describeError(err)}`);
      }
      toast(result.passed ? "success" : "info", result.passed ? "Practice complete" : "Review these topics", `${result.score} of ${result.max_score} correct; this practice does not certify course completion.`);
    } catch (err) {
      setPracticeError(describeError(err));
      toast("error", "Practice answers could not be saved", describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function completeTraining() {
    if (!plan?.id || plan.status === "completed" || busy) return;
    setBusy(true);
    try {
      const progress = await api.completeTraining(plan.id);
      setPlan({ ...plan, status: progress.status, completed_at: progress.completed_at });
      setHistory((items) => items.map((item) => item.id === progress.id ? progress : item));
      toast("success", "Training marked complete", "Your progress is saved to your account.");
    } catch (err) {
      toast("error", "Training progress could not be saved", describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function assignPlan(event: React.FormEvent) {
    event.preventDefault();
    if (!plan?.id || !departmentId || selectedEmployees.length === 0 || busy) return;
    setBusy(true);
    try {
      const dueAt = dueDate ? `${dueDate}T23:59:59Z` : undefined;
      const result = await api.assignTraining(plan.id, departmentId, selectedEmployees, dueAt);
      toast("success", "Training assigned", `${result.created} new employee assignment${result.created === 1 ? "" : "s"} created.`);
      await reloadAssignments();
    } catch (err) {
      toast("error", "Training could not be assigned", describeError(err));
    } finally {
      setBusy(false);
    }
  }

  function toggleEmployee(id: string) {
    setSelectedEmployees((current) => current.includes(id) ? current.filter((value) => value !== id) : [...current, id]);
  }

  const quizPassed = Boolean(quizResult?.passed || quizAttempts.some((attempt) => attempt.passed));
  const completedCount = history.filter((item) => item.status === "completed").length;

  return (
    <>
      <PageHeader
        title="Training"
        description="Turn company documents into lessons with scored quizzes. Every lesson cites its sources, and access is re-checked each time a lesson is opened or scored."
      />
      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat icon={<BookOpen />} label="My training plans" value={history.length} />
        <Stat icon={<CheckCircle2 />} label="Completed" value={completedCount} />
        <Stat icon={<AlarmClock />} label="Deadlines needing attention" value={reminders.length} />
        <Stat icon={<Users />} label={canAssign ? "Team assignments" : "Assigned to me"} value={canAssign ? assignments.length : history.filter((item) => item.assigned).length} />
      </div>

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
        <div className="grid min-w-0 gap-5">
          <Card className="overflow-hidden">
            <CardHeader icon={<Sparkles />} title="Create a lesson" description="Lessons and quiz answers use only documents you can read. Anything the documents do not support is left out." />
            <CardBody>
              <form className="grid gap-3" onSubmit={(event) => void createPlan(event)}>
                <Field label="What should people learn?">
                  <Textarea value={topic} onChange={(event) => setTopic(event.target.value)} maxLength={2000} rows={3} placeholder="For example: safely hand over an active infrastructure incident" />
                </Field>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-xs text-muted">{topic.trim().length}/2000</span>
                  <Button type="submit" variant="primary" loading={busy} disabled={busy || topic.trim().length < 2}>{!busy && <Sparkles />} Create training</Button>
                </div>
              </form>
            </CardBody>
          </Card>

          {busy && <Alert tone="info" title="Working…">Preparing or saving source-backed training progress.</Alert>}

          {!plan && !busy && (
            <Card><EmptyState icon={<GraduationCap />} title="No lesson open" description="Create a lesson above, or open one from My training plans." /></Card>
          )}

          {plan && (
            <section className="grid gap-4" aria-label="Training plan">
              <Card className="overflow-hidden">
                <CardHeader
                  icon={<GraduationCap />}
                  title={plan.topic}
                  description={<>Grounded in readable documents · {plan.provider ?? "configured provider"} {plan.model ?? ""}</>}
                  actions={<div className="training-plan-actions flex flex-wrap items-center gap-2">
                    {plan.status === "completed" && <Badge tone="success" dot>Completed</Badge>}
                    <Button size="sm" variant="ghost" onClick={() => window.print()}><Printer /> Print</Button>
                    {plan.id && plan.status === "in_progress" && (!plan.quiz_required || (plan.quiz.length > 0 && quizPassed)) && <Button size="sm" variant="primary" loading={busy} onClick={() => void completeTraining()}>{!busy && <Check />} Mark as completed</Button>}
                  </div>}
                />
                {plan.id && plan.status === "in_progress" && plan.quiz_required && plan.quiz.length > 0 && !quizPassed && <div className="px-5 pt-4"><Alert tone="info">Pass the quiz below (70% or more) to complete this plan.</Alert></div>}
                {plan.quiz_required && plan.quiz.length === 0 && <div className="px-5 pt-4"><Alert tone="warning">The quiz is hidden because a source changed or your access was removed. Recreate the lesson with current sources.</Alert></div>}
                <CardBody className="grid gap-4">
                  {plan.modules.map((module, index) => (
                    <article className="rounded-lg border border-border p-4" key={`${index}-${module.title}`}>
                      <div className="mb-2 flex flex-wrap items-center gap-2">
                        <Badge tone="primary">Module {index + 1}</Badge>
                        <Badge tone={module.insufficient_evidence ? "warning" : "success"} dot>{module.insufficient_evidence ? "Evidence gap" : "Source-backed"}</Badge>
                      </div>
                      <h3 className="mb-2 text-sm font-semibold text-foreground">{module.title}</h3>
                      <div className="research-answer text-[13px] leading-relaxed"><Markdown text={module.answer} renderCitation={(tag, key) => <span className="source-tag" key={key}>{tag}</span>} /></div>
                      {module.citations.length > 0 && (
                        <div className="mt-3 grid gap-2 border-t border-border pt-3">
                          <strong className="text-xs font-semibold text-muted uppercase">Sources</strong>
                          {module.citations.map((citation) => (
                            <div key={`${citation.document_id}-${citation.chunk_index}`} className="flex min-w-0 gap-2 text-xs">
                              <span className="source-tag shrink-0">{citation.source_id}</span>
                              <span className="grid min-w-0 gap-0.5">
                                <strong className="font-medium [overflow-wrap:anywhere]">{citation.filename}</strong>
                                <small className="text-muted">{citation.location ?? `Page ${citation.page_number}`}</small>
                                <small className="text-foreground-2 [overflow-wrap:anywhere]">{citation.snippet}</small>
                                {citation.available && mediaPreviewHref(citation) && <a className="text-primary underline" href={mediaPreviewHref(citation) ?? undefined} target="_blank" rel="noopener noreferrer">Open original media at {citationPlace(citation)}</a>}
                              </span>
                            </div>
                          ))}
                        </div>
                      )}
                    </article>
                  ))}
                </CardBody>
              </Card>

              {plan.quiz.length > 0 && (
                <Card className="overflow-hidden" aria-labelledby="training-quiz-title">
                  <CardHeader icon={<ClipboardCheck />} title={<span id="training-quiz-title">Knowledge check</span>} description="Answers and explanations appear after you submit. The pass mark is 70%." />
                  <CardBody className="grid gap-4">
                    <form className="grid gap-4" onSubmit={(event) => void submitQuiz(event)}>
                      {plan.quiz.map((question, questionIndex) => (
                        <QuestionCard
                          key={`${questionIndex}-${question.question}`}
                          number={questionIndex + 1}
                          question={question}
                          name={`training-question-${questionIndex}`}
                          value={quizAnswers[questionIndex]}
                          onChange={(choiceIndex) => setQuizAnswers((answers) => answers.map((answer, index) => index === questionIndex ? choiceIndex : answer))}
                        />
                      ))}
                      {plan.id && <div><Button type="submit" variant="primary" disabled={busy || quizAnswers.some((answer) => answer < 0)}>Submit answers</Button></div>}
                    </form>
                    {quizResult && <ResultBox passed={quizResult.passed} summary={`${quizResult.passed ? "Passed" : "Not passed yet"}: ${quizResult.score}/${quizResult.max_score} (${quizResult.percentage}%)`} feedback={quizResult.feedback} />}
                    {quizAttempts.length > 0 && (
                      <div className="grid gap-1">
                        <strong className="text-xs font-semibold text-muted uppercase">Saved attempts</strong>
                        {quizAttempts.map((attempt) => (
                          <div className={rowClass} key={attempt.attempt_id}>
                            <span className="flex items-center gap-2 text-[13px]"><Badge tone={attempt.passed ? "success" : "warning"} dot>{attempt.passed ? "Passed" : "Not passed"}</Badge>{attempt.score}/{attempt.max_score} ({attempt.percentage}%)</span>
                            <span className="text-xs text-muted">{formatDate(attempt.created_at)}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </CardBody>
                </Card>
              )}

              {plan.id && plan.quiz.length > 0 && (
                <Card className="overflow-hidden" aria-labelledby="adaptive-practice-title">
                  <CardHeader
                    icon={<Repeat />}
                    title={<span id="adaptive-practice-title">Practice</span>}
                    description="A short review built from this quiz, focusing on recent mistakes and less-practised questions. Practice never counts as course completion."
                    actions={<Button size="sm" variant="primary" loading={busy} onClick={() => void createPractice()}>{practiceResult ? "Practice again" : "Start short practice"}</Button>}
                  />
                  <CardBody className="grid gap-4">
                    {practiceError && <Alert tone="danger">{practiceError}</Alert>}
                    {practiceScenario && !practiceResult && (
                      <form className="grid gap-4" onSubmit={(event) => void submitPractice(event)}>
                        {practiceScenario.questions.map((item, questionPosition) => (
                          <QuestionCard
                            key={`${item.question_index}-${item.question.question}`}
                            number={questionPosition + 1}
                            question={item.question}
                            name={`practice-question-${item.question_index}`}
                            value={practiceAnswers[questionPosition]}
                            onChange={(choiceIndex) => setPracticeAnswers((answers) => answers.map((answer, index) => index === questionPosition ? choiceIndex : answer))}
                          />
                        ))}
                        <div><Button type="submit" variant="primary" disabled={busy || practiceAnswers.some((answer) => answer < 0)}>Submit practice</Button></div>
                      </form>
                    )}
                    {practiceResult && <ResultBox passed={practiceResult.passed} summary={`${practiceResult.score}/${practiceResult.max_score} correct (${practiceResult.percentage}%). This result is not a certification attempt.`} feedback={practiceResult.feedback} />}
                    {practiceHistory.length > 0 ? (
                      <div className="grid gap-1" aria-label="Adaptive practice history">
                        <strong className="text-xs font-semibold text-muted uppercase">Practice history</strong>
                        {practiceHistory.map((item) => (
                          <div className={rowClass} key={item.scenario_id}>
                            <span className="text-[13px]">{item.completed_at && item.score !== null && item.max_score !== null
                              ? `${item.score}/${item.max_score} correct (${item.percentage}%)`
                              : "Not submitted"} · {item.question_indexes.length} question{item.question_indexes.length === 1 ? "" : "s"}</span>
                            <span className="text-xs text-muted">{formatDate(item.created_at)}</span>
                          </div>
                        ))}
                      </div>
                    ) : !practiceScenario && <p className="text-xs text-muted">No practice yet.</p>}
                  </CardBody>
                </Card>
              )}
            </section>
          )}
        </div>

        <div className="grid min-w-0 gap-5">
          <Card className="overflow-hidden" aria-labelledby="training-history-title">
            <CardHeader icon={<BookOpen />} title={<span id="training-history-title">My training plans</span>} description="Assigned and personal lessons." actions={<Button size="icon-sm" variant="ghost" aria-label="Refresh training plans" onClick={() => void refreshHistory()}><RefreshCw /></Button>} />
            <CardBody>
              {historyError && <Alert tone="danger">{historyError}</Alert>}
              {!historyError && history.length === 0 && <EmptyState className="py-8" icon={<BookOpen />} title="No plans yet" description="Create a lesson, or ask your manager to assign one." />}
              {history.map((item) => (
                <div className={rowClass} key={`${item.id}-${item.assignment_id ?? "personal"}`}>
                  <div className="grid min-w-0 gap-1">
                    <strong className="text-[13px] font-medium [overflow-wrap:anywhere]">{item.topic}</strong>
                    <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted">
                      <Badge tone={item.status === "completed" ? "success" : "warning"} dot>{item.status === "completed" ? "Completed" : "In progress"}</Badge>
                      <span>{item.module_count} modules · {item.assigned ? "Assigned" : "Personal"}{item.due_at ? ` · Due ${formatDay(item.due_at)}` : ""}</span>
                    </div>
                  </div>
                  <Button size="sm" disabled={busy} onClick={() => void openTraining(item.id)}>{item.status === "completed" ? "Review" : "Continue"}</Button>
                </div>
              ))}
            </CardBody>
          </Card>

          <Card className="overflow-hidden" aria-labelledby="training-reminders-title">
            <CardHeader icon={<AlarmClock />} title={<span id="training-reminders-title">Deadlines</span>} description="Assignments due within three days, or overdue." actions={<Button size="icon-sm" variant="ghost" aria-label="Refresh training reminders" onClick={() => void refreshReminders()}><RefreshCw /></Button>} />
            <CardBody>
              {reminderError && <Alert tone="danger">{reminderError}</Alert>}
              {!reminderError && reminders.length === 0 && <p className="text-xs text-muted">No training deadlines need attention.</p>}
              {reminders.map((reminder) => (
                <div className={rowClass} key={reminder.id}>
                  <div className="grid min-w-0 gap-1">
                    <strong className="text-[13px] font-medium [overflow-wrap:anywhere]">{reminder.topic}</strong>
                    <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted">
                      <Badge tone={reminder.reminder_type === "overdue" ? "danger" : "warning"} dot>{reminder.reminder_type === "overdue" ? "Overdue" : "Due soon"}</Badge>
                      <span>Due {formatDate(reminder.due_at)} · {reminder.read_at ? "Opened" : "New"}</span>
                    </div>
                  </div>
                  <Button size="sm" disabled={busy} onClick={() => void openReminder(reminder)}>Open</Button>
                </div>
              ))}
            </CardBody>
          </Card>

          {canAssign && (
            <Card className="overflow-hidden" aria-labelledby="training-assignment-title">
              <CardHeader icon={<Users />} title={<span id="training-assignment-title">Assign to a department</span>} description="Everyone picked must be able to read the lesson sources." actions={<Button size="icon-sm" variant="ghost" aria-label="Refresh assignments" onClick={() => void reloadAssignments()}><RefreshCw /></Button>} />
              <CardBody className="grid gap-4">
                {assignmentError && <Alert tone="danger">{assignmentError}</Alert>}
                {plan?.id ? (
                  <form className="training-assign-form grid gap-3" onSubmit={(event) => void assignPlan(event)}>
                    <p className="text-xs text-muted">Assigning: <strong className="text-foreground">{plan.topic}</strong></p>
                    <div className="grid gap-3 sm:grid-cols-2">
                      <Field label="Department"><SelectInput value={departmentId} onChange={(event) => setDepartmentId(event.target.value)} disabled={!departments.length}><option value="">Select a department</option>{departments.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</SelectInput></Field>
                      <Field label="Due date (optional)"><Input type="date" value={dueDate} onChange={(event) => setDueDate(event.target.value)} /></Field>
                    </div>
                    <div className="training-employee-picker grid max-h-56 gap-1 overflow-y-auto rounded-lg border border-border p-2" role="group" aria-label="Employees to assign">
                      {employees.length ? employees.map((employee) => (
                        <label key={employee.user_id} className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-[13px] hover:bg-surface-2">
                          <input className="size-4 accent-[var(--primary)]" type="checkbox" checked={selectedEmployees.includes(employee.user_id)} onChange={() => toggleEmployee(employee.user_id)} />
                          <span className="min-w-0 truncate">{employee.email}</span>
                        </label>
                      )) : <span className="px-2 py-1.5 text-xs text-muted">Select a department with active employees.</span>}
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Button type="submit" variant="primary" disabled={busy || !plan.quiz.length || !selectedEmployees.length}>Assign to {selectedEmployees.length || "…"} employee{selectedEmployees.length === 1 ? "" : "s"}</Button>
                      {!plan.quiz.length && <p className="text-xs text-muted">This lesson needs a validated, source-cited quiz before it can be assigned.</p>}
                    </div>
                  </form>
                ) : <Alert tone="info">Open or create a lesson first, then assign it here.</Alert>}
                {assignments.length === 0 ? <p className="text-xs text-muted">No department training assignments yet.</p> : (
                  <div className="training-assignment-list grid">
                    {assignments.map((item) => (
                      <div className={rowClass} key={item.assignment_id}>
                        <div className="grid min-w-0 gap-1">
                          <strong className="text-[13px] font-medium [overflow-wrap:anywhere]">{item.topic}</strong>
                          <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted">
                            <Badge tone={item.status === "completed" ? "success" : "neutral"} dot>{item.status === "completed" ? "Completed" : "Assigned"}</Badge>
                            <span className="[overflow-wrap:anywhere]">{item.assignee_email}{item.due_at ? ` · Due ${formatDay(item.due_at)}` : ""}{item.latest_score !== null && item.latest_max_score ? ` · Latest score ${item.latest_score}/${item.latest_max_score}` : ""}</span>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </CardBody>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}

const rowClass = "training-history-row flex flex-wrap items-center justify-between gap-2 border-t border-border py-2.5 first:border-t-0";

type QuizQuestion = TrainingPlan["quiz"][number];

function QuestionCard({ number, question, name, value, onChange }: {
  number: number; question: QuizQuestion; name: string; value: number | undefined; onChange: (choiceIndex: number) => void;
}) {
  return (
    <fieldset className="training-quiz-question grid gap-2 rounded-lg border border-border p-4">
      <legend className="px-1 text-[13px] font-semibold text-foreground">{number}. {question.question}</legend>
      {question.choices.map((choice, choiceIndex) => (
        <label key={`${choiceIndex}-${choice}`} className={cn("flex cursor-pointer items-start gap-2.5 rounded-md border px-3 py-2 text-[13px] transition-colors", value === choiceIndex ? "border-primary bg-primary-soft" : "border-border hover:bg-surface-2")}>
          <input className="mt-0.5 size-4 accent-[var(--primary)]" type="radio" name={name} checked={value === choiceIndex} onChange={() => onChange(choiceIndex)} />
          <span>{choice}</span>
        </label>
      ))}
      <div className="text-xs text-muted">Sources: {question.citations.map((citation) => `${citation.source_id} ${citation.filename}${citation.location ? `, ${citation.location}` : `, page ${citation.page_number}`}`).join(" · ")}</div>
      {question.citations.map((citation) => {
        const preview = citation.available ? mediaPreviewHref(citation) : null;
        return preview ? <a key={`${citation.document_id}-${citation.chunk_index}`} className="text-xs text-primary underline" href={preview} target="_blank" rel="noopener noreferrer">Open original media at {citationPlace(citation)}</a> : null;
      })}
    </fieldset>
  );
}

function ResultBox({ passed, summary, feedback }: { passed: boolean; summary: string; feedback: { correct: boolean; correct_choice: number; explanation: string }[] }) {
  return (
    <div className={cn("training-quiz-result grid gap-2 rounded-lg border px-4 py-3 text-[13px]", passed ? "border-success/30 bg-success-soft" : "border-warning/30 bg-warning-soft")} role="status">
      <strong className={passed ? "text-success-text" : "text-warning-text"}>{summary}</strong>
      {feedback.map((item, index) => (
        <p key={index} className="text-foreground-2"><strong className="text-foreground">Question {index + 1}: {item.correct ? "Correct" : `Correct choice ${item.correct_choice + 1}`}</strong> · {item.explanation}</p>
      ))}
    </div>
  );
}
