import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { describeError } from "../api";
import { workforceApi } from "./api";
import type {
  AgentReport,
  AgentSkill,
  DelegationAudit,
  DelegationPolicy,
  LifecycleEvent,
  OrgQuota,
  OrgReport,
  RegistryAgent,
  Skill,
  SpendReport,
} from "./types";
import {
  filterAgents,
  formatDurationMs,
  formatMoneyUsd,
  sortAgents,
  statusLabel,
  statusTone,
  summarizeOrg,
} from "./workforceView.mjs";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Input, PageHeader, SelectInput, Skeleton, Stat, Tabs, Textarea } from "../components/ui";

type Tab = "registry" | "delegation" | "spend";

function numberOrNull(value: string): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function WorkforcePage() {
  const [tab, setTab] = useState<Tab>("registry");
  const [agents, setAgents] = useState<RegistryAgent[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [selectedKey, setSelectedKey] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [agentRows, skillRows] = await Promise.all([workforceApi.agents(), workforceApi.skills()]);
      setAgents(agentRows);
      setSkills(skillRows);
      setError("");
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const visible = useMemo(
    () => sortAgents(filterAgents(agents, query, statusFilter), "name"),
    [agents, query, statusFilter]);
  const selected = agents.find((a) => `${a.agent_kind}:${a.agent_id}` === selectedKey) ?? null;

  return (
    <div className="grid gap-5">
      <PageHeader
        title="AI Workforce"
        description="Every agent in one registry: lifecycle, verified skills, delegation policy, budgets and spend."
        actions={<Button variant="secondary" onClick={() => void refresh()}>Refresh</Button>}
      />
      {error && <Alert tone="danger" title="Workforce data could not be loaded">{error}</Alert>}
      {notice && <Alert tone="success" title={notice}>{notice}</Alert>}
      <Tabs
        value={tab}
        onValueChange={(value) => setTab(value as Tab)}
        items={[
          { value: "registry", label: "Registry", count: agents.length },
          { value: "delegation", label: "Delegation" },
          { value: "spend", label: "Budgets & spend" },
        ]}
      />
      {loading ? (
        <Skeleton className="h-64 w-full" />
      ) : tab === "registry" ? (
        <RegistryTab
          agents={visible}
          allSkills={skills}
          selected={selected}
          onSelect={(agent) => setSelectedKey(agent ? `${agent.agent_kind}:${agent.agent_id}` : "")}
          onChanged={() => void refresh()}
          onNotice={setNotice}
          onError={setError}
          query={query}
          onQuery={setQuery}
          statusFilter={statusFilter}
          onStatusFilter={setStatusFilter}
        />
      ) : tab === "delegation" ? (
        <DelegationTab onError={setError} onNotice={setNotice} />
      ) : (
        <SpendTab onError={setError} onNotice={setNotice} />
      )}
    </div>
  );
}

function RegistryTab({ agents, allSkills, selected, onSelect, onChanged, onNotice, onError, query, onQuery, statusFilter, onStatusFilter }: {
  agents: RegistryAgent[];
  allSkills: Skill[];
  selected: RegistryAgent | null;
  onSelect: (agent: RegistryAgent | null) => void;
  onChanged: () => void;
  onNotice: (message: string) => void;
  onError: (message: string) => void;
  query: string;
  onQuery: (value: string) => void;
  statusFilter: string;
  onStatusFilter: (value: string) => void;
}) {
  return (
    <div className="grid gap-4">
      <Card>
        <CardBody>
          <div className="flex flex-wrap gap-3">
            <Input
              className="min-w-56 flex-1"
              placeholder="Search by name, kind or id"
              value={query}
              onChange={(event) => onQuery(event.target.value)}
            />
            <SelectInput value={statusFilter} onChange={(event) => onStatusFilter(event.target.value)} aria-label="Filter by status">
              <option value="">All statuses</option>
              <option value="active">Active</option>
              <option value="paused">Paused</option>
              <option value="retired">Retired</option>
              <option value="dead">Dead</option>
            </SelectInput>
          </div>
        </CardBody>
      </Card>
      {agents.length === 0 ? (
        <Card><EmptyState title="No agents in the registry" description="Agents appear here once they are registered on the Studio, governance or profile islands." /></Card>
      ) : (
        <Card>
          <table className="w-full text-left text-[13px]">
            <thead>
              <tr className="border-b border-border text-muted">
                <th className="px-4 py-2 font-medium">Agent</th>
                <th className="px-4 py-2 font-medium">Kind</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">Skills</th>
                <th className="px-4 py-2 font-medium">Owner</th>
              </tr>
            </thead>
            <tbody>
              {agents.map((agent) => (
                <tr
                  key={`${agent.agent_kind}:${agent.agent_id}`}
                  className="cursor-pointer border-b border-border last:border-0 hover:bg-surface-2"
                  onClick={() => onSelect(agent)}
                >
                  <td className="px-4 py-2 font-medium">{agent.display_name}</td>
                  <td className="px-4 py-2">{agent.agent_kind}</td>
                  <td className="px-4 py-2"><Badge tone={statusTone(agent.effective_status)}>{statusLabel(agent.effective_status)}</Badge></td>
                  <td className="px-4 py-2">{agent.current_skills.length}</td>
                  <td className="px-4 py-2 truncate text-muted">{agent.owner_user_id ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
      {selected && (
        <AgentDetail
          agent={selected}
          allSkills={allSkills}
          onClose={() => onSelect(null)}
          onChanged={onChanged}
          onNotice={onNotice}
          onError={onError}
        />
      )}
    </div>
  );
}

function AgentDetail({ agent, allSkills, onClose, onChanged, onNotice, onError }: {
  agent: RegistryAgent;
  allSkills: Skill[];
  onClose: () => void;
  onChanged: () => void;
  onNotice: (message: string) => void;
  onError: (message: string) => void;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [agentSkills, setAgentSkills] = useState<AgentSkill[]>([]);
  const [report, setReport] = useState<AgentReport | null>(null);
  const [history, setHistory] = useState<LifecycleEvent[]>([]);
  const [attachId, setAttachId] = useState("");
  const [dailySpend, setDailySpend] = useState("");
  const [monthlySpend, setMonthlySpend] = useState("");
  const [dailyCalls, setDailyCalls] = useState("");
  const [monthlyCalls, setMonthlyCalls] = useState("");
  const [checkResult, setCheckResult] = useState("");

  const loadDetail = useCallback(async () => {
    try {
      const [skillRows, agentReport, events] = await Promise.all([
        workforceApi.agentSkills(agent.agent_kind, agent.agent_id),
        workforceApi.agentReport(agent.agent_kind, agent.agent_id),
        workforceApi.history(agent.agent_kind, agent.agent_id),
      ]);
      setAgentSkills(skillRows);
      setReport(agentReport);
      setHistory(events);
      const budget = await workforceApi.budget(agent.agent_kind, agent.agent_id);
      setDailySpend(budget.daily_spend_limit_usd === null ? "" : String(budget.daily_spend_limit_usd));
      setMonthlySpend(budget.monthly_spend_limit_usd === null ? "" : String(budget.monthly_spend_limit_usd));
      setDailyCalls(budget.daily_call_limit === null ? "" : String(budget.daily_call_limit));
      setMonthlyCalls(budget.monthly_call_limit === null ? "" : String(budget.monthly_call_limit));
    } catch (reason) {
      onError(describeError(reason));
    }
  }, [agent, onError]);

  useEffect(() => { void loadDetail(); }, [loadDetail]);

  async function lifecycle(action: "pause" | "resume" | "retire") {
    if (!reason.trim()) { onError("A reason is required for lifecycle changes."); return; }
    setBusy(true);
    try {
      await workforceApi[action](agent.agent_kind, agent.agent_id, reason.trim());
      onNotice(`${agent.display_name} ${action}d.`);
      setReason("");
      onChanged();
    } catch (reason) {
      onError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function saveBudget() {
    setBusy(true);
    try {
      await workforceApi.setBudget(agent.agent_kind, agent.agent_id, {
        daily_spend_limit_usd: numberOrNull(dailySpend),
        monthly_spend_limit_usd: numberOrNull(monthlySpend),
        daily_call_limit: numberOrNull(dailyCalls),
        monthly_call_limit: numberOrNull(monthlyCalls),
      });
      onNotice(`Budget saved for ${agent.display_name}.`);
      onChanged();
    } catch (reason) {
      onError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function checkStart() {
    try {
      const result = await workforceApi.checkStart(agent.agent_kind, agent.agent_id);
      setCheckResult(result.allowed ? "Can start." : `Cannot start: ${result.reason}`);
    } catch (reason) {
      onError(describeError(reason));
    }
  }

  const attachedIds = new Set(agentSkills.map((s) => s.skill_id));
  const unattached = allSkills.filter((s) => !attachedIds.has(s.id));

  return (
    <Card>
      <CardHeader
        title={agent.display_name}
        description={`${agent.agent_kind} · ${agent.island_status}${agent.owner_revoked ? " · owner revoked" : ""}`}
        actions={<Button variant="ghost" onClick={onClose}>Close</Button>}
      />
      <CardBody>
        <div className="grid gap-5">
          {agent.notes.length > 0 && (
            <Alert tone="info" title="Registry notes">{agent.notes.join(" ")}</Alert>
          )}
          <div>
            <p className="mb-2 text-sm font-medium">Lifecycle</p>
            <div className="flex flex-wrap items-center gap-2">
              <Textarea
                className="min-w-64 flex-1"
                placeholder="Reason (required, recorded in the audit trail)"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
              <Button disabled={busy || agent.effective_status === "paused"} onClick={() => void lifecycle("pause")}>Pause</Button>
              <Button disabled={busy || agent.effective_status !== "paused"} onClick={() => void lifecycle("resume")}>Resume</Button>
              <Button variant="danger" disabled={busy || agent.effective_status === "retired"} onClick={() => void lifecycle("retire")}>Retire</Button>
            </div>
          </div>
          <div>
            <p className="mb-2 text-sm font-medium">Verified skills</p>
            {agentSkills.length === 0 ? (
              <p className="text-sm text-muted">No skills attached.</p>
            ) : (
              <ul className="grid gap-2">
                {agentSkills.map((skill) => (
                  <li key={skill.skill_id} className="flex flex-wrap items-center gap-2 text-[13px]">
                    <span className="font-medium">{skill.name}</span>
                    <Badge tone={skill.status === "verified" ? "success" : skill.status === "expired" ? "danger" : "neutral"}>{skill.status}</Badge>
                    {skill.expires_at && <span className="text-muted">expires {skill.expires_at.slice(0, 10)}</span>}
                    <span className="flex gap-1">
                      <Button size="sm" disabled={skill.status === "verified"} onClick={() => void workforceApi.verifySkill(agent.agent_kind, agent.agent_id, skill.skill_id).then(() => void loadDetail()).catch((e) => onError(describeError(e)))}>Verify</Button>
                      <Button size="sm" variant="secondary" disabled={skill.status !== "verified"} onClick={() => void workforceApi.revokeSkill(agent.agent_kind, agent.agent_id, skill.skill_id).then(() => void loadDetail()).catch((e) => onError(describeError(e)))}>Revoke</Button>
                      <Button size="sm" variant="ghost" onClick={() => void workforceApi.removeSkill(agent.agent_kind, agent.agent_id, skill.skill_id).then(() => void loadDetail()).catch((e) => onError(describeError(e)))}>Remove</Button>
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {unattached.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-2">
                <SelectInput value={attachId} onChange={(event) => setAttachId(event.target.value)} aria-label="Skill to attach">
                  <option value="">Attach a skill…</option>
                  {unattached.map((skill) => <option key={skill.id} value={skill.id}>{skill.name}</option>)}
                </SelectInput>
                <Button size="sm" disabled={!attachId} onClick={() => void workforceApi.attachSkill(agent.agent_kind, agent.agent_id, attachId).then(() => { setAttachId(""); void loadDetail(); }).catch((e) => onError(describeError(e)))}>Attach</Button>
              </div>
            )}
          </div>
          <div>
            <p className="mb-2 text-sm font-medium">Budget (USD / calls, blank = no cap)</p>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Input placeholder="Daily spend" value={dailySpend} onChange={(e) => setDailySpend(e.target.value)} />
              <Input placeholder="Monthly spend" value={monthlySpend} onChange={(e) => setMonthlySpend(e.target.value)} />
              <Input placeholder="Daily calls" value={dailyCalls} onChange={(e) => setDailyCalls(e.target.value)} />
              <Input placeholder="Monthly calls" value={monthlyCalls} onChange={(e) => setMonthlyCalls(e.target.value)} />
            </div>
            <div className="mt-2 flex flex-wrap gap-2">
              <Button size="sm" disabled={busy} onClick={() => void saveBudget()}>Save budget</Button>
              <Button size="sm" variant="secondary" disabled={busy} onClick={() => void workforceApi.clearBudget(agent.agent_kind, agent.agent_id).then(() => void loadDetail()).catch((e) => onError(describeError(e)))}>Clear to default</Button>
              <Button size="sm" variant="secondary" onClick={() => void checkStart()}>Check can start</Button>
              {checkResult && <span className="self-center text-[13px]">{checkResult}</span>}
            </div>
          </div>
          {report && (
            <div>
              <p className="mb-2 text-sm font-medium">Last 30 days</p>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Stat label="Runs" value={String(report.runs)} />
                <Stat label="Success rate" value={report.success_rate === null ? "—" : `${(report.success_rate * 100).toFixed(1)}%`} />
                <Stat label="Avg duration" value={formatDurationMs(report.avg_duration_ms)} />
                <Stat label="Cost" value={formatMoneyUsd(report.total_cost_usd)} />
              </div>
              <p className="mt-2 text-[13px] text-muted">
                {report.delegations_as_parent} delegations as parent · {report.delegations_as_child} as child · {report.delegations_denied} denied · {report.usage_note}
              </p>
            </div>
          )}
          {history.length > 0 && (
            <div>
              <p className="mb-2 text-sm font-medium">Lifecycle history</p>
              <ul className="grid gap-1 text-[13px] text-muted">
                {history.slice(0, 10).map((event) => (
                  <li key={`${event.created_at}:${event.to_state}`}>
                    {event.created_at.slice(0, 19).replace("T", " ")} — {event.from_state ?? "—"} → {event.to_state} · {event.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </CardBody>
    </Card>
  );
}

function DelegationTab({ onError, onNotice }: { onError: (message: string) => void; onNotice: (message: string) => void }) {
  const [policies, setPolicies] = useState<DelegationPolicy[]>([]);
  const [audits, setAudits] = useState<DelegationAudit[]>([]);
  const [loading, setLoading] = useState(true);
  const [maxDepth, setMaxDepth] = useState("2");
  const [fromKind, setFromKind] = useState("studio");
  const [toKind, setToKind] = useState("studio");
  const [action, setAction] = useState("");
  const [scope, setScope] = useState("");
  const [sensitive, setSensitive] = useState(false);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [policyRows, auditRows] = await Promise.all([workforceApi.policies(), workforceApi.delegations()]);
      setPolicies(policyRows);
      setAudits(auditRows);
    } catch (reason) {
      onError(describeError(reason));
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => { void refresh(); }, [refresh]);

  async function createPolicy(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await workforceApi.createPolicy(
        [{
          from_kind: fromKind,
          to_kind: toKind,
          from_agent_id: null,
          to_agent_id: null,
          allowed_actions: action.trim() ? [action.trim()] : [],
          allowed_scopes: scope.trim() ? [scope.trim()] : [],
          allows_sensitive: sensitive,
        }],
        Number(maxDepth) || 2);
      onNotice("Delegation policy created and activated.");
      await refresh();
    } catch (reason) {
      onError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <Skeleton className="h-64 w-full" />;
  return (
    <div className="grid gap-4">
      <Card>
        <CardHeader title="Delegation policies" description="A new policy version deactivates the previous one. Blank action/scope means any." />
        <CardBody>
          {policies.length === 0 ? (
            <p className="text-sm text-muted">No policy yet — delegations are allowed and audited as “no_active_policy”.</p>
          ) : (
            <ul className="grid gap-2 text-[13px]">
              {policies.map((policy) => (
                <li key={policy.version} className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">v{policy.version}</span>
                  <Badge tone={policy.is_active ? "success" : "neutral"}>{policy.is_active ? "Active" : "Inactive"}</Badge>
                  <span className="text-muted">max depth {policy.max_depth} · {policy.rules.length} rule(s)</span>
                </li>
              ))}
            </ul>
          )}
          <form className="mt-4 grid gap-2" onSubmit={(event) => void createPolicy(event)}>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <SelectInput value={fromKind} onChange={(e) => setFromKind(e.target.value)} aria-label="From kind">
                <option value="studio">studio</option>
                <option value="governance">governance</option>
                <option value="profile">profile</option>
              </SelectInput>
              <SelectInput value={toKind} onChange={(e) => setToKind(e.target.value)} aria-label="To kind">
                <option value="studio">studio</option>
                <option value="governance">governance</option>
                <option value="profile">profile</option>
              </SelectInput>
              <Input placeholder="Action (blank = any)" value={action} onChange={(e) => setAction(e.target.value)} />
              <Input placeholder="Scope id (blank = any)" value={scope} onChange={(e) => setScope(e.target.value)} />
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex items-center gap-2 text-[13px]">
                <input type="checkbox" checked={sensitive} onChange={(e) => setSensitive(e.target.checked)} />
                Allows sensitive actions
              </label>
              <Input className="w-28" placeholder="Max depth" value={maxDepth} onChange={(e) => setMaxDepth(e.target.value)} />
              <Button type="submit" disabled={busy}>Create &amp; activate policy</Button>
            </div>
          </form>
        </CardBody>
      </Card>
      <Card>
        <CardHeader title="Delegation audit" description="Allowed and denied delegations, newest first." />
        <CardBody>
          {audits.length === 0 ? (
            <p className="text-sm text-muted">No delegations recorded yet.</p>
          ) : (
            <ul className="grid gap-2 text-[13px]">
              {audits.slice(0, 25).map((audit) => (
                <li key={audit.id} className="flex flex-wrap items-center gap-2">
                  <Badge tone={audit.decision === "allowed" ? "success" : "danger"}>{audit.decision}</Badge>
                  <span>{audit.parent_kind} → {audit.child_kind} · depth {audit.depth} · v{audit.policy_version}</span>
                  <span className="text-muted">{audit.action} · {audit.reason}</span>
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>
    </div>
  );
}

function SpendTab({ onError, onNotice }: { onError: (message: string) => void; onNotice: (message: string) => void }) {
  const [quota, setQuota] = useState<OrgQuota | null>(null);
  const [org, setOrg] = useState<OrgReport | null>(null);
  const [spend, setSpend] = useState<SpendReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [maxAgents, setMaxAgents] = useState("");
  const [monthlyLimit, setMonthlyLimit] = useState("");
  const [days, setDays] = useState("30");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async (windowDays: number) => {
    setLoading(true);
    try {
      const [quotaRow, orgReport, spendReport] = await Promise.all([
        workforceApi.orgQuota(),
        workforceApi.orgReport(windowDays),
        workforceApi.spendReport(windowDays),
      ]);
      setQuota(quotaRow);
      setOrg(orgReport);
      setSpend(spendReport);
      setMaxAgents(quotaRow.max_agents === null ? "" : String(quotaRow.max_agents));
      setMonthlyLimit(quotaRow.monthly_spend_limit_usd === null ? "" : String(quotaRow.monthly_spend_limit_usd));
    } catch (reason) {
      onError(describeError(reason));
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => { void refresh(30); }, [refresh]);

  async function saveQuota() {
    setBusy(true);
    try {
      await workforceApi.setOrgQuota({
        max_agents: numberOrNull(maxAgents),
        monthly_spend_limit_usd: numberOrNull(monthlyLimit),
      });
      onNotice("Organisation quota saved.");
      await refresh(Number(days) || 30);
    } catch (reason) {
      onError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <Skeleton className="h-64 w-full" />;
  const summary = org ? summarizeOrg(org) : null;
  return (
    <div className="grid gap-4">
      <Card>
        <CardHeader title="Organisation quota" description="Blank means no cap. New agents are refused once the quota is hit." />
        <CardBody>
          <div className="flex flex-wrap gap-2">
            <Input className="w-40" placeholder="Max agents" value={maxAgents} onChange={(e) => setMaxAgents(e.target.value)} />
            <Input className="w-48" placeholder="Monthly spend (USD)" value={monthlyLimit} onChange={(e) => setMonthlyLimit(e.target.value)} />
            <Button disabled={busy} onClick={() => void saveQuota()}>Save quota</Button>
            {quota && !quota.explicit && <span className="self-center text-[13px] text-muted">No quota set yet — defaults apply.</span>}
          </div>
        </CardBody>
      </Card>
      {org && summary && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Agents" value={String(summary.totalAgents)} />
          <Stat label="Paused" value={String(summary.pausedAgents)} />
          <Stat label="Cannot start" value={String(summary.cannotStartAgents)} />
          <Stat label="Total cost" value={formatMoneyUsd(summary.totalCostUsd)} />
        </div>
      )}
      <Card>
        <CardHeader
          title="Spend by agent"
          description={spend ? `Last ${spend.days} days · ${spend.total_runs} runs · ${formatMoneyUsd(spend.total_cost_usd)} total` : ""}
          actions={(
            <SelectInput value={days} onChange={(e) => { setDays(e.target.value); void refresh(Number(e.target.value) || 30); }} aria-label="Window in days">
              <option value="7">7 days</option>
              <option value="30">30 days</option>
              <option value="90">90 days</option>
            </SelectInput>
          )}
        />
        <CardBody>
          {!spend || spend.by_agent.length === 0 ? (
            <p className="text-sm text-muted">No runs in this window.</p>
          ) : (
            <table className="w-full text-left text-[13px]">
              <thead>
                <tr className="border-b border-border text-muted">
                  <th className="px-2 py-2 font-medium">Agent</th>
                  <th className="px-2 py-2 font-medium">Runs</th>
                  <th className="px-2 py-2 font-medium">Cost</th>
                  <th className="px-2 py-2 font-medium">Cost / task</th>
                  <th className="px-2 py-2 font-medium">LLM calls</th>
                </tr>
              </thead>
              <tbody>
                {spend.by_agent.map((row) => (
                  <tr key={`${row.agent_kind}:${row.agent_id}`} className="border-b border-border last:border-0">
                    <td className="px-2 py-2 font-medium">{row.display_name}</td>
                    <td className="px-2 py-2">{row.runs}</td>
                    <td className="px-2 py-2">{formatMoneyUsd(row.total_cost_usd)}</td>
                    <td className="px-2 py-2">{row.cost_per_task_usd === null ? "—" : formatMoneyUsd(row.cost_per_task_usd)}</td>
                    <td className="px-2 py-2">{row.total_llm_calls}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
