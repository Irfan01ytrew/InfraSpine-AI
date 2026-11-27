import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api, describeError } from "../api";
import { citationPlace, formatDate, mediaPreviewHref } from "../format";
import type { AgentCatalogTemplate, AgentDepartment, AgentKnowledgeScope, AgentProfile, AgentRunRecord, AgentRunResponse, AgentTool, AgentType, Citation, User } from "../types";
import { AgentCatalogReviewSection } from "./AgentCatalogReviewSection";
import { AgentRunRetentionPanel } from "./AgentRunRetentionPanel";
import { Ban, BarChart3, BookOpen, Bot, Building2, CheckCircle2, Globe2, GraduationCap, MoreHorizontal, Pencil, Play, Plus, RefreshCw, RotateCw, X } from "lucide-react";
import { cn } from "../lib/utils";
import { Alert, Badge, Button, Card, CardBody, CardHeader, Checkbox, Dialog, DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger, EmptyState, Field, Input, PageHeader, Select, Textarea } from "./ui";
import { Markdown } from "./Markdown";
import { useLiveRefresh } from "../lib/useLiveRefresh";

const TOOLS: Record<AgentType, Array<{ id: AgentTool; label: string }>> = {
  knowledge_research: [{ id: "knowledge_search", label: "Search company knowledge" }],
  market_research: [
    { id: "knowledge_search", label: "Search company knowledge" },
    { id: "public_web_research", label: "Public web research (existing consent required)" },
  ],
  company_analysis: [
    { id: "knowledge_search", label: "Search company knowledge" },
    { id: "company_data_analysis", label: "Company data analysis" },
  ],
  employee_training: [
    { id: "knowledge_search", label: "Search company knowledge" },
    { id: "training_plan", label: "Employee training plans" },
  ],
};

const TYPE_LABELS: Record<AgentType, string> = {
  knowledge_research: "Knowledge research",
  market_research: "Market research",
  company_analysis: "Company data analysis",
  employee_training: "Employee training",
};

const TYPE_OPTIONS = Object.keys(TYPE_LABELS) as AgentType[];
const TOOL_LABELS: Record<AgentTool, string> = {
  knowledge_search: "Search company knowledge",
  public_web_research: "Public web research",
  company_data_analysis: "Company data analysis",
  training_plan: "Employee training plan",
};

function AgentRunOutput({ result }: { result: AgentRunResponse["result"] }) {
  if ("modules" in result) {
    return <div className="agent-run-output">
      <h3>Training plan: {result.topic}</h3>
      {result.modules.map((module, index) => <article className="research-finding" key={`${index}-${module.title}`}>
        <h4>{module.title}</h4>
        <Markdown text={module.answer} renderCitation={(tag, key) => <span className="source-tag" key={key}>{tag}</span>} />
        <RunCitationList citations={module.citations} />
      </article>)}
      {result.quiz.length > 0 && <p className="muted">This plan includes {result.quiz.length} cited quiz question{result.quiz.length === 1 ? "" : "s"}.</p>}
    </div>;
  }
  if ("sources" in result) {
    return <div className="agent-run-output">
      <Markdown text={result.answer} renderCitation={(tag, key) => {
        const match = tag.match(/^S(\d+)$/);
        const source = match ? result.sources[Number(match[1]) - 1] : undefined;
        return source ? <a className="source-tag" key={key} href={source.url} target="_blank" rel="noopener noreferrer" title={source.title}>{tag}</a> : <span key={key}>{tag}</span>;
      }} />
      {result.sources.length > 0 && <div className="research-sources"><strong>Public sources</strong>{result.sources.map((source) => <div className="research-source" key={source.url}><a href={source.url} target="_blank" rel="noopener noreferrer">{source.title}</a></div>)}</div>}
    </div>;
  }
  return <div className="agent-run-output">
    {result.insufficient_evidence && <Alert tone="warning" className="mb-2">The available documents did not support a complete answer.</Alert>}
    <Markdown text={result.answer} renderCitation={(tag, key) => <span className="source-tag" key={key}>{tag}</span>} />
    <RunCitationList citations={result.citations} />
  </div>;
}

function RunCitationList({ citations }: { citations: Citation[] }) {
  if (!citations.length) return null;
  return <div className="research-sources"><strong>Sources</strong>{citations.map((citation) => <div className="research-source" key={`${citation.document_id}-${citation.chunk_index}`}>
    <span className="source-tag">{citation.source_id}</span><span><strong>{citation.filename}</strong><small>{citation.location ?? `Page ${citation.page_number}`}</small><small>{citation.snippet}</small>
      {citation.available && mediaPreviewHref(citation) && <a className="source-page-link" href={mediaPreviewHref(citation) ?? undefined} target="_blank" rel="noopener noreferrer">Open original media at {citationPlace(citation)}</a>}
      {citation.available && (citation.locator?.kind === "page" || citation.locator?.kind === "image") && <a className="source-page-link" href={`/api/rag/documents/${encodeURIComponent(citation.document_id)}/pages/${citation.page_number}/image`} target="_blank" rel="noopener noreferrer">Open cited source ↗</a>}
    </span>
  </div>)}</div>;
}

export function AgentCatalogPage({ user }: { user: User }) {
  const [agents, setAgents] = useState<AgentProfile[]>([]);
  const [departments, setDepartments] = useState<AgentDepartment[]>([]);
  const [name, setName] = useState("");
  const [departmentId, setDepartmentId] = useState("");
  const [agentType, setAgentType] = useState<AgentType>("knowledge_research");
  const [knowledgeScope, setKnowledgeScope] = useState<AgentKnowledgeScope>("requester_acl");
  const [toolPolicy, setToolPolicy] = useState<AgentTool[]>(["knowledge_search"]);
  const [editing, setEditing] = useState<AgentProfile | null>(null);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<AgentProfile | null>(null);
  const [runCapability, setRunCapability] = useState<AgentTool>("knowledge_search");
  const [runTask, setRunTask] = useState("");
  const [confirmExternalSearch, setConfirmExternalSearch] = useState(false);
  const [runBusy, setRunBusy] = useState(false);
  const [runResult, setRunResult] = useState<AgentRunResponse | null>(null);
  const [runHistory, setRunHistory] = useState<AgentRunRecord[]>([]);
  const [historyBusy, setHistoryBusy] = useState(false);
  const allowedTools = useMemo(() => TOOLS[agentType], [agentType]);

  async function refresh() {
    try {
      const [profiles, groups] = await Promise.all([api.agentProfiles(), api.agentDepartments()]);
      setAgents(profiles.agents);
      setDepartments(groups.departments);
      setDepartmentId((current) => current || groups.departments[0]?.id || "");
      setError(null);
    } catch (err) {
      setError(describeError(err));
    }
  }

  useEffect(() => { void refresh(); }, []);
  useLiveRefresh(refresh);

  function resetForm() {
    setEditing(null);
    setName("");
    setAgentType("knowledge_research");
    setKnowledgeScope("requester_acl");
    setToolPolicy(["knowledge_search"]);
  }

  function startEdit(profile: AgentProfile) {
    setEditing(profile);
    setName(profile.name);
    setDepartmentId(profile.department_group_id);
    setAgentType(profile.agent_type);
    setKnowledgeScope(profile.knowledge_scope);
    setToolPolicy(profile.tool_policy);
    setNotice(null);
  }

  function changeType(next: AgentType) {
    setAgentType(next);
    setToolPolicy((current) => current.filter((tool) => TOOLS[next].some((item) => item.id === tool)));
  }

  function useReviewedTemplate(template: AgentCatalogTemplate) {
    resetForm();
    setName(template.name);
    setAgentType(template.agent_type);
    setKnowledgeScope(template.knowledge_scope);
    setToolPolicy([...template.tool_policy]);
    setNotice(`Reviewed template ${template.slug} v${template.version} copied into the new agent form. Choose a department and save.`);
  }

  function toggleTool(tool: AgentTool, checked: boolean) {
    setToolPolicy((current) => checked ? [...new Set([...current, tool])] : current.filter((item) => item !== tool));
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (editing) {
        await api.versionAgentProfile(editing.id, {
          expected_version: editing.version, name, agent_type: agentType,
          knowledge_scope: knowledgeScope, tool_policy: toolPolicy,
        });
        setNotice("A new immutable version was created; the previous version remains in history.");
      } else {
        await api.createAgentProfile({
          name, department_group_id: departmentId, agent_type: agentType,
          knowledge_scope: knowledgeScope, tool_policy: toolPolicy,
        });
        setNotice("Agent profile saved.");
      }
      resetForm();
      setCreating(false);  // close the dialog only after a successful save
      await refresh();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function disable(profile: AgentProfile) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.disableAgentProfile(profile.id);
      setNotice("Agent profile disabled. Its history is retained.");
      if (editing?.id === profile.id) resetForm();
      await refresh();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  function selectForRun(profile: AgentProfile) {
    setSelectedRun(profile);
    setRunCapability(profile.tool_policy[0]);
    setRunTask("");
    setConfirmExternalSearch(false);
    setRunResult(null);
    setRunHistory([]);
    setError(null);
    setNotice(null);
    void api.agentProfileRuns(profile.id).then((history) => setRunHistory(history.runs)).catch((err) => setError(describeError(err)));
  }

  async function runAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedRun) return;
    setRunBusy(true);
    setError(null);
    setNotice(null);
    setRunResult(null);
    try {
      if (runCapability === "knowledge_search" || runCapability === "company_data_analysis") {
        const queued = await api.queueAgentRunProfile(selectedRun.id, { capability: runCapability, task: runTask });
        setRunHistory((current) => [queued, ...current.filter((item) => item.run_id !== queued.run_id)].slice(0, 20));
        setNotice("Agent run saved to the department queue.");
        let current = queued;
        for (let poll = 0; poll < 90; poll += 1) {
          await new Promise((resolve) => window.setTimeout(resolve, 1000));
          current = await api.agentRun(queued.run_id);
          setRunHistory((history) => [current, ...history.filter((item) => item.run_id !== current.run_id)].slice(0, 20));
          if (current.status === "completed") {
            if (!current.result) throw new Error("The worker completed the run without a saved result.");
            setRunResult({ run_id: current.run_id, profile_id: current.profile_id, profile_version: current.profile_version, capability: current.capability, result: current.result });
            setNotice("Agent run completed and saved.");
            break;
          }
          if (current.status === "failed") throw new Error(`Agent run failed after ${current.attempts} attempt(s) (${current.failure_code ?? "worker error"}).`);
          setNotice(`Agent run is ${current.status}; waiting for its worker.`);
        }
        if (current.status === "queued" || current.status === "running") {
          setNotice("Agent run remains saved in the queue. Refresh run history to check its status.");
        }
      } else {
        const result = await api.runAgentProfile(selectedRun.id, {
          capability: runCapability,
          task: runTask,
          confirm_external_search: runCapability === "public_web_research" && confirmExternalSearch,
        });
        setRunResult(result);
      }
      try {
        const history = await api.agentProfileRuns(selectedRun.id);
        setRunHistory(history.runs);
      } catch (historyError) {
        setError(`Run completed, but saved history could not be refreshed: ${describeError(historyError)}`);
      }
    } catch (err) {
      setError(describeError(err));
      try {
        const history = await api.agentProfileRuns(selectedRun.id);
        setRunHistory(history.runs);
      } catch { /* keep the original run failure visible */ }
    } finally {
      setRunBusy(false);
    }
  }

  async function refreshRunHistory(profileId: string) {
    setHistoryBusy(true);
    try {
      const history = await api.agentProfileRuns(profileId);
      setRunHistory(history.runs);
    } finally {
      setHistoryBusy(false);
    }
  }

  async function retryRun(run: AgentRunRecord) {
    if (!selectedRun) return;
    setRunBusy(true);
    setError(null);
    try {
      const result = await api.retryAgentRun(run.run_id);
      setRunResult(result);
      await refreshRunHistory(selectedRun.id);
    } catch (err) {
      setError(describeError(err));
      try { await refreshRunHistory(selectedRun.id); } catch { /* preserve the retry failure */ }
    } finally {
      setRunBusy(false);
    }
  }

  const typeIcon = (type: AgentType) => type === "market_research" ? <Globe2 /> : type === "company_analysis" ? <BarChart3 /> : type === "employee_training" ? <GraduationCap /> : <BookOpen />;
  const formOpen = creating || editing !== null;
  const visibleHistory = runHistory.filter((run) => run.run_id !== runResult?.run_id);

  return <>
    <PageHeader
      title="AI agents"
      description="Assistants for each department. An agent only ever sees documents the person using it is already allowed to read."
      actions={<>
        <Button onClick={() => void refresh()} disabled={busy}><RefreshCw /> Refresh</Button>
        {departments.length > 0 && <Button variant="primary" onClick={() => { resetForm(); setCreating(true); }}><Plus /> New agent</Button>}
      </>}
    />
    {error && <Alert tone="danger" className="mb-4">{error}</Alert>}
    {notice && <Alert tone="success" className="mb-4">{notice}</Alert>}

    {selectedRun && (
      <Card className="mb-5">
        <CardHeader
          icon={typeIcon(selectedRun.agent_type)}
          title={`Run ${selectedRun.name}`}
          description={`${selectedRun.department_name} · version ${selectedRun.version}. Sources are checked against your access when the agent runs.`}
          actions={<Button size="sm" variant="ghost" onClick={() => { setSelectedRun(null); setRunResult(null); }}><X /> Close</Button>}
        />
        <CardBody className="grid gap-5">
          <form className="grid gap-4" onSubmit={(event) => void runAgent(event)}>
            <div className="grid gap-4 md:grid-cols-[260px_minmax(0,1fr)]">
              <Field label="What should it do?">
                <Select aria-label="Capability" value={runCapability} onValueChange={(value) => { setRunCapability(value as AgentTool); setRunResult(null); }} options={selectedRun.tool_policy.map((tool) => ({ value: tool, label: TOOL_LABELS[tool] }))} />
              </Field>
              <Field label={runCapability === "training_plan" ? "Training topic" : runCapability === "public_web_research" ? "Public research topic" : "Your question"}>
                <Textarea required rows={3} maxLength={4000} value={runTask} onChange={(event) => setRunTask(event.target.value)} placeholder="Describe the question or task." />
              </Field>
            </div>
            {runCapability === "public_web_research" && (
              <label className="flex items-start gap-2 text-[13px] text-foreground-2">
                <Checkbox checked={confirmExternalSearch} onCheckedChange={(value) => setConfirmExternalSearch(value === true)} className="mt-0.5" />
                <span>I confirm this topic may be sent to Gemini and Google Search. Internal documents are not sent.</span>
              </label>
            )}
            <div className="flex flex-wrap items-center gap-3">
              <Button type="submit" variant="primary" loading={runBusy} disabled={runBusy || !runTask.trim() || (runCapability === "public_web_research" && !confirmExternalSearch)}><Play /> {runBusy ? "Working…" : "Run agent"}</Button>
              {runBusy && <span className="text-xs text-muted" role="status">The agent is working; long tasks continue in the background.</span>}
            </div>
          </form>
          {runResult && (
            <div className="rounded-lg border border-success/30 bg-success-soft/40 p-4">
              <p className="mb-2 flex items-center gap-2 text-[13px] font-semibold text-foreground"><CheckCircle2 className="size-4 text-success" /> Result · {TOOL_LABELS[runResult.capability as AgentTool] ?? runResult.capability}</p>
              <AgentRunOutput result={runResult.result} />
            </div>
          )}
          <div>
            <div className="mb-2 flex items-center justify-between gap-2">
              <p className="text-xs font-semibold tracking-wide text-muted uppercase">Your recent runs</p>
              <Button size="sm" variant="ghost" loading={historyBusy} onClick={() => void refreshRunHistory(selectedRun.id)}>{!historyBusy && <RefreshCw />} Refresh</Button>
            </div>
            {visibleHistory.length === 0 ? <p className="text-xs text-muted">No earlier runs. Results are private to you.</p> : (
              <div className="grid gap-2">{visibleHistory.map((run) => (
                <details key={run.run_id} className="rounded-lg border border-border p-3 [&_summary]:list-none">
                  <summary className="flex cursor-pointer items-center justify-between gap-2">
                    <span className="min-w-0"><span className="block truncate text-[13px] font-medium text-foreground">{TOOL_LABELS[run.capability as AgentTool] ?? run.capability}</span><span className="block text-xs text-muted">{formatDate(run.requested_at)} · attempt {run.attempts}</span></span>
                    <Badge tone={run.status === "completed" ? "success" : run.status === "failed" ? "danger" : "warning"} dot>{run.status}</Badge>
                  </summary>
                  <div className="mt-3 grid gap-2">
                    {run.content_purged_at && <p className="text-xs text-muted">Content removed by the retention policy; status and audit history remain.</p>}
                    {run.failure_code && <p className="text-xs text-danger-text">Ended with {run.failure_code}.</p>}
                    {run.result && <AgentRunOutput result={run.result} />}
                    {run.retry_available && <div><Button size="sm" loading={runBusy} disabled={runBusy} onClick={() => void retryRun(run)}><RotateCw /> {run.status === "running" ? "Recover and retry" : "Retry once"}</Button></div>}
                  </div>
                </details>
              ))}</div>
            )}
          </div>
        </CardBody>
      </Card>
    )}

    {agents.length === 0 ? (
      <Card className="mb-5"><EmptyState icon={<Bot />} title="No agents in your departments yet" description={departments.length ? "Create the first agent for a department with New agent." : "Ask an administrator or your department manager to create one."} action={departments.length ? <Button variant="primary" onClick={() => { resetForm(); setCreating(true); }}><Plus /> New agent</Button> : undefined} /></Card>
    ) : (
      <div className="mb-6 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {agents.map((profile) => (
          <Card key={profile.id} className={cn("flex flex-col p-4", !profile.enabled && "opacity-70")}>
            <div className="flex items-start gap-3">
              <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-primary-soft text-primary [&_svg]:size-5">{typeIcon(profile.agent_type)}</span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-[14px] font-semibold text-foreground">{profile.name}</p>
                <p className="text-xs text-muted">{TYPE_LABELS[profile.agent_type]} · v{profile.version}</p>
              </div>
              <Badge tone={profile.enabled ? "success" : "neutral"} dot>{profile.enabled ? "Active" : "Disabled"}</Badge>
            </div>
            <div className="mt-3 flex flex-wrap gap-1">
              <Badge><Building2 /> {profile.department_name}</Badge>
              {profile.tool_policy.map((tool) => <Badge key={tool} tone="primary">{TOOL_LABELS[tool]}</Badge>)}
            </div>
            <p className="mt-2 text-xs text-muted">{profile.knowledge_scope === "requester_acl" ? "Reads what the user can read" : "Reads the user's documents in this department only"}</p>
            <div className="mt-4 flex items-center gap-2 pt-1">
              {profile.enabled && <Button size="sm" variant="primary" disabled={runBusy} onClick={() => selectForRun(profile)}><Play /> Run</Button>}
              <span className="flex-1" />
              {profile.can_manage && profile.enabled && (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild><Button size="icon-sm" variant="ghost" aria-label={`More actions for ${profile.name}`}><MoreHorizontal /></Button></DropdownMenuTrigger>
                  <DropdownMenuContent>
                    <DropdownMenuItem onSelect={() => startEdit(profile)}><Pencil /> Edit (new version)</DropdownMenuItem>
                    <DropdownMenuItem danger onSelect={() => void disable(profile)}><Ban /> Disable</DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
            </div>
          </Card>
        ))}
      </div>
    )}

    <Dialog
      open={formOpen}
      onOpenChange={(open) => { if (!open) { resetForm(); setCreating(false); } }}
      title={editing ? `Edit ${editing.name}` : "New agent"}
      description={editing ? `Saving creates version ${editing.version + 1}; the previous version stays in the history.` : "Choose what the agent does and which department it belongs to."}
      className="max-w-xl"
      footer={<>
        <Button onClick={() => { resetForm(); setCreating(false); }}>Cancel</Button>
        <Button variant="primary" loading={busy} disabled={busy || !name.trim() || !departmentId || toolPolicy.length === 0} onClick={() => (document.getElementById("department-agent-profile-form") as HTMLFormElement | null)?.requestSubmit()}>{editing ? "Save new version" : "Create agent"}</Button>
      </>}
    >
      <form id="department-agent-profile-form" className="grid gap-4" onSubmit={(event) => void save(event)}>
        {error && <Alert tone="danger">{error}</Alert>}
        <Field label="Agent name"><Input required maxLength={120} value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Finance policy assistant" /></Field>
        {!editing && <Field label="Department"><Select aria-label="Department" value={departmentId} onValueChange={setDepartmentId} options={departments.map((group) => ({ value: group.id, label: group.name }))} /></Field>}
        <Field label="Type"><Select aria-label="Agent type" value={agentType} onValueChange={(value) => changeType(value as AgentType)} options={TYPE_OPTIONS.map((item) => ({ value: item, label: TYPE_LABELS[item] }))} /></Field>
        <Field label="Which documents can it use?"><Select aria-label="Knowledge boundary" value={knowledgeScope} onValueChange={(value) => setKnowledgeScope(value as AgentKnowledgeScope)} options={[{ value: "requester_acl", label: "Everything the person using it can read" }, { value: "department_only", label: "Only that person's documents in this department" }]} /></Field>
        <Field label="What can it do?">
          <div className="grid gap-2">{allowedTools.map((tool) => (
            <label key={tool.id} className="flex cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-2 text-[13px] hover:bg-surface-2">
              <Checkbox checked={toolPolicy.includes(tool.id)} onCheckedChange={(value) => toggleTool(tool.id, value === true)} />{tool.label}
            </label>
          ))}</div>
        </Field>
      </form>
    </Dialog>

    <AgentCatalogReviewSection user={user} onUseTemplate={(template) => { useReviewedTemplate(template); setCreating(true); }} />

    {user.role === "admin" && <AgentRunRetentionPanel />}
  </>;
}
