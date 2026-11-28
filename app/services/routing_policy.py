"""Step T slice 4: the stored routing policy for the smart model router.

The policy is one admin-editable row (``routing_policy_settings``, id = 1,
like ``OrgSettings``) that says which model the router picks, per task class,
and how much the organisation may spend on routed calls each month:

- ``default_policy``: cheapest / fastest / balanced (default: cheapest).
- ``task_class_policies``: per-task-class overrides, e.g. complex -> balanced.
- ``capability_tiers``: per-model reasoning-tier overrides, keyed
  ``"provider:model"`` (1 = fast/cheap, 2 = standard, 3 = flagship).
  Overrides the ``_capability_tier`` heuristic in ``llm_runtime``; the
  provider catalog has no tier field, so the admin supplies it here.
- ``monthly_budget_usd``: the org-wide monthly budget for routed calls.
  ``None`` means no budget is enforced (the guard always allows).
- ``department_budgets``: per-department monthly budgets, keyed by the
  department AccessGroup id. A department's own budget wins over the
  org-wide one; a department without an entry uses the org-wide budget.

A caller's department is their first department AccessGroup by name (the
same convention Step N uses for cost attribution, via
``app.agent_studio.unified.scope_for_owner``). A caller with no department
is charged against the org-wide budget.

Pure functions only in the validation/effective helpers: no I/O, no secrets.
The policy contains no credentials, keys or endpoints, so it is safe to
return from the admin API and to record in the audit log.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccessGroup, RoutingPolicySetting, User
from app.services.model_router import RoutingPolicy, TaskClass

logger = logging.getLogger(__name__)

__all__ = [
    "TASK_CLASSES",
    "POLICIES",
    "DEFAULT_CONFIG",
    "RoutingPolicyConfig",
    "RoutingContext",
    "department_for_user",
    "effective_budget_usd",
    "effective_capability_tier",
    "effective_policy",
    "load_routing_policy",
    "parse_routing_policy",
    "routing_context_for_user",
    "save_routing_policy",
    "validate_routing_policy",
]

TASK_CLASSES = tuple(task.value for task in TaskClass)
POLICIES = tuple(policy.value for policy in RoutingPolicy)

# A monthly budget above this is almost certainly a typo (an extra zero).
# The cap is a guard against fat-fingered admin input, not a product limit.
MAX_MONTHLY_BUDGET_USD = Decimal("1000000")


@dataclass(frozen=True)
class RoutingPolicyConfig:
    """The stored routing policy. Ids, policies and amounts only."""

    default_policy: RoutingPolicy = RoutingPolicy.CHEAPEST
    task_class_policies: dict[TaskClass, RoutingPolicy] = field(default_factory=dict)
    capability_tiers: dict[str, int] = field(default_factory=dict)
    monthly_budget_usd: Decimal | None = None
    department_budgets: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutingContext:
    """Everything one routed request needs: the policy, the caller's
    department, and the monthly budget that applies to them."""

    config: RoutingPolicyConfig
    department_id: uuid.UUID | None
    monthly_budget_usd: Decimal | None


DEFAULT_CONFIG = RoutingPolicyConfig()


def _budget_problems(value, *, field_name: str) -> list[str]:
    """Problems with one budget value (None = no budget, always fine)."""
    if value is None:
        return []
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return [f"{field_name} must be a number (USD per month) or null."]
    if amount.is_nan() or amount.is_infinite():
        return [f"{field_name} must be a finite number."]
    if amount < 0:
        return [f"{field_name} must not be negative."]
    if amount > MAX_MONTHLY_BUDGET_USD:
        return [f"{field_name} must not exceed ${MAX_MONTHLY_BUDGET_USD}."]
    return []


def _as_budget(value) -> Decimal | None:
    """Parse an already-validated budget value."""
    if value is None:
        return None
    return Decimal(str(value))


def validate_routing_policy(data) -> list[str]:
    """Problems with a raw policy document (empty means valid). Pure."""
    if not isinstance(data, dict):
        return ["Routing policy must be an object."]
    problems: list[str] = []
    unknown = sorted(set(data) - {
        "default_policy", "task_class_policies", "capability_tiers",
        "monthly_budget_usd", "department_budgets",
    })
    if unknown:
        problems.append(f"Unknown routing policy field(s): {', '.join(unknown)}.")
    default = data.get("default_policy", RoutingPolicy.CHEAPEST.value)
    if default not in POLICIES:
        problems.append(
            f"default_policy must be one of {', '.join(POLICIES)} (got {default!r})."
        )
    overrides = data.get("task_class_policies") or {}
    if not isinstance(overrides, dict):
        problems.append("task_class_policies must be an object.")
    else:
        for task_class, policy in overrides.items():
            if task_class not in TASK_CLASSES:
                problems.append(
                    f"task_class_policies has unknown task class {task_class!r} "
                    f"(known: {', '.join(TASK_CLASSES)})."
                )
            elif policy not in POLICIES:
                problems.append(
                    f"task_class_policies[{task_class!r}] must be one of "
                    f"{', '.join(POLICIES)} (got {policy!r})."
                )
    tiers = data.get("capability_tiers") or {}
    if not isinstance(tiers, dict):
        problems.append("capability_tiers must be an object.")
    else:
        for key, tier in tiers.items():
            parts = key.split(":") if isinstance(key, str) else []
            if len(parts) != 2 or not all(part.strip() for part in parts):
                problems.append(
                    f"capability_tiers key {key!r} must look like "
                    "'provider:model'."
                )
            if isinstance(tier, bool) or not isinstance(tier, int) or tier not in (1, 2, 3):
                problems.append(
                    f"capability_tiers[{key!r}] must be 1, 2 or 3 (got {tier!r})."
                )
    problems.extend(_budget_problems(data.get("monthly_budget_usd"), field_name="monthly_budget_usd"))
    dept_budgets = data.get("department_budgets") or {}
    if not isinstance(dept_budgets, dict):
        problems.append("department_budgets must be an object.")
    else:
        for department_id, amount in dept_budgets.items():
            try:
                uuid.UUID(str(department_id))
            except (ValueError, TypeError):
                problems.append(
                    f"department_budgets key {department_id!r} must be a department id."
                )
            problems.extend(
                _budget_problems(amount, field_name=f"department_budgets[{department_id!r}]")
            )
    return problems


def parse_routing_policy(data) -> RoutingPolicyConfig:
    """Validate a raw policy document and build the config. Pure.

    Raises ``ValueError`` listing every problem, so the admin API can
    return them all at once instead of failing on the first one."""
    problems = validate_routing_policy(data)
    if problems:
        raise ValueError("Invalid routing policy: " + "; ".join(problems))
    return RoutingPolicyConfig(
        default_policy=RoutingPolicy(data.get("default_policy", RoutingPolicy.CHEAPEST.value)),
        task_class_policies={
            TaskClass(task_class): RoutingPolicy(policy)
            for task_class, policy in (data.get("task_class_policies") or {}).items()
        },
        capability_tiers=dict(data.get("capability_tiers") or {}),
        monthly_budget_usd=_as_budget(data.get("monthly_budget_usd")),
        department_budgets={
            str(department_id): _as_budget(amount)
            for department_id, amount in (data.get("department_budgets") or {}).items()
        },
    )


def effective_policy(config: RoutingPolicyConfig, task_class: TaskClass) -> RoutingPolicy:
    """The policy for one task class: its override, else the default."""
    return config.task_class_policies.get(task_class, config.default_policy)


def effective_budget_usd(
    config: RoutingPolicyConfig, department_id: uuid.UUID | None
) -> Decimal | None:
    """The monthly budget for a caller: their department's own budget when
    one is configured, else the org-wide budget, else None (unenforced)."""
    if department_id is not None:
        own = config.department_budgets.get(str(department_id))
        if own is not None:
            return own
    return config.monthly_budget_usd


def effective_capability_tier(
    config: RoutingPolicyConfig, provider: str, model: str, default_tier: int
) -> int:
    """A model's reasoning tier: the admin override when set, else the
    caller's heuristic tier."""
    return config.capability_tiers.get(f"{provider}:{model}", default_tier)


def _config_from_row(row: RoutingPolicySetting) -> RoutingPolicyConfig:
    """Parse a stored row. A corrupt row must never break chat: log and
    fall back to the default policy."""
    try:
        return parse_routing_policy({
            "default_policy": row.default_policy,
            "task_class_policies": row.task_class_policies or {},
            "capability_tiers": row.capability_tiers or {},
            "monthly_budget_usd": (
                str(row.monthly_budget_usd) if row.monthly_budget_usd is not None else None
            ),
            "department_budgets": row.department_budgets or {},
        })
    except ValueError:
        logger.warning("Stored routing policy is invalid; using the default policy", exc_info=True)
        return DEFAULT_CONFIG


async def load_routing_policy(db: AsyncSession) -> RoutingPolicyConfig:
    """The stored policy, or the default when no admin has saved one yet."""
    row = await db.get(RoutingPolicySetting, 1)
    if row is None:
        return DEFAULT_CONFIG
    return _config_from_row(row)


async def save_routing_policy(
    db: AsyncSession, config: RoutingPolicyConfig, *, updated_by: uuid.UUID | None = None
) -> RoutingPolicySetting:
    """Upsert the singleton policy row (id = 1). Flushes; the caller commits."""
    row = await db.get(RoutingPolicySetting, 1)
    if row is None:
        row = RoutingPolicySetting(id=1)
        db.add(row)
    row.default_policy = config.default_policy.value
    row.task_class_policies = {
        task_class.value: policy.value for task_class, policy in config.task_class_policies.items()
    }
    row.capability_tiers = dict(config.capability_tiers)
    row.monthly_budget_usd = config.monthly_budget_usd
    row.department_budgets = {
        department_id: str(amount) for department_id, amount in config.department_budgets.items()
    }
    row.updated_by = updated_by
    await db.flush()
    return row


async def department_for_user(db: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    """The caller's department: their first department AccessGroup by name
    (the Step N cost-attribution convention). Best effort -- a caller with
    no department, or a lookup failure, resolves to None and the org-wide
    budget applies. Never raises: routing must not break chat.

    Takes the user id, not the user object: an ORM user expires after a
    session commit, and reading its attributes afterwards raises
    MissingGreenlet in async SQLAlchemy. Loading the owner fresh here
    keeps the lookup safe no matter what the caller did before.
    """
    try:
        from app.agent_studio.unified import scope_for_owner

        owner = await db.get(User, user_id)
        if owner is None:
            return None
        return await scope_for_owner(db, owner)
    except Exception:
        logger.debug("Could not resolve a department for routing", exc_info=True)
        return None


async def list_departments(db: AsyncSession) -> list[AccessGroup]:
    """Every department group, by name, for the admin policy editor."""
    return list(
        await db.scalars(
            select(AccessGroup)
            .where(AccessGroup.kind == "department")
            .order_by(AccessGroup.name, AccessGroup.id)
        )
    )


async def routing_context_for_user(
    db: AsyncSession, user_id: uuid.UUID | None, *, department_id: uuid.UUID | None = None
) -> RoutingContext:
    """One load for a routed request: the stored policy, the caller's
    department, and the monthly budget that applies to them. Takes the
    user id (see department_for_user): callers pass user.id while the
    user object is fresh, so a commit earlier in the request cannot
    break the lookup."""
    config = await load_routing_policy(db)
    department = department_id
    if department is None and user_id is not None:
        department = await department_for_user(db, user_id)
    return RoutingContext(
        config=config,
        department_id=department,
        monthly_budget_usd=effective_budget_usd(config, department),
    )
