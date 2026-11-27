"""Owner-module contract. No shell, arbitrary URL, SQL or LLM-selected tools.

Adapters must check both tenant and owner, honor the supplied deadline, and deduplicate
effects by command ID durably. Registration is operator code, never incident data.
"""
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, text

from app.core.errors import InvalidInputError, NotFoundError
from app.db.models import AutomationRun


@dataclass(frozen=True)
class Context:
    tenant_id: UUID
    owner_id: UUID
    resource: str
    command_id: UUID | None = None
    generation: int = 1


@dataclass(frozen=True)
class Observation:
    healthy: bool
    code: str
    evidence: dict = field(default_factory=dict)


class Adapter(Protocol):
    actions: frozenset[str]
    idempotent: bool

    async def authorize(self, context: Context) -> None: ...
    async def execute(self, context: Context, action: str) -> dict: ...
    async def observe(self, context: Context) -> Observation: ...
    async def impact(self, context: Context) -> dict: ...


class Registry:
    def __init__(self):
        self.adapters: dict[str, Adapter] = {}

    def register(self, name: str, adapter: Adapter):
        if name in self.adapters:
            raise ValueError("Adapter already registered")
        self.adapters[name] = adapter

    def get(self, name: str):
        if name not in self.adapters:
            raise InvalidInputError("The owning module's recovery adapter is not connected.")
        return self.adapters[name]

    def catalog(self):
        return [{"id": key, "actions": sorted(value.actions), "idempotent": value.idempotent} for key, value in self.adapters.items()]


class DatabaseProbe:
    """Read-only SELECT 1 on this deployment's configured database; no user SQL."""
    actions = frozenset({"probe"})
    idempotent = True

    def __init__(self, sessionmaker):
        self.sessionmaker = sessionmaker

    async def authorize(self, context):
        if context.resource != "primary":
            raise NotFoundError("Unknown database health resource")

    async def observe(self, context):
        await self.authorize(context)
        async with self.sessionmaker() as db:
            await db.execute(text("SELECT 1"))
        return Observation(True, "database_connected", {"check": "SELECT 1"})

    async def execute(self, context, action):
        if action != "probe":
            raise InvalidInputError("Only database connectivity checks are supported.")
        return {"code": (await self.observe(context)).code}

    async def impact(self, context):
        return {"status": "unknown", "reason": "Company Graph adapter not connected"}


class AutomationProbe:
    """Read-only bridge to existing Workflow. Does not reset its retry or approval state."""
    actions = frozenset({"probe"})
    idempotent = True

    def __init__(self, sessionmaker):
        self.sessionmaker = sessionmaker

    async def _run(self, context):
        try:
            run_id = UUID(context.resource)
        except ValueError:
            raise InvalidInputError("Workflow resource must be a run UUID") from None
        async with self.sessionmaker() as db:
            row = await db.scalar(select(AutomationRun).where(AutomationRun.id == run_id,
                                      AutomationRun.requester_user_id == context.owner_id))
            if row is None:
                raise NotFoundError("Owned workflow run not found")
            return row

    async def authorize(self, context):
        await self._run(context)

    async def observe(self, context):
        row = await self._run(context)
        return Observation(row.status == "completed", f"workflow_{row.status}", {"status": row.status, "attempts": row.attempts})

    async def execute(self, context, action):
        if action != "probe":
            raise InvalidInputError("Workflow execution remains with Module 05.")
        return {"code": (await self.observe(context)).code}

    async def impact(self, context):
        row = await self._run(context)
        return {"departments": [str(row.department_group_id)], "basis": "workflow ownership", "graph_confirmed": False}


def default_registry(sessionmaker):
    registry = Registry()
    registry.register("database", DatabaseProbe(sessionmaker))
    registry.register("automation", AutomationProbe(sessionmaker))
    return registry
