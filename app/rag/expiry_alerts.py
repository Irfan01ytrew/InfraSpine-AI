"""Step AB slice 3 — daily expiry-alert check for Knowledge documents.

Finds ready documents whose extracted expiry date is past or due soon and
records one alert row per (document, expiry date, checked day). The check is
idempotent: re-running it on the same day inserts nothing new.

Alerts are read-only notices. This module never sends anything: Step AA
includes pending rows in its digest (see ``pending_alerts_for_digest``), and
marks them delivered afterwards (``mark_alerts_delivered``). Delivery must
permission-filter by ``documents.read`` — an alert is visible only to users
who may read the document.

Run once a day, e.g. from cron/systemd:

    python -m app.rag.expiry_alerts
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import DOC_READY, Document, KnowledgeExpiryAlert
from app.rag.auto_enrich import expiry_alerts

logger = logging.getLogger(__name__)

__all__ = [
    "mark_alerts_delivered",
    "pending_alerts_for_digest",
    "run_expiry_alert_check",
]


async def run_expiry_alert_check(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    reference_date: date | None = None,
    warn_days: int = 30,
    limit: int = 1000,
) -> dict:
    """One daily pass over ready documents with an expiry date.

    Returns ``{"checked": n, "expired": n, "due_soon": n, "inserted": n}``.
    Only ``expired`` / ``due_soon`` documents get alert rows; documents whose
    expiry is far away produce no row. Safe to re-run: the unique constraint
    on (document, expiry date, checked day) plus ``ON CONFLICT DO NOTHING``
    makes a same-day re-run insert zero rows.
    """
    ref = reference_date or date.today()
    checked = expired = due_soon = inserted = 0
    async with sessionmaker() as db:
        rows = (
            await db.execute(
                select(Document.id, Document.expiry_date)
                .where(Document.status == DOC_READY, Document.expiry_date.is_not(None))
                .order_by(Document.expiry_date)
                .limit(limit)
            )
        ).all()
        for doc_id, expiry in rows:
            checked += 1
            alerts = expiry_alerts([expiry], reference_date=ref, warn_days=warn_days)
            if not alerts or alerts[0].urgency == "ok":
                continue
            alert = alerts[0]
            if alert.urgency == "expired":
                expired += 1
            else:
                due_soon += 1
            result = await db.execute(
                pg_insert(KnowledgeExpiryAlert)
                .values(
                    id=uuid.uuid4(),
                    document_id=doc_id,
                    expiry_date=expiry,
                    urgency=alert.urgency,
                    days_left=alert.days_left,
                    checked_on=ref,
                    status="pending",
                )
                .on_conflict_do_nothing(
                    constraint="uq_knowledge_expiry_alerts_doc_date_day"
                )
            )
            inserted += result.rowcount or 0
        await db.commit()
    return {"checked": checked, "expired": expired, "due_soon": due_soon, "inserted": inserted}


async def pending_alerts_for_digest(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    checked_on: date | None = None,
    limit: int = 200,
) -> list[dict]:
    """Pending alert rows for the Step AA digest, newest urgency first.

    Each row carries the document's filename and owner so the digest can show
    what is expiring. The caller must permission-filter by ``documents.read``
    before delivering: an alert is visible only to users who may read the
    document it belongs to.
    """
    ref = checked_on or date.today()
    async with sessionmaker() as db:
        rows = (
            await db.execute(
                select(
                    KnowledgeExpiryAlert.id,
                    KnowledgeExpiryAlert.document_id,
                    KnowledgeExpiryAlert.expiry_date,
                    KnowledgeExpiryAlert.urgency,
                    KnowledgeExpiryAlert.days_left,
                    KnowledgeExpiryAlert.checked_on,
                    Document.filename,
                    Document.owner_id,
                )
                .join(Document, Document.id == KnowledgeExpiryAlert.document_id)
                .where(
                    KnowledgeExpiryAlert.checked_on == ref,
                    KnowledgeExpiryAlert.status == "pending",
                )
                .order_by(KnowledgeExpiryAlert.expiry_date)
                .limit(limit)
            )
        ).all()
    return [
        {
            "alert_id": str(row.id),
            "document_id": str(row.document_id),
            "filename": row.filename,
            "owner_id": str(row.owner_id),
            "expiry_date": row.expiry_date.isoformat(),
            "urgency": row.urgency,
            "days_left": row.days_left,
            "checked_on": row.checked_on.isoformat(),
        }
        for row in rows
    ]


async def mark_alerts_delivered(
    sessionmaker: async_sessionmaker[AsyncSession], alert_ids: list[uuid.UUID]
) -> int:
    """Mark alert rows delivered after the Step AA digest sends them."""
    if not alert_ids:
        return 0
    async with sessionmaker() as db:
        result = await db.execute(
            KnowledgeExpiryAlert.__table__.update()
            .where(
                KnowledgeExpiryAlert.id.in_(alert_ids),
                KnowledgeExpiryAlert.status == "pending",
            )
            .values(status="delivered")
        )
        await db.commit()
        return result.rowcount or 0


async def _run_once(warn_days: int) -> dict:
    from app.core.config import get_settings
    from app.db.session import create_engine, create_sessionmaker

    settings = get_settings()
    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_max_overflow,
        pool_timeout=getattr(settings, "database_pool_timeout_seconds", 30),
        pool_recycle=getattr(settings, "database_pool_recycle_seconds", 1800),
    )
    try:
        return await run_expiry_alert_check(create_sessionmaker(engine), warn_days=warn_days)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.rag.expiry_alerts",
        description="Daily Knowledge expiry-alert check (Step AB slice 3).",
    )
    parser.add_argument("--warn-days", type=int, default=30, help="days ahead that count as due soon")
    args = parser.parse_args(argv)
    result = asyncio.run(_run_once(args.warn_days))
    print(
        "expiry-alert check: checked={checked} expired={expired} due_soon={due_soon} "
        "inserted={inserted}".format(**result)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
