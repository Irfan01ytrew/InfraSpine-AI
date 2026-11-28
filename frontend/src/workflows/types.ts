export type Kind = 'start' | 'end' | 'task' | 'human' | 'approval' | 'decision' | 'parallel' | 'timer' | 'event' | 'agent' | 'integration' | 'subworkflow' | 'loop';
export type Risk = 'low' | 'medium' | 'high' | 'critical';
export interface Field { name: string; label: string; type: 'text' | 'number' | 'boolean' | 'choice'; required: boolean; options?: string[] }
export interface NodeConfig {
  assignee_id?: string | null; assignee_variable?: string | null; approver_ids?: string[]; approval_chain?: boolean;
  instructions?: string; form?: Field[]; values?: Record<string, unknown>;
  condition?: { path: string; operator: 'eq' | 'ne' | 'gt' | 'gte' | 'lt' | 'lte' | 'contains' | 'exists'; value: unknown } | null;
  delay_seconds?: number; event_name?: string | null; adapter?: string | null; resource_id?: string | null;
  child_version_id?: string | null; iterations?: number; compensation_adapter?: string | null;
  financial_category?: 'purchase' | 'expense' | null; amount_variable?: string | null; currency?: string;
}
export interface FlowNode {
  id: string; type: Kind; label: string; x: number; y: number; risk?: Risk; priority?: number;
  deadline_seconds?: number; timeout_seconds?: number; max_attempts?: number; config: NodeConfig;
}
export interface Edge { source: string; target: string; branch?: 'always' | 'true' | 'false' }
export interface Spec {
  schema_version?: 1; nodes: FlowNode[]; edges: Edge[]; input_form?: Field[];
  trigger?: {type: 'manual' | 'event' | 'schedule' | 'interval'; event_name?: string | null; interval_seconds?: number | null; start_at?: string | null};
  max_concurrent_runs?: number; max_parallel_steps?: number; source?: 'human' | 'ai' | 'template';
}
export interface Scope { id: string; name: string; can_manage: boolean }
export interface Member { id: string; email: string; role: string }
export interface Adapter { name: string; label: string; module: string; allowed_risks: Risk[]; idempotent: boolean }
export interface Template { key: string; name: string; description: string; spec: Spec; category?: string }
export interface Workflow {
  id: string; scope_id: string; owner_id: string; name: string; description: string; latest_version: number;
  active_version: number | null; enabled: boolean; updated_at: string; latest_status?: string; latest_version_id?: string; can_edit?: boolean;
}
export interface Version { id: string; number: number; status: string; author_id: string; digest: string; spec: Spec; created_at: string }
export interface Review { id: string; actor_id: string; approved: boolean; version_id: string; created_at: string }
export interface WorkflowDetail { workflow: Workflow; versions: Version[]; reviews: Review[] }
export interface Run {
  id: string; workflow_id: string; version_id: string; requester_id: string; status: string; simulation: boolean;
  created_at: string; started_at: string | null; finished_at: string | null; failure_code: string | null;
  phase?: string; origin?: string | null; origin_ref?: string | null; blocked_reason?: string | null;
  last_verified_node?: string | null; deadline_at?: string | null; plan?: Array<{ node_id: string; type: string; label?: string | null; handler?: string | null; permission?: string | null }> | null;
}
export interface Step {
  id: string; run_id: string; node_id: string; status: string; assignee_id: string | null; attempts: number;
  deadline_at: string | null; started_at: string | null; finished_at: string | null; failure_code: string | null;
  output?: Record<string, unknown> | null; escalated_at: string | null;
  phase?: string; acting_user_id?: string | null; verified_at?: string | null; recoveries?: number;
}
export interface Event { id: string; sequence: number; kind: string; node_id: string | null; created_at: string; detail: Record<string,unknown>; hash: string }
export interface RunDetail { run: Run; spec: Spec; inputs: Record<string,unknown>; steps: Step[]; events: Event[]; audit_verified: boolean; decisions: Array<{id:string;actor_id:string;approved:boolean;evidence:Record<string,unknown>}> }
export interface Notice { id: string; step_id: string; kind: string; read_at: string | null; created_at: string }
export interface Dashboard { counts: Record<string,number>; total_runs: number; completion_rate: number | null; overdue_steps: number; waiting_count: number; queue: Step[]; notices: Notice[]; recommendations: Array<{code:string;message:string;evidence_count:number}>; as_of: string }
export interface Validation { code: string; message: string; node_id: string | null }
export interface Page<T> { total: number; offset: number; limit: number; runs?: T[]; workflows?: T[] }
