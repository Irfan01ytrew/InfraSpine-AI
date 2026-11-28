"""Registered action handlers for `action` nodes (step E: one workflow runtime).

An action handler does real work for a workflow step on behalf of a *principal*: the person who took the step
(`perform`) or, for `auto` nodes, the requester. The engine guarantees:

* the principal holds the node's platform permission on the workflow's platform scope, checked before the step
  starts and again by `ctx.should_stop()` between items (pause, revoke or an expired membership stop the next
  item, never the middle of one);
* progress is checkpointed (`ctx.checkpoint`) so a crash or pause resumes from the last finished item;
* "done" is not trusted: after `run()` returns `done`, `verify()` must read the target state back before the
  step is marked completed (`verified_at`);
* failures are retried with backoff (`recover` phase) only when the handler says the failure is retryable.

Handlers must be idempotent per item (use `execute_once` for external side effects). Text that came from
documents or messages is data: it never selects a handler, a permission or an approval target.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.access import Principal
from app.platform.execution import VerifiedResult


@dataclass
class ActionContext:
    db: AsyncSession
    cipher: Any
    principal: Principal
    run_id: uuid.UUID
    step_id: uuid.UUID
    group_id: uuid.UUID          # wf05 scope (department access group)
    platform_scope_id: uuid.UUID  # its platform scope (permissions, approvals, executions)
    values: dict
    inputs: dict
    checkpoint: dict
    should_stop: Callable[[], Awaitable[str | None]]
    events: list[tuple[str, dict]] = field(default_factory=list)

    def note(self, kind: str, **detail) -> None:
        """Queue a per-item audit event; the engine appends it to the run's hash-chained evidence."""
        self.events.append((kind, detail))


@dataclass(frozen=True)
class ActionOutcome:
    status: Literal['more', 'waiting', 'done', 'failed']
    output: dict = field(default_factory=dict)
    failure_code: str | None = None
    retryable: bool = False
    poll_seconds: int = 5


class ActionHandler(Protocol):
    name: str
    label: str
    module: str
    permission: str | None

    async def run(self, ctx: ActionContext) -> ActionOutcome: ...

    async def verify(self, ctx: ActionContext, output: dict) -> VerifiedResult: ...


HANDLERS: dict[str, ActionHandler] = {}


def register(handler: ActionHandler) -> ActionHandler:
    if handler.name in HANDLERS and HANDLERS[handler.name] is not handler:
        raise ValueError(f'Action handler {handler.name} is already registered.')
    HANDLERS[handler.name] = handler
    return handler


def load_builtin_handlers() -> dict[str, ActionHandler]:
    """Import modules that register handlers (kept lazy to avoid import cycles)."""
    from app.workflows import builtin_actions  # noqa: F401
    from app.finance import handlers  # noqa: F401
    from app.agent_studio import unified as studio_unified  # noqa: F401
    from app.services import agent_action_runtime  # noqa: F401
    from app.voice import unified as voice_unified  # noqa: F401
    return HANDLERS


def get_handler(name: str | None) -> ActionHandler | None:
    if not name:
        return None
    load_builtin_handlers()
    return HANDLERS.get(name)


def catalog() -> list[dict[str, Any]]:
    return [{'name': h.name, 'label': h.label, 'module': h.module, 'permission': h.permission}
            for h in sorted(load_builtin_handlers().values(), key=lambda item: item.name)]
