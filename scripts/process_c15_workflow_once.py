"""Run one current-source approval-gated workflow item on the isolated C15 review DB."""

from __future__ import annotations

import asyncio
import re

from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.db.session import create_engine, create_sessionmaker
from app.services.automation_worker import process_next_workflow_run


async def main() -> None:
    settings = get_settings()
    url = make_url(settings.database_url.get_secret_value())
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host not in {"127.0.0.1", "localhost"}
        or url.port != 55929
        or not re.fullmatch(r"infraspine_c15mywork_[a-f0-9]{8}_review", url.database or "")
    ):
        raise RuntimeError("The C15 one-shot worker only accepts the unique loopback review database.")
    engine = create_engine(settings.database_url.get_secret_value(), pool_size=1, max_overflow=0)
    try:
        sessionmaker = create_sessionmaker(engine)
        processed = await process_next_workflow_run(sessionmaker)
        print(f"C15_WORKFLOW_ITEM_PROCESSED={str(processed).lower()}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
