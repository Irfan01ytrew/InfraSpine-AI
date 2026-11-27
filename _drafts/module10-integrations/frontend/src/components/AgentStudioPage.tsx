import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Activity, Bot, Check, ChevronRight, FileOutput, LoaderCircle, MessageCircle, Plus, RefreshCw, ShieldCheck, Sparkles, Trash2, Wrench, X } from "lucide-react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import type { AgentStudioAgent, AgentStudioAgentInput, AgentStudioMemory, AgentStudioRun, AgentStudioThread, AgentStudioTool, AgentStudioToolName, AgentStudioWhatsAppConnection } from "../types";
import { Alert, Badge, Button, Card, CardBody, CardHeader, Checkbox, Dialog, EmptyState, Field, Input, PageHeader, Tabs, Textarea } from "./ui";
import { Markdown } from "./Markdown";
import { AgentStudioWhatsAppPanel } from "./AgentStudioWhatsAppPanel";

const TOOL_LABELS: Record<AgentStudioToolName, string> = {
  web_search: "Live web search",
  browse_url: "Read public URLs",
  browser_capture: "Isolated browser",
  file_list: "List workspace files",
  file_read: "Read workspace files",
  create_file: "Create downloadable files",
  generate_image: "Generate images",
  rag_search: "Optional company knowledge",
  my_work: "My assigned open work",
  remember: "Save private memory",
  recall: "Recall private memory",
  delegate_agent: "Delegate to subagents",
};

const NEW_AGENT: AgentStudioAgentInput = {
  name: "",
  description: "",
  instructions: "Help the user complete tasks accurately. Make a short plan, use authorized tools when useful, and clearly report what you actually completed.",
  tool_permissions: ["web_search", "browse_url", "file_list", "file_read", "create_file", "generate_image", "remember", "recall"],
  approval_tools: ["create_file", "generate_image"],
  delegate_agent_ids: [],
  rag_enabled: false,
  max_steps: 6,
  provider_id: null,
  model: null,
};

const AGENT_STARTERS: { name: string; description: string; instructions: string; tools: AgentStudioToolName[]; rag?: boolean }[] = [
  {
    name: "Research Analyst",
    description: "Research a topic using current public sources and deliver a cited summary.",
    instructions: "Research the user's question. Search the public web, open primary sources, compare evidence, and give a concise answer with clickable citations. State when information is uncertain or unavailable.",
    tools: ["web_search", "browse_url", "browser_capture", "remember", "recall"],
  },
  {
    name: "Company Knowledge Assistant",
    description: "Answer questions from authorized company documents and explain the sources.",
    instructions: "Answer from the user's authorized company knowledge when relevant. Search before answering company-specific questions, cite the returned sources, and distinguish explicit facts from reasonable analysis. Say when the available evidence does not answer the question.",
    tools: ["rag_search", "remember", "recall"], rag: true,
  },
  {
    name: "Document & Presentation Creator",
    description: "Turn a task or research into a downloadable document, spreadsheet, or presentation.",
    instructions: "Create a clear, useful deliverable from the user's request. Research when current facts matter, read relevant files when authorized, structure the content for the selected format, and request approval before creating the final file.",
    tools: ["web_search", "browse_url", "file_list", "file_read", "create_file", "generate_image"],
  },
  {
    name: "WhatsApp Support Agent",
    description: "Handle approved inbound WhatsApp questions with helpful, permission-aware answers.",
    instructions: "Respond politely and briefly in the user's language. When asked about the user's pending work, call My assigned open work and summarize only the returned tasks and due dates. Use authorized company knowledge for company questions, public search only for current public facts, and never expose private data or claim an action succeeded without a tool result.",
    tools: ["my_work", "web_search", "browse_url", "rag_search", "remember", "recall"], rag: true,
  },
];

function agentInput(agent: AgentStudioAgent): AgentStudioAgentInput {
  return {
    name: agent.name,
    description: agent.description,
    instructions: agent.instructions,
    tool_permissions: agent.tool_permissions,
    approval_tools: agent.approval_tools,
    delegate_agent_ids: agent.delegate_agent_ids,
    rag_enabled: agent.rag_enabled,
    max_steps: agent.max_steps,
    provider_id: agent.provider_id,
    model: agent.model,
  };
}

function runStatusTone(status: string): "success" | "warning" | "danger" | "neutral" {
  if (status === "completed") return "success";
  if (status === "awaiting_approval") return "warning";
  if (status === "failed") return "danger";
  return "neutral";
}

function WebToolResult({ result }: { result: unknown }) {
  if (!result || typeof result !== "object" || Array.isArray(result)) {
    return <pre className="mt-2 max-h-36 overflow-auto whitespace-pre-wrap break-words text-[11px] text-foreground-2">{JSON.stringify(result, null, 2)}</pre>;
  }
  const data = result as Record<string, unknown>;
  if (typeof data.answer !== "string" || !Array.isArray(data.sources)) {
    return <pre className="mt-2 max-h-36 overflow-auto whitespace-pre-wrap break-words text-[11px] text-foreground-2">{JSON.stringify(result, null, 2)}</pre>;
  }
  const sources = data.sources.filter((source): source is Record<string, unknown> => Boolean(source) && typeof source === "object" && !Array.isArray(source));
  const hrefFor = (source: Record<string, unknown>) => {
    try {
      const url = new URL(String(source.url ?? ""));
      return url.protocol === "https:" && !url.username && !url.password ? url.href : null;
    } catch {
      return null;
    }
  };
  return <div className="mt-2 grid gap-2">
    <Markdown text={data.answer} renderCitation={(tag, key) => {
      const source = tag.startsWith("S") ? sources[Number(tag.slice(1)) - 1] : undefined;
      const href = source ? hrefFor(source) : null;
      return href
        ? <a key={key} href={href} target="_blank" rel="noopener noreferrer" title={String(source?.title ?? "Public source")} className="mx-0.5 inline-grid h-[18px] min-w-[18px] place-items-center rounded-md bg-primary-soft px-1 align-[1px] text-[10.5px] font-semibold text-primary hover:bg-primary hover:text-primary-foreground">{tag.slice(1)}</a>
        : <span key={key} className="source-tag">{tag}</span>;
    }} />
    {sources.length > 0 && <ul className="grid gap-1 border-t border-border pt-2">
      {sources.slice(0, 8).map((source, index) => {
        const href = hrefFor(source);
        const title = String(source.title ?? href ?? `Public source ${index + 1}`);
        const publishedAt = typeof source.published_at === "string" ? source.published_at : "";
        const retrievedAt = typeof source.retrieved_at === "string" ? source.retrieved_at : "";
        return <li key={href ?? `${title}-${index}`} className="grid min-w-0 gap-0.5">
          <span className="flex min-w-0 items-baseline gap-1.5"><span className="source-tag shrink-0">S{index + 1}</span>{href ? <a className="truncate font-medium text-primary underline decoration-primary/30 underline-offset-2" href={href} target="_blank" rel="noopener noreferrer">{title}</a> : <span className="truncate">{title}</span>}</span>
          <small className="pl-7 text-muted">{publishedAt ? `Published: ${publishedAt}` : retrievedAt ? `Retrieved: ${retrievedAt}` : "Publication time not provided by source"}</small>
        </li>;
      })}
    </ul>}
  </div>;
}

export function AgentStudioPage() {
  const [agents, setAgents] = useState<AgentStudioAgent[]>([]);
  const [tools, setTools] = useState<AgentStudioTool[]>([]);
  const [memories, setMemories] = useState<AgentStudioMemory[]>([]);
  const [threads, setThreads] = useState<AgentStudioThread[]>([]);
  const [runs, setRuns] = useState<AgentStudioRun[]>([]);
  const [allRuns, setAllRuns] = useState<AgentStudioRun[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [selectedThreadId, setSelectedThreadId] = useState("");
  const [panel, setPanel] = useState(() => new URLSearchParams(window.location.search).get("panel") === "whatsapp" ? "whatsapp" : "agents");
  const [draft, setDraft] = useState<AgentStudioAgentInput>(NEW_AGENT);
  const [editingAgent, setEditingAgent] = useState<AgentStudioAgent | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [whatsAppConnection, setWhatsAppConnection] = useState<AgentStudioWhatsAppConnection | null>(null);

  const selectedAgent = useMemo(() => agents.find((agent) => agent.id === selectedAgentId) ?? null, [agents, selectedAgentId]);
  const pendingRuns = allRuns.filter((run) => run.status === "awaiting_approval" && run.pending_approval);
  const activeRuns = allRuns.filter((run) => run.status === "running");

  const loadBase = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true);
    try {
      const [agentResult, toolResult] = await Promise.all([api.agentStudioAgents(), api.agentStudioTools()]);
      setAgents(agentResult.agents);
      setTools(toolResult.tools);
      setSelectedAgentId((current) => agentResult.agents.some((agent) => agent.id === current) ? current : agentResult.agents[0]?.id ?? "");
      setError(null);
      // WhatsApp is an optional integration. A temporary status/API failure must not
      // make the independent agent workspace unusable.
      void api.agentStudioWhatsApp().then(({ connection }) => {
        setWhatsAppConnection(connection);
      }).catch((err) => {
        setWhatsAppConnection(null);
        setError(`Agents are available, but WhatsApp status could not be loaded: ${describeError(err)}`);
      });
    } catch (err) {
      setError(describeError(err));
    } finally {
      if (showLoading) setLoading(false);
    }
  }, []);

  const loadRuns = useCallback(async (items: AgentStudioAgent[]) => {
    if (!items.length) { setAllRuns([]); return; }
    try {
      const results = await Promise.all(items.map((agent) => api.agentStudioAgentRuns(agent.id)));
      setAllRuns(results.flatMap((result) => result.runs).sort((a, b) => b.created_at.localeCompare(a.created_at)));
    } catch (err) {
      setError(describeError(err));
    }
  }, []);

  useEffect(() => { void loadBase(); }, [loadBase]);

  useEffect(() => { void loadRuns(agents); }, [agents, loadRuns]);

  useEffect(() => {
    let cancelled = false;
    if (!selectedAgent) {
      setThreads([]);
      setMemories([]);
      setRuns([]);
      setSelectedThreadId("");
      return;
    }
    Promise.all([
      api.agentStudioThreads(selectedAgent.id),
      api.agentStudioMemories(selectedAgent.id),
      api.agentStudioAgentRuns(selectedAgent.id),
    ]).then(([threadResult, memoryResult, runResult]) => {
      if (cancelled) return;
      setThreads(threadResult.threads);
      setMemories(memoryResult.memories);
      setSelectedThreadId((current) => threadResult.threads.some((thread) => thread.id === current) ? current : threadResult.threads[0]?.id ?? "");
      setAllRuns((current) => {
        const others = current.filter((run) => run.agent_id !== selectedAgent.id);
        return [...others, ...runResult.runs].sort((a, b) => b.created_at.localeCompare(a.created_at));
      });
    }).catch((err) => { if (!cancelled) setError(describeError(err)); });
    return () => { cancelled = true; };
  }, [selectedAgent]);

  useEffect(() => {
    let cancelled = false;
    if (!selectedThreadId) { setRuns([]); return; }
    const load = () => api.agentStudioThreadRuns(selectedThreadId)
      .then((result) => { if (!cancelled) setRuns(result.runs); })
      .catch((err) => { if (!cancelled) setError(describeError(err)); });
    void load();
    const poll = window.setInterval(() => {
      if (runs.some((run) => run.status === "running")) void load();
    }, 2500);
    return () => { cancelled = true; window.clearInterval(poll); };
  }, [selectedThreadId, runs]);

  function startCreate(starter?: (typeof AGENT_STARTERS)[number]) {
    setEditingAgent(null);
    setDraft({ ...NEW_AGENT });
    if (starter) applyStarter(starter);
    setEditorOpen(true);
  }

  function applyStarter(starter: (typeof AGENT_STARTERS)[number]) {
    const available = new Set(tools.filter((tool) => tool.available).map((tool) => tool.name));
    const chosen = starter.tools.filter((tool) => available.has(tool));
    setDraft({
      ...NEW_AGENT,
      name: starter.name,
      description: starter.description,
      instructions: starter.instructions,
      tool_permissions: chosen.length ? chosen : ["remember"],
      approval_tools: chosen.filter((tool) => tool === "create_file" || tool === "generate_image"),
      rag_enabled: Boolean(starter.rag && available.has("rag_search")),
    });
  }

  function startEdit(agent: AgentStudioAgent) {
    setEditingAgent(agent);
    setDraft(agentInput(agent));
    setEditorOpen(true);
  }

  function updateDraft<K extends keyof AgentStudioAgentInput>(key: K, value: AgentStudioAgentInput[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function toggleTool(tool: AgentStudioToolName, enabled: boolean) {
    setDraft((current) => {
      const tool_permissions = enabled
        ? [...new Set([...current.tool_permissions, tool])]
        : current.tool_permissions.filter((item) => item !== tool);
      return {
        ...current,
        tool_permissions,
        approval_tools: enabled ? current.approval_tools : current.approval_tools.filter((item) => item !== tool),
        rag_enabled: tool === "rag_search" && !enabled ? false : current.rag_enabled,
      };
    });
  }

  async function saveAgent(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const saved = editingAgent
        ? await api.updateAgentStudioAgent(editingAgent.id, { ...draft, expected_version: editingAgent.version })
        : await api.createAgentStudioAgent(draft);
      setEditorOpen(false);
      await loadBase(false);
      setSelectedAgentId(saved.id);
      setNotice(editingAgent ? "Agent configuration saved." : "Your new agent is ready.");
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function toggleEnabled(agent: AgentStudioAgent) {
    try {
      const updated = agent.enabled ? await api.disableAgentStudioAgent(agent.id) : await api.enableAgentStudioAgent(agent.id);
      setAgents((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (err) { setError(describeError(err)); }
  }

  async function deleteAgent(agent: AgentStudioAgent) {
    const confirmed = window.confirm(`Delete “${agent.name}” and permanently remove its conversation history and private memories?`);
    if (!confirmed) return;
    try {
      await api.deleteAgentStudioAgent(agent.id);
      setAgents((current) => current.filter((item) => item.id !== agent.id));
      setSelectedAgentId("");
      setSelectedThreadId("");
      setThreads([]);
      setRuns([]);
      setMemories([]);
      await loadBase(false);
      setNotice("Agent and its private data were deleted.");
    } catch (err) { setError(describeError(err)); }
  }

  async function createThread() {
    if (!selectedAgent) return;
    try {
      const row = await api.createAgentStudioThread(selectedAgent.id);
      setThreads((current) => [row, ...current]);
      setSelectedThreadId(row.id);
      setRuns([]);
    } catch (err) { setError(describeError(err)); }
  }

  async function sendTask(event: FormEvent) {
    event.preventDefault();
    if (!selectedAgent || !prompt.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      let threadId = selectedThreadId;
      if (!threadId) {
        const thread = await api.createAgentStudioThread(selectedAgent.id, prompt.trim().slice(0, 180));
        threadId = thread.id;
        setThreads((current) => [thread, ...current]);
        setSelectedThreadId(threadId);
      }
      const result = await api.runAgentStudio(selectedAgent.id, threadId, prompt.trim());
      setPrompt("");
      setRuns((current) => [...current, result]);
      await loadRuns(agents);
      const [threadResult, memoryResult] = await Promise.all([
        api.agentStudioThreads(selectedAgent.id),
        api.agentStudioMemories(selectedAgent.id),
      ]);
      setThreads(threadResult.threads);
      setMemories(memoryResult.memories);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function decide(runId: string, decision: "approve" | "reject") {
    setBusy(true);
    setError(null);
    try {
      const updated = await api.decideAgentStudioRun(runId, decision);
      setAllRuns((current) => current.map((run) => run.id === updated.id ? updated : run));
      setRuns((current) => current.map((run) => run.id === updated.id ? updated : run));
      await loadRuns(agents);
      setNotice(decision === "approve" ? "Approved action completed." : "Action rejected; the agent resumed safely.");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }

  async function deleteMemory(memoryId: string) {
    if (!selectedAgent) return;
    try {
      await api.deleteAgentStudioMemory(selectedAgent.id, memoryId);
      setMemories((current) => current.filter((memory) => memory.id !== memoryId));
    } catch (err) { setError(describeError(err)); }
  }

  const refresh = async () => {
    await loadBase(false);
    await loadRuns(agents);
  };

  return <div className="agent-studio-page">
    <PageHeader
      eyebrow="Independent LangGraph workspace"
      title="AI Agents"
      description="Create task-focused agents that plan, use permitted tools, keep private memory, and pause for approval before consequential actions."
      actions={<div className="flex flex-wrap items-center justify-end gap-2">
        <Button variant="secondary" onClick={() => setPanel("whatsapp")}><MessageCircle /> Connect WhatsApp</Button>
        <Button variant="secondary" onClick={() => void refresh()} aria-label="Refresh agents"><RefreshCw /> Refresh</Button>
        <Button variant="primary" onClick={() => startCreate()}><Plus /> New agent</Button>
      </div>}
    />
    {error && <Alert tone="danger" title="Agent Studio could not complete that request">{error}</Alert>}
    {notice && <Alert tone="success" title={notice} />}
    <div className="mb-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Card><CardBody className="flex items-center gap-3 py-4"><span className="grid size-10 place-items-center rounded-lg bg-primary-soft text-primary"><Bot /></span><div><p className="text-xs text-muted">Custom agents</p><strong className="text-xl">{agents.length}</strong></div></CardBody></Card>
      <Card role="button" tabIndex={0} className="cursor-pointer transition hover:border-warning/40 hover:bg-warning-soft/20" onClick={() => setPanel("approvals")} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setPanel("approvals"); } }}>
        <CardBody className="flex items-center gap-3 py-4"><span className="grid size-10 place-items-center rounded-lg bg-warning-soft text-warning-text"><ShieldCheck /></span><div><p className="text-xs text-muted">Awaiting approval</p><strong className="text-xl">{pendingRuns.length}</strong></div><ChevronRight className="ml-auto size-4 text-muted" /></CardBody>
      </Card>
      <Card><CardBody className="flex items-center gap-3 py-4"><span className="grid size-10 place-items-center rounded-lg bg-success-soft text-success-text"><Activity /></span><div><p className="text-xs text-muted">Completed runs</p><strong className="text-xl">{allRuns.filter((run) => run.status === "completed").length}</strong></div></CardBody></Card>
      <Card role="button" tabIndex={0} className="cursor-pointer transition hover:border-primary/40 hover:bg-primary-soft/20" onClick={() => setPanel("whatsapp")} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setPanel("whatsapp"); } }}>
        <CardBody className="flex items-center gap-3 py-4"><span className="grid size-10 place-items-center rounded-lg bg-primary-soft text-primary"><MessageCircle /></span><div className="min-w-0"><p className="text-xs text-muted">WhatsApp</p><strong className="block truncate text-sm">{whatsAppConnection?.last_verified_at ? `Connected · ${whatsAppConnection.display_phone_number ?? "ready"}` : "Connect your agent"}</strong></div><ChevronRight className="ml-auto size-4 shrink-0 text-muted" /></CardBody>
      </Card>
    </div>
    <Tabs value={panel} onValueChange={setPanel} items={[
      { value: "agents", label: "Agents & workspace", count: agents.length },
      { value: "approvals", label: "Approvals & execution log", count: pendingRuns.length },
      { value: "whatsapp", label: "WhatsApp connection" },
    ]} className="mb-4" />

    {panel === "whatsapp" ? <AgentStudioWhatsAppPanel
      agents={agents}
      onCreateAgent={() => { setPanel("agents"); startCreate(AGENT_STARTERS[3]); }}
      onConfigureAgent={(agent) => { setPanel("agents"); setSelectedAgentId(agent.id); startEdit(agent); }}
    /> : panel === "agents" ? <div className="grid items-start gap-4 xl:grid-cols-[minmax(240px,0.32fr)_minmax(0,1fr)]">
      <Card>
        <CardHeader title="Your agents" description="Private to your account unless you explicitly add them as subagents." actions={<Button size="sm" variant="primary" onClick={() => startCreate()}><Plus /> New agent</Button>} />
        <CardBody className="grid gap-2">
          {loading ? <p className="muted">Loading agents…</p> : agents.length ? agents.map((agent) => <button
            key={agent.id}
            type="button"
            onClick={() => setSelectedAgentId(agent.id)}
            className={"flex w-full items-start gap-3 rounded-lg border p-3 text-left transition " + (selectedAgentId === agent.id ? "border-primary bg-primary-soft/40" : "border-border hover:bg-surface-2")}
          >
            <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-surface-2 text-primary"><Bot size={18} /></span>
            <span className="min-w-0 flex-1"><span className="flex items-center gap-2"><strong className="truncate text-sm">{agent.name}</strong><Badge tone={agent.enabled ? "success" : "neutral"}>{agent.enabled ? "Enabled" : "Paused"}</Badge></span><span className="mt-1 block line-clamp-2 text-xs text-muted">{agent.description || "Custom task agent"}</span></span>
            <ChevronRight className="mt-1 size-4 shrink-0 text-muted" />
          </button>) : <EmptyState icon={<Bot />} title="Create your first agent" description="Pick its role, tools and approval rules. You can make as many agents as your work needs." action={<Button variant="primary" onClick={() => startCreate()}><Plus /> New agent</Button>} />}
        </CardBody>
      </Card>

      <div className="grid gap-4">
        {selectedAgent ? <>
          <Card>
            <CardHeader
              title={selectedAgent.name}
              description={selectedAgent.description || "Custom Agent Studio assistant"}
              icon={<Sparkles />}
              actions={<><Badge tone={selectedAgent.enabled ? "success" : "neutral"}>{selectedAgent.enabled ? "Ready" : "Paused"}</Badge><Button size="sm" variant="secondary" onClick={() => startEdit(selectedAgent)}>Configure</Button><Button size="sm" variant="ghost" onClick={() => void toggleEnabled(selectedAgent)}>{selectedAgent.enabled ? "Pause agent" : "Enable agent"}</Button>{selectedAgent.name !== "Master Agent" && <Button size="sm" variant="danger-ghost" onClick={() => void deleteAgent(selectedAgent)}><Trash2 /> Delete</Button>}</>}
            />
            <CardBody className="flex flex-wrap gap-2">
              {selectedAgent.tool_permissions.map((tool) => <Badge key={tool} tone={selectedAgent.approval_tools.includes(tool) ? "warning" : "neutral"}>{TOOL_LABELS[tool] ?? tool}{selectedAgent.approval_tools.includes(tool) ? " · approval" : ""}</Badge>)}
              {selectedAgent.rag_enabled && <Badge tone="primary">Optional RAG enabled</Badge>}
              <Badge>{selectedAgent.max_steps} step limit</Badge>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Agent workspace" description="Natural-language tasks run through a bounded plan → tool → result loop." icon={<Wrench />} actions={<><select className="h-9 max-w-56 rounded-md border border-border bg-surface px-2 text-xs" value={selectedThreadId} onChange={(event) => setSelectedThreadId(event.target.value)} aria-label="Select agent conversation"><option value="">New conversation</option>{threads.map((thread) => <option key={thread.id} value={thread.id}>{thread.title}</option>)}</select><Button size="sm" variant="secondary" onClick={() => void createThread()}><Plus /> New task</Button></>} />
            <CardBody className="grid gap-4">
              <div className="grid max-h-[420px] gap-3 overflow-y-auto pr-1">
                {runs.length ? runs.map((run) => <article key={run.id} className="rounded-lg border border-border bg-surface-2/40 p-4">
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2"><p className="font-medium text-sm">{run.prompt}</p><Badge tone={runStatusTone(run.status)} dot>{run.status.replaceAll("_", " ")}</Badge></div>
                  {run.answer && <Markdown text={run.answer} />}
                  {run.steps.length > 0 && <details className="mt-3 rounded-md border border-border bg-surface p-3"><summary className="cursor-pointer text-xs font-medium">Execution log · {run.steps.length} step{run.steps.length === 1 ? "" : "s"}</summary><ol className="mt-3 grid gap-2">{run.steps.map((step, index) => <li key={index} className="rounded-md bg-surface-2 p-2 text-xs"><div className="flex items-center gap-2"><Badge tone={runStatusTone(step.status)}>{step.status}</Badge><strong>{TOOL_LABELS[step.tool as AgentStudioToolName] ?? step.tool}</strong></div><p className="mt-1 text-muted">{step.summary}</p>{step.result !== undefined && (step.tool === "web_search" || step.tool === "browse_url" ? <WebToolResult result={step.result} /> : <pre className="mt-2 max-h-36 overflow-auto whitespace-pre-wrap break-words text-[11px] text-foreground-2">{JSON.stringify(step.result, null, 2)}</pre>)}</li>)}</ol></details>}
                  {run.pending_approval && <div className="mt-3 rounded-lg border border-warning/30 bg-warning-soft/40 p-3"><div className="flex items-start gap-2"><ShieldCheck className="mt-0.5 size-4 shrink-0 text-warning-text" /><div className="flex-1"><strong className="text-sm">Approval needed</strong><p className="mt-1 text-xs">{run.pending_approval.summary}</p></div></div><div className="mt-3 flex justify-end gap-2"><Button size="sm" variant="secondary" disabled={busy} onClick={() => void decide(run.id, "reject")}><X /> Reject</Button><Button size="sm" variant="primary" disabled={busy} onClick={() => void decide(run.id, "approve")}><Check /> Approve and continue</Button></div></div>}
                </article>) : <EmptyState icon={<Sparkles />} title="Start a task" description="Ask this agent to research, read files, prepare a deliverable, or work through a multi-step task." />}
              </div>
              <form className="grid gap-2" onSubmit={(event) => void sendTask(event)}>
                <Field label="What should this agent do?">
                  <Textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Research three current options, compare their official documentation, then prepare a short PDF report…" rows={3} maxLength={6000} disabled={!selectedAgent.enabled || busy} />
                </Field>
                <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-xs text-muted">Tool calls obey this agent’s permissions. File creation and image generation pause for approval by default.</p><Button variant="primary" type="submit" disabled={!selectedAgent.enabled || !prompt.trim() || busy}>{busy ? <><LoaderCircle className="animate-spin" /> Working…</> : <><Sparkles /> Run agent</>}</Button></div>
              </form>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Private agent memory" description="Encrypted preferences and project facts saved by this agent." icon={<ShieldCheck />} />
            <CardBody className="grid gap-2">
              {memories.length ? memories.map((memory) => <div key={memory.id} className="flex items-start gap-2 rounded-md border border-border p-3"><div className="min-w-0 flex-1"><strong className="text-xs">{memory.memory_key}</strong><p className="mt-1 text-xs text-muted">{memory.value}</p></div><Button size="sm" variant="ghost" onClick={() => void deleteMemory(memory.id)} aria-label={"Delete memory " + memory.memory_key}><X /></Button></div>) : <p className="text-xs text-muted">No memories saved yet. The agent can save a preference when the memory tool is enabled.</p>}
            </CardBody>
          </Card>
        </> : <Card><EmptyState icon={<Bot />} title="Choose or create an agent" description="Agent Studio runs separately from Knowledge chat and can use company RAG only when you enable that tool." action={<Button variant="primary" onClick={() => startCreate()}><Plus /> Create agent</Button>} /></Card>}
      </div>
    </div> : <div className="grid gap-4">
      <Card>
        <CardHeader title="Pending human approvals" description="Write-capable tools wait here before they run." icon={<ShieldCheck />} />
        <CardBody className="grid gap-3">
          {pendingRuns.length ? pendingRuns.map((run) => <div key={run.id} className="flex flex-wrap items-center gap-3 rounded-lg border border-warning/30 bg-warning-soft/30 p-4"><div className="min-w-0 flex-1"><strong className="text-sm">{agents.find((agent) => agent.id === run.agent_id)?.name ?? "Agent"} · {TOOL_LABELS[run.pending_approval!.tool_name as AgentStudioToolName] ?? run.pending_approval!.tool_name}</strong><p className="mt-1 text-xs text-muted">{run.pending_approval!.summary}</p><p className="mt-1 truncate text-xs">{run.prompt}</p></div><Button size="sm" variant="secondary" disabled={busy} onClick={() => void decide(run.id, "reject")}><X /> Reject</Button><Button size="sm" variant="primary" disabled={busy} onClick={() => void decide(run.id, "approve")}><Check /> Approve</Button></div>) : <EmptyState icon={<ShieldCheck />} title="No pending approvals" description="Any action configured to require approval will appear here before it executes." />}
        </CardBody>
      </Card>
      <Card>
        <CardHeader title="Recent execution log" description="Run status, agent, task, tool calls and completion time." icon={<Activity />} actions={<Badge>{activeRuns.length} active</Badge>} />
        <CardBody className="grid gap-2">
          {allRuns.length ? allRuns.map((run) => <details key={run.id} className="rounded-lg border border-border p-3"><summary className="flex cursor-pointer list-none flex-wrap items-center gap-2"><Badge tone={runStatusTone(run.status)} dot>{run.status.replaceAll("_", " ")}</Badge><strong className="text-sm">{agents.find((agent) => agent.id === run.agent_id)?.name ?? "Agent"}</strong><span className="min-w-0 flex-1 truncate text-xs text-muted">{run.prompt}</span><span className="text-xs text-muted">{formatDate(run.created_at)}</span></summary><div className="mt-3 grid gap-3 border-t border-border pt-3">{run.answer && <Markdown text={run.answer} />}{run.steps.map((step, index) => <div key={index} className="flex items-start gap-2 text-xs"><Badge tone={runStatusTone(step.status)}>{step.status}</Badge><span><strong>{TOOL_LABELS[step.tool as AgentStudioToolName] ?? step.tool}</strong><span className="ml-2 text-muted">{step.summary}</span></span></div>)}{run.pending_approval && <div className="flex justify-end gap-2"><Button size="sm" variant="secondary" disabled={busy} onClick={() => void decide(run.id, "reject")}>Reject</Button><Button size="sm" variant="primary" disabled={busy} onClick={() => void decide(run.id, "approve")}>Approve and continue</Button></div>}</div></details>) : <EmptyState icon={<FileOutput />} title="No execution history yet" description="Completed and paused runs will be listed here." />}
        </CardBody>
      </Card>
    </div>}

    <Dialog
      open={editorOpen}
      onOpenChange={setEditorOpen}
      title={editingAgent ? "Configure agent" : "Create an agent"}
      description="Agent definitions are private to you. Tool access is enforced by the server on every run."
      className="max-w-3xl"
      footer={<><Button variant="secondary" onClick={() => setEditorOpen(false)}>Cancel</Button><Button variant="primary" form="agent-studio-editor" type="submit" disabled={busy}>{busy ? "Saving…" : editingAgent ? "Save changes" : "Create agent"}</Button></>}
    >
      <form id="agent-studio-editor" className="grid gap-4" onSubmit={(event) => void saveAgent(event)}>
        {!editingAgent && <section className="grid gap-2"><div><h3 className="text-sm font-semibold">Start from a template</h3><p className="text-xs text-muted">Choose a role, then customize its instructions and permissions.</p></div><div className="grid gap-2 sm:grid-cols-2">{AGENT_STARTERS.map((starter) => <button key={starter.name} type="button" onClick={() => applyStarter(starter)} className="rounded-lg border border-border p-3 text-left transition hover:border-primary/50 hover:bg-primary-soft/20"><strong className="text-xs">{starter.name}</strong><span className="mt-1 block text-[11px] leading-relaxed text-muted">{starter.description}</span></button>)}</div></section>}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Agent name"><Input value={draft.name} onChange={(event) => updateDraft("name", event.target.value)} required maxLength={100} placeholder="Market research analyst" /></Field>
          <Field label="Purpose"><Input value={draft.description} onChange={(event) => updateDraft("description", event.target.value)} maxLength={500} placeholder="Research competitors and produce sourced briefs" /></Field>
        </div>
        <Field label="Agent instructions" hint="Define the role, expected output and operating constraints. This is not executable code.">
          <Textarea value={draft.instructions} onChange={(event) => updateDraft("instructions", event.target.value)} required rows={5} maxLength={6000} />
        </Field>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Maximum tool steps"><Input type="number" min={1} max={8} value={draft.max_steps} onChange={(event) => updateDraft("max_steps", Number(event.target.value))} /></Field>
          <Field label="Provider ID (optional)" hint="Blank uses the active configured provider."><Input value={draft.provider_id ?? ""} onChange={(event) => updateDraft("provider_id", event.target.value || null)} placeholder="Active default" /></Field>
          <Field label="Model ID (optional)" hint="Must belong to the selected provider."><Input value={draft.model ?? ""} onChange={(event) => updateDraft("model", event.target.value || null)} placeholder="Active default" /></Field>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          <section className="rounded-lg border border-border p-3"><h3 className="mb-2 text-sm font-semibold">Tool permissions</h3><div className="grid gap-2">{tools.map((tool) => <label key={tool.name} className="flex cursor-pointer items-start gap-2 rounded-md p-2 hover:bg-surface-2"><Checkbox checked={draft.tool_permissions.includes(tool.name)} disabled={!tool.available && !draft.tool_permissions.includes(tool.name)} onCheckedChange={(checked) => toggleTool(tool.name, checked === true)} /><span className="min-w-0 flex-1"><span className="flex items-center gap-2 text-xs font-medium">{TOOL_LABELS[tool.name]}{tool.requires_approval_by_default && <Badge tone="warning">Write action</Badge>}{!tool.available && <Badge tone="neutral">Not configured</Badge>}</span><span className="mt-1 block text-[11px] text-muted">{tool.availability_note ?? tool.description}</span></span></label>)}</div></section>
          <section className="grid content-start gap-3">
            <div className="rounded-lg border border-border p-3"><label className="flex cursor-pointer items-start gap-2"><Checkbox checked={draft.rag_enabled} onCheckedChange={(checked) => { const enabled = checked === true; updateDraft("rag_enabled", enabled); toggleTool("rag_search", enabled); }} /><span><strong className="text-xs">Optional company knowledge</strong><span className="mt-1 block text-[11px] text-muted">Agents remain usable without RAG. Enabling this adds only ACL-filtered document search.</span></span></label></div>
            <div className="rounded-lg border border-border p-3"><h3 className="mb-2 text-sm font-semibold">Approval policy</h3><p className="mb-2 text-[11px] text-muted">These tools stop and wait for your explicit approval.</p>{draft.tool_permissions.filter((tool) => tools.find((item) => item.name === tool)?.requires_approval_by_default).map((tool) => <label key={tool} className="flex items-center gap-2 rounded-md p-2 text-xs hover:bg-surface-2"><Checkbox checked={draft.approval_tools.includes(tool)} onCheckedChange={(checked) => updateDraft("approval_tools", checked === true ? [...new Set([...draft.approval_tools, tool])] : draft.approval_tools.filter((item) => item !== tool))} />Require approval for {TOOL_LABELS[tool]}</label>)}</div>
            {draft.delegate_agent_ids.length > 0 && <Badge tone="primary">Delegation targets: {draft.delegate_agent_ids.length}</Badge>}
            <p className="text-[11px] text-muted">Browser capture is isolated, GET-only and uses the existing public-origin allowlist. Agents cannot run shell commands or access arbitrary server paths.</p>
          </section>
        </div>
        {draft.tool_permissions.includes("delegate_agent") && <Field label="Allowed subagents"><select multiple className="min-h-24 w-full rounded-md border border-border bg-surface p-2 text-xs" value={draft.delegate_agent_ids} onChange={(event) => updateDraft("delegate_agent_ids", Array.from(event.target.selectedOptions, (option) => option.value))}>{agents.filter((agent) => agent.id !== editingAgent?.id && agent.enabled).map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select><span className="text-[11px] text-muted">Subagents must be enabled, owned by you, and configured with read-only tools.</span></Field>}
      </form>
    </Dialog>
  </div>;
}
