"""Finance autopilot (Step R) — scheduled pickup of invoice batches.

The sweep is the only new automation in this slice. It starts workflow runs for
batches that are waiting and wakes runs stalled on an expired approval. It never
creates a payment, never requests or decides an approval, and never calls a bank
connector: payments are created only by the existing approval handler, approvals
are decided only by a human with ``payment.approve``, and transfers are submitted
only by the existing execute handler after a consumed approval, through
``execute_once`` with read-back verification.

Safety summary:
* The sweep user needs ``invoice.ingest`` (to start a batch) and ``finance.read``.
  It must NOT hold ``payment.approve`` or ``funds.transfer``.
* The payee account always comes from the vendor master (see
  ``app.finance.handlers.payment_spec``). Document/invoice text is untrusted and
  can never change a payee or an approval.
* Re-running the sweep is safe: a batch with an active run is skipped, verified
  invoices already bound to a payment are never re-grouped, and starting a batch
  that is already running raises ``ConflictError`` in ``FinanceService.start``.
* ``max_payment_amount`` (per vendor payment group) and ``daily_cap`` (per
  department, newly requested payment total per day) are hard stops: an
  over-limit batch is skipped and recorded, never started.
* **Per-vendor allow-list (slice 4).** Only vendors an administrator marked
  ``autopilot_allowed`` on the vendor master may be auto-picked-up. A batch
  naming a known but unlisted vendor — or no vendor in the master at all —
  is skipped as ``skipped_vendor_not_allowed`` and left for a human to start.
  New vendors are never allow-listed by default. An invoice naming an unknown
  vendor does not block pickup when a vetted vendor is also present:
  verification rejects it (``unknown_vendor``) and it never reaches a payment.
  The flag is set only through the vendor save endpoint (``finance.manage``),
  never by the sweep and never from invoice/document text.
* ``dry_run=True`` returns decisions and writes nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update

from app.core.errors import ConflictError
from app.db.finance_models import FinInvoice, FinInvoiceBatch, FinPayment, FinVendor
from app.db.workflow_engine_models import WorkflowRun, WorkflowStep
from app.finance.service import ACTIVE_RUN, FinanceService
from app.platform.events import audit
from app.platform.models import PlatformApprovalRequest

# Batches the sweep may touch. 'completed'/'failed'/'cancelled' are terminal.
CANDIDATE_STATUSES = ('open', 'verifying', 'awaiting_approval', 'paying')
# Payments that already count towards the daily cap (requested or paid today).
COUNTED_PAYMENT_STATUSES = ('awaiting_approval', 'approved', 'dispatched', 'verified')


@dataclass(frozen=True)
class AutopilotPolicy:
    """Per-sweep policy. ``enabled=False`` (the default) means the sweep does nothing."""

    enabled: bool = False
    dry_run: bool = False
    max_payment_amount: Decimal | None = None  # per (vendor, currency) payment group
    daily_cap: Decimal | None = None           # per department, per calendar day (UTC)
    departments: tuple[uuid.UUID, ...] | None = None  # None = every readable department


@dataclass(frozen=True)
class SweepDecision:
    batch_id: uuid.UUID
    action: str  # started | would_start | woken | skipped_active | skipped_no_work | skipped_over_limit
                # | skipped_vendor_not_allowed
    reason: str
    amount: Decimal = Decimal('0')
    dry_run: bool = False


def policy_from_row(row) -> AutopilotPolicy:
    """Build the sweep policy from an ``OrgSettings`` row (slice 2).

    ``getattr`` defaults keep a row written before migration 0104 readable: a
    pre-migration installation sweeps nothing until an administrator enables it.
    """
    if row is None:
        return AutopilotPolicy()
    return AutopilotPolicy(
        enabled=bool(getattr(row, "finance_autopilot_enabled", False)),
        dry_run=bool(getattr(row, "finance_autopilot_dry_run", False)),
        max_payment_amount=(Decimal(getattr(row, "finance_autopilot_max_payment_amount"))
                            if getattr(row, "finance_autopilot_max_payment_amount", None) is not None else None),
        daily_cap=(Decimal(getattr(row, "finance_autopilot_daily_cap"))
                   if getattr(row, "finance_autopilot_daily_cap", None) is not None else None),
    )


async def load_policy(db) -> AutopilotPolicy:
    """Load the autopilot policy from the single org_settings row (id = 1)."""
    from app.platform.models import OrgSettings

    row = await db.get(OrgSettings, 1, populate_existing=True)
    return policy_from_row(row)


def group_amounts(invoices: list[FinInvoice]) -> dict[tuple[uuid.UUID, str], Decimal]:
    """Group verified invoices the way the approval handler groups payments:
    per (vendor, currency). Pure function, no database access."""
    totals: dict[tuple[uuid.UUID, str], Decimal] = {}
    for inv in invoices:
        if inv.vendor_id is None or inv.amount is None:
            continue
        key = (inv.vendor_id, inv.currency or '')
        totals[key] = totals.get(key, Decimal('0')) + Decimal(inv.amount)
    return totals


def disallowed_vendors(invoices, allowed_ids, vendors_by_name_key) -> tuple[list[uuid.UUID], bool]:
    """Step R slice 4 (pure): which vendors on these invoices are NOT on the
    autopilot allow-list, and whether the batch names no known vendor at all.

    ``invoices`` carry ``.vendor_id`` (set by verification) and
    ``.vendor_name`` (set at intake). ``allowed_ids`` holds the ids of
    allow-listed vendors; ``vendors_by_name_key`` maps ``name_key(name)`` to
    the vendor id for vendors named on still-pending invoices (intake never
    sets ``vendor_id``, so a pending invoice can only be judged by name).

    Returns ``(disallowed, no_known_vendor)``: ``disallowed`` is the sorted
    list of vendor ids the invoices resolve to that are not allow-listed;
    ``no_known_vendor`` is True when none of the invoices resolves to a
    vendor in the master — every invoice names an unknown vendor or no vendor
    (a new vendor by definition). Either being non-empty means the sweep must
    not pick the batch up. An invoice naming an unknown vendor does NOT block
    pickup on its own when a vetted vendor is also present: verification
    rejects the unknown one (``unknown_vendor``) and it never reaches a
    payment — the allow-list gates vendors that can be paid, not typos.
    """
    from app.finance.verification import name_key

    allowed = set(allowed_ids or ())
    vendor_ids: set[uuid.UUID] = set()
    for inv in invoices:
        if inv.vendor_id is not None:
            vendor_ids.add(inv.vendor_id)
        elif inv.vendor_name:
            resolved = (vendors_by_name_key or {}).get(name_key(inv.vendor_name))
            if resolved is not None:
                vendor_ids.add(resolved)
    return sorted(v for v in vendor_ids if v not in allowed), not vendor_ids


async def _active_run(db, batch: FinInvoiceBatch) -> WorkflowRun | None:
    if batch.wf_run_id is None:
        return None
    run = await db.get(WorkflowRun, batch.wf_run_id, populate_existing=True)
    return run if run is not None and run.status in ACTIVE_RUN else None


async def _unpaid_verified(db, batch: FinInvoiceBatch) -> list[FinInvoice]:
    return list((await db.scalars(select(FinInvoice).where(
        FinInvoice.batch_id == batch.id, FinInvoice.status == 'verified',
        FinInvoice.payment_id.is_(None)).order_by(FinInvoice.seq))).all())


async def _pending_invoices(db, batch: FinInvoiceBatch) -> list[FinInvoice]:
    return list((await db.scalars(select(FinInvoice).where(
        FinInvoice.batch_id == batch.id, FinInvoice.status == 'pending').order_by(FinInvoice.seq))).all())


async def _requested_today(db, group_id: uuid.UUID) -> Decimal:
    """Amount of payments already requested/paid today in this department (UTC day)."""
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    total = await db.scalar(select(func.coalesce(func.sum(FinPayment.amount), Decimal('0'))).where(
        FinPayment.group_id == group_id, FinPayment.created_at >= start,
        FinPayment.status.in_(COUNTED_PAYMENT_STATUSES)))
    return Decimal(total or 0)


async def _expired_approvals(db, batch: FinInvoiceBatch) -> bool:
    """True when this batch has a payment whose approval request is pending but expired."""
    now = datetime.now(UTC)
    row = await db.scalar(select(PlatformApprovalRequest.id).join(
        FinPayment, FinPayment.approval_id == PlatformApprovalRequest.id).where(
            FinPayment.batch_id == batch.id, PlatformApprovalRequest.status == 'pending',
            PlatformApprovalRequest.expires_at <= now).limit(1))
    return row is not None


async def sweep(db, user, settings, policy: AutopilotPolicy, *, cipher=None) -> list[SweepDecision]:
    """One autopilot pass. Returns one decision per candidate batch (empty when disabled).

    The sweep user must hold ``invoice.ingest`` in the departments it sweeps; it must
    not hold ``payment.approve`` or ``funds.transfer``. Every decision is recorded in
    the audit log as ``finance.autopilot_sweep``.
    """
    if not policy.enabled:
        return []
    service = FinanceService(db, user, settings, cipher)
    groups = await service.readable_groups('finance.read')
    if policy.departments is not None:
        wanted = set(policy.departments)
        groups = [g for g in groups if g in wanted]
    if not groups:
        return []

    decisions: list[SweepDecision] = []
    requested_today: dict[uuid.UUID, Decimal] = {}
    for batch in (await db.scalars(select(FinInvoiceBatch).where(
            FinInvoiceBatch.group_id.in_(groups), FinInvoiceBatch.status.in_(CANDIDATE_STATUSES))
            .order_by(FinInvoiceBatch.created_at))).all():
        now = datetime.now(UTC)
        run = await _active_run(db, batch)
        if run is not None:
            if batch.status == 'awaiting_approval' and await _expired_approvals(db, batch):
                if not policy.dry_run:
                    await db.execute(update(WorkflowRun).where(WorkflowRun.id == run.id).values(wake_at=now))
                    await db.execute(update(WorkflowStep).where(
                        WorkflowStep.run_id == run.id, WorkflowStep.status == 'waiting')
                        .values(available_at=now))
                    await db.commit()
                decisions.append(SweepDecision(batch.id, 'woken', 'approval expired; run woken for re-request',
                                               dry_run=policy.dry_run))
            else:
                decisions.append(SweepDecision(batch.id, 'skipped_active', 'batch already has an active run',
                                               dry_run=policy.dry_run))
            continue

        pending_rows = await _pending_invoices(db, batch)
        unpaid = await _unpaid_verified(db, batch)
        if not pending_rows and not unpaid:
            decisions.append(SweepDecision(batch.id, 'skipped_no_work', 'no pending or verified-unpaid invoices',
                                           dry_run=policy.dry_run))
            continue

        # Step R slice 4: per-vendor allow-list. Only invoices whose vendor an
        # administrator marked autopilot_allowed may be auto-picked-up. A batch
        # naming a known but unlisted vendor — or no vendor in the master at
        # all (a new vendor) — is left for a human to start. Pending invoices
        # carry only a vendor *name* (intake never sets vendor_id; verification
        # does), so they are judged by name; verified-unpaid invoices are
        # judged by id. An invoice naming an unknown vendor does not block
        # pickup when a vetted vendor is also present: verification rejects
        # it and it never reaches a payment. The wake path above is untouched:
        # waking only re-drives a batch that was already started, it never
        # picks a new vendor up.
        from app.finance.verification import name_key

        batch_invoices = pending_rows + unpaid
        vendor_ids = {inv.vendor_id for inv in batch_invoices if inv.vendor_id is not None}
        name_keys = {name_key(inv.vendor_name) for inv in batch_invoices
                     if inv.vendor_id is None and inv.vendor_name}
        named: dict[str, FinVendor] = {}
        if name_keys:
            named = {v.name_key: v for v in await db.scalars(select(FinVendor).where(
                FinVendor.group_id == batch.group_id, FinVendor.name_key.in_(name_keys)))}
        ids_to_check = set(vendor_ids) | {v.id for v in named.values()}
        allowed = set()
        if ids_to_check:
            allowed = set(await db.scalars(select(FinVendor.id).where(
                FinVendor.id.in_(ids_to_check), FinVendor.autopilot_allowed.is_(True))))
        disallowed, no_known_vendor = disallowed_vendors(
            batch_invoices, allowed, {k: v.id for k, v in named.items()})
        if no_known_vendor or disallowed:
            if no_known_vendor:
                reason = ('no vendor on this batch is in the vendor master; '
                          'a new vendor needs a human to start the batch')
            else:
                names = await db.scalars(select(FinVendor.name).where(FinVendor.id.in_(disallowed))
                                         .order_by(FinVendor.name))
                reason = ('vendor(s) not on the autopilot allow-list: ' + ', '.join(names)
                          + '; ask a finance manager to allow-list the vendor first')
            decisions.append(SweepDecision(batch.id, 'skipped_vendor_not_allowed', reason,
                                           dry_run=policy.dry_run))
            continue

        amounts = group_amounts(unpaid)
        new_total = sum(amounts.values(), Decimal('0'))
        over_limit = None
        if policy.max_payment_amount is not None:
            for key, total in amounts.items():
                if total > policy.max_payment_amount:
                    over_limit = (f'payment group {key[0]} ({key[1]}) totals {total}, '
                                  f'over max {policy.max_payment_amount}')
                    break
        if over_limit is None and policy.daily_cap is not None:
            if batch.group_id not in requested_today:
                requested_today[batch.group_id] = await _requested_today(db, batch.group_id)
            if requested_today[batch.group_id] + new_total > policy.daily_cap:
                over_limit = (f'department already requested {requested_today[batch.group_id]} today; '
                              f'this batch would add {new_total}, over daily cap {policy.daily_cap}')
        if over_limit is not None:
            decisions.append(SweepDecision(batch.id, 'skipped_over_limit', over_limit, amount=new_total,
                                           dry_run=policy.dry_run))
            continue

        if policy.dry_run:
            decisions.append(SweepDecision(batch.id, 'would_start', 'dry run; batch would be started',
                                           amount=new_total, dry_run=True))
            continue
        try:
            await service.start(batch.id)
        except ConflictError:
            decisions.append(SweepDecision(batch.id, 'skipped_active',
                                           'batch started elsewhere while sweeping', amount=new_total,
                                           dry_run=policy.dry_run))
            continue
        if policy.daily_cap is not None:
            requested_today[batch.group_id] = requested_today.get(batch.group_id, Decimal('0')) + new_total
        decisions.append(SweepDecision(batch.id, 'started', 'batch workflow started', amount=new_total,
                                       dry_run=policy.dry_run))

    if decisions and not policy.dry_run:
        audit(db, service.p, 'finance.autopilot_sweep', target='autopilot', details={
            'decisions': [{'batch_id': str(d.batch_id), 'action': d.action, 'reason': d.reason[:200]}
                          for d in decisions]})
        await db.commit()
    return decisions


__all__ = ['AutopilotPolicy', 'SweepDecision', 'disallowed_vendors', 'group_amounts', 'load_policy',
           'policy_from_row', 'sweep']
