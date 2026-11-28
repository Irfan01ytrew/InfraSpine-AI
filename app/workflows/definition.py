"""Bounded, executable graph schema. No eval, arbitrary code or executable URLs."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

NodeKind = Literal['start', 'end', 'task', 'human', 'approval', 'decision', 'parallel',
                   'timer', 'event', 'agent', 'integration', 'subworkflow', 'loop']
Risk = Literal['low', 'medium', 'high', 'critical']
KINDS = ('start', 'end', 'task', 'human', 'approval', 'decision', 'parallel', 'timer',
         'event', 'agent', 'integration', 'subworkflow', 'loop')
TERMINAL = {'completed', 'skipped', 'cancelled', 'compensated'}


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class FormField(Strict):
    name: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
    label: str = Field(min_length=1, max_length=120)
    type: Literal['text', 'number', 'boolean', 'choice'] = 'text'
    required: bool = True
    options: list[str] = Field(default_factory=list, max_length=30)


class Condition(Strict):
    path: str = Field(pattern=r'^(input|steps)(\.[A-Za-z0-9_-]+){1,8}$')
    operator: Literal['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains', 'exists'] = 'eq'
    value: Any = None


class NodeConfig(Strict):
    assignee_id: UUID | None = None
    assignee_variable: str | None = Field(default=None, pattern=r'^input\.[A-Za-z][A-Za-z0-9_]{0,63}$')
    approver_ids: list[UUID] = Field(default_factory=list, max_length=10)
    approval_chain: bool = False
    instructions: str = Field(default='', max_length=4000)
    form: list[FormField] = Field(default_factory=list, max_length=30)
    values: dict[str, Any] = Field(default_factory=dict)
    condition: Condition | None = None
    delay_seconds: int = Field(default=0, ge=0, le=31536000)
    event_name: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_.-]{0,79}$')
    adapter: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_.-]{0,79}$')
    resource_id: UUID | None = None
    child_version_id: UUID | None = None
    iterations: int = Field(default=1, ge=1, le=100)
    compensation_adapter: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_.-]{0,79}$')
    financial_category: Literal['purchase', 'expense'] | None = None
    amount_variable: str | None = Field(default=None, pattern=r'^input\.[A-Za-z][A-Za-z0-9_]{0,63}$')
    currency: str = Field(default='USD', pattern=r'^[A-Z]{3}$')


class Node(Strict):
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    type: NodeKind
    label: str = Field(min_length=1, max_length=120)
    x: float = Field(default=100, ge=0, le=12000, allow_inf_nan=False)
    y: float = Field(default=100, ge=0, le=12000, allow_inf_nan=False)
    risk: Risk = 'low'
    priority: int = Field(default=0, ge=0, le=100)
    deadline_seconds: int = Field(default=86400, ge=1, le=31536000)
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    config: NodeConfig = Field(default_factory=NodeConfig)


class Edge(Strict):
    source: str
    target: str
    branch: Literal['always', 'true', 'false'] = 'always'


class Trigger(Strict):
    type: Literal['manual', 'event', 'schedule', 'interval'] = 'manual'
    event_name: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_.-]{0,79}$')
    interval_seconds: int | None = Field(default=None, ge=60, le=31536000)
    start_at: str | None = None


class Definition(Strict):
    schema_version: Literal[1] = 1
    nodes: list[Node] = Field(min_length=2, max_length=100)
    edges: list[Edge] = Field(max_length=400)
    input_form: list[FormField] = Field(default_factory=list, max_length=30)
    trigger: Trigger = Field(default_factory=Trigger)
    max_concurrent_runs: int = Field(default=10, ge=1, le=100)
    max_parallel_steps: int = Field(default=10, ge=1, le=32)
    source: Literal['human', 'ai', 'template'] = 'human'

    @model_validator(mode='after')
    def bounded(self):
        bounded_data(self.model_dump(mode='json'), limit=131072)
        return self


def bounded_data(value: Any, *, limit: int = 65536) -> None:
    def visit(item, depth=0):
        if depth > 20:
            raise ValueError('Data nesting exceeds 20 levels.')
        if isinstance(item, dict):
            for key, val in item.items():
                if re.sub(r'[^a-z]', '', key.lower()) in {
                    'password', 'secret', 'apikey', 'accesstoken', 'authorization', 'privatekey',
                }:
                    raise ValueError('Use a module-owned credential reference; do not embed secrets.')
                visit(val, depth + 1)
        elif isinstance(item, list):
            for val in item:
                visit(val, depth + 1)
    visit(value)
    if len(json.dumps(value, allow_nan=False, ensure_ascii=False).encode()) > limit:
        raise ValueError('Workflow data exceeds its size limit.')


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def validate_graph(spec: Definition) -> list[dict]:
    errors: list[dict] = []
    def error(code, message, node_id=None):
        errors.append({'code': code, 'message': message, 'node_id': node_id})
    nodes = {n.id: n for n in spec.nodes}
    if len(nodes) != len(spec.nodes):
        error('duplicate_node', 'Node IDs must be unique.')
    starts = [n.id for n in spec.nodes if n.type == 'start']
    ends = {n.id for n in spec.nodes if n.type == 'end'}
    if len(starts) != 1 or not ends:
        error('terminals', 'Exactly one start and at least one end are required.')
    incoming = {key: [] for key in nodes}
    outgoing = {key: [] for key in nodes}
    seen = set()
    for e in spec.edges:
        if e.source not in nodes or e.target not in nodes:
            error('missing_node', 'A connector references a missing node.')
            continue
        if (e.source, e.target, e.branch) in seen or e.source == e.target:
            error('invalid_edge', 'Duplicate connectors and self loops are not allowed.', e.source)
        seen.add((e.source, e.target, e.branch))
        incoming[e.target].append(e)
        outgoing[e.source].append(e)
    pending = {key: len(val) for key, val in incoming.items()}
    queue = [key for key, count in pending.items() if count == 0]
    ordered = []
    while queue:
        key = queue.pop()
        ordered.append(key)
        for e in outgoing[key]:
            pending[e.target] -= 1
            if pending[e.target] == 0:
                queue.append(e.target)
    if len(ordered) != len(nodes):
        error('cycle', 'Graph cycles are unsafe; use a bounded loop with a pinned subworkflow.')
    reached = set(starts)
    for _ in nodes:
        reached.update(e.target for e in spec.edges if e.source in reached and e.target in nodes)
    for n in spec.nodes:
        if n.id not in reached:
            error('unreachable', 'Node is not reachable from start.', n.id)
        if n.type != 'end' and not outgoing[n.id]:
            error('dead_end', 'Connect this node to an executable end path.', n.id)
        if n.type == 'start' and incoming[n.id] or n.type == 'end' and outgoing[n.id]:
            error('terminal_edge', 'Start cannot have inputs; end cannot have outputs.', n.id)
        if n.type == 'decision':
            if n.config.condition is None or {e.branch for e in outgoing[n.id]} != {'true', 'false'}:
                error('decision_branches', 'Decision requires a condition and true/false connectors.', n.id)
        elif any(e.branch != 'always' for e in outgoing[n.id]):
            error('unexpected_branch', 'Only decision nodes can have conditional connectors.', n.id)
        if n.type == 'human' and not (n.config.assignee_id or n.config.assignee_variable):
            error('owner_missing', 'Human tasks require an assignee or input assignee variable.', n.id)
        if n.type in {'agent', 'integration'} and not n.config.adapter:
            error('adapter_missing', 'Select a registered module adapter.', n.id)
        if n.type in {'subworkflow', 'loop'} and not n.config.child_version_id:
            error('child_missing', 'Pin a published subworkflow version.', n.id)
        if n.type == 'event' and not n.config.event_name:
            error('event_missing', 'Choose the event to wait for.', n.id)
        if n.config.financial_category and (not n.config.amount_variable or n.risk not in {'high', 'critical'}):
            error('financial_policy', 'Financial actions require an amount variable and high/critical approval.', n.id)
        if len(set(n.config.approver_ids)) != len(n.config.approver_ids):
            error('duplicate_approver', 'Approval authorities must be distinct.', n.id)
        if n.risk == 'critical' and n.config.approver_ids and len(n.config.approver_ids) < 2:
            error('critical_quorum', 'Critical actions require two independent authorities.', n.id)
        if n.config.compensation_adapter and n.type not in {'agent', 'integration'}:
            error('compensation_type', 'Only dispatched module actions have compensating adapters.', n.id)
    trigger = spec.trigger
    if trigger.type == 'interval' and not trigger.interval_seconds:
        error('schedule_interval', 'A recurring trigger requires an interval.')
    if trigger.type in {'interval', 'schedule'}:
        from datetime import datetime
        try:
            if not trigger.start_at or datetime.fromisoformat(trigger.start_at).utcoffset() is None:
                raise ValueError()
        except ValueError:
            error('schedule_start', 'Schedule start must be an ISO timestamp with an explicit timezone.')
    if trigger.type == 'event' and not trigger.event_name:
        error('trigger_event', 'An event trigger requires an event name.')
    for fields in [spec.input_form, *(n.config.form for n in spec.nodes)]:
        if len({f.name for f in fields}) != len(fields):
            error('duplicate_field', 'Form field names must be unique.')
    return errors


def lookup(path: str, context: dict) -> Any:
    value: Any = context
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def resolve(value: Any, context: dict) -> Any:
    if isinstance(value, str) and value.startswith('$'):
        return lookup(value[1:], context)
    if isinstance(value, dict):
        return {k: resolve(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, context) for v in value]
    return value


def evaluate(condition: Condition, context: dict) -> bool:
    left, right = lookup(condition.path, context), condition.value
    op = condition.operator
    if op == 'exists': return left is not None
    if op == 'eq': return type(left) is type(right) and left == right
    if op == 'ne': return type(left) is not type(right) or left != right
    if op == 'contains': return isinstance(left, (str, list)) and type(right) in (str, int, float, bool) and right in left
    if isinstance(left, bool) or isinstance(right, bool) or not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    return {'gt': left > right, 'gte': left >= right, 'lt': left < right, 'lte': left <= right}[op]


def validate_form(fields: list[FormField], data: dict) -> None:
    bounded_data(data)
    for f in fields:
        value = data.get(f.name)
        if value is None or value == '':
            if f.required: raise ValueError(f'{f.label} is required.')
            continue
        valid = (f.type == 'text' and isinstance(value, str) and len(value) <= 4000
                 or f.type == 'number' and type(value) in (int, float)
                 or f.type == 'boolean' and isinstance(value, bool)
                 or f.type == 'choice' and value in f.options)
        if not valid: raise ValueError(f'{f.label} has an invalid value.')
