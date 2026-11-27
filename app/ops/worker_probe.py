"""Bounded probe of this host/container's worker heartbeat, without loading ML libraries."""

import asyncio
import socket

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import create_engine


async def check() -> bool:
    settings = get_settings()
    engine = create_engine(settings.database_url.get_secret_value(), pool_size=1, max_overflow=0, pool_timeout=3)
    try:
        async with asyncio.timeout(5):
            async with engine.connect() as connection:
                return bool(await connection.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM ingestion_workers WHERE hostname = :host "
                    "AND mode = 'worker' AND heartbeat_at >= now() - :seconds * interval '1 second')"
                ), {"host": socket.gethostname()[:255], "seconds": 3 * settings.rag_worker_heartbeat_seconds}))
    except Exception:
        return False
    finally:
        await engine.dispose()


if __name__ == "__main__":
    healthy = asyncio.run(check())
    print("Worker heartbeat current" if healthy else "Worker heartbeat missing or expired")
    raise SystemExit(0 if healthy else 1)
