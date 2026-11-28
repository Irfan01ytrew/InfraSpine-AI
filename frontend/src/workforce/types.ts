export interface RegistryAgent {
  agent_kind: string;
  agent_id: string;
  display_name: string;
  owner_user_id: string | null;
  manager_group_id: string | null;
  registry_state: string;
  island_status: string;
  effective_status: string;
  owner_revoked: boolean;
  current_skills: string[];
  notes: string[];
}

export interface LifecycleEvent {
  agent_kind: string;
  agent_id: string;
  from_state: string | null;
  to_state: string;
  actor_user_id: string | null;
  owner_user_id: string | null;
  reason: string;
  created_at: string;
}

export interface Skill {
  id: string;
  name: string;
  description: string;
  category: string | null;
}

export interface AgentSkill {
  skill_id: string;
  name: string;
  category: string | null;
  status: string;
  verified_by_user_id: string | null;
  verified_at: string | null;
  expires_at: string | null;
}

export interface Budget {
  agent_kind: string;
  agent_id: string;
  daily_spend_limit_usd: number | null;
  monthly_spend_limit_usd: number | null;
  daily_call_limit: number | null;
  monthly_call_limit: number | null;
  explicit: boolean;
  note: string;
}

export interface OrgQuota {
  max_agents: number | null;
  monthly_spend_limit_usd: number | null;
  explicit: boolean;
  note: string;
}

export interface DelegationRule {
  from_kind: string;
  to_kind: string;
  from_agent_id: string | null;
  to_agent_id: string | null;
  allowed_actions: string[];
  allowed_scopes: string[];
  allows_sensitive: boolean;
}

export interface DelegationPolicy {
  version: number;
  max_depth: number;
  is_active: boolean;
  rules: DelegationRule[];
}

export interface DelegationAudit {
  id: string;
  parent_kind: string;
  parent_agent_id: string;
  child_kind: string;
  child_agent_id: string;
  parent_run_id: string | null;
  child_run_id: string | null;
  child_run_kind: string;
  policy_version: number;
  decision: string;
  reason: string;
  action: string;
  scope_id: string;
  depth: number;
}

export interface AgentReport {
  agent_kind: string;
  agent_id: string;
  display_name: string;
  owner_user_id: string | null;
  effective_status: string;
  can_start: boolean;
  start_reason: string;
  runs: number;
  runs_by_status: Record<string, number>;
  completed: number;
  failed: number;
  success_rate: number | null;
  avg_duration_ms: number | null;
  total_cost_usd: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_llm_calls: number;
  cost_per_task_usd: number | null;
  current_skills: string[];
  delegations_as_parent: number;
  delegations_as_child: number;
  delegations_denied: number;
  monthly_spend_limit_usd: number | null;
  usage_note: string;
}

export interface OrgReport {
  days: number;
  window_start: string;
  agents: AgentReport[];
  total_agents: number;
  agents_by_status: Record<string, number>;
  total_runs: number;
  total_completed: number;
  total_failed: number;
  success_rate: number | null;
  total_cost_usd: number;
  org_monthly_spend_limit_usd: number | null;
  org_max_agents: number | null;
}

export interface SpendReport {
  days: number;
  window_start: string;
  total_cost_usd: number;
  total_runs: number;
  cost_per_task_usd: number | null;
  by_agent: {
    agent_kind: string;
    agent_id: string;
    display_name: string;
    runs: number;
    total_cost_usd: string;
    cost_per_task_usd: string | null;
    total_llm_calls: number;
  }[];
}
