"""Seed one synthetic administrator into a new isolated PostgreSQL *_test database."""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import User
from app.db.session import test_engine_options


async def main() -> None:
    settings = get_settings()
    database = settings.database_url.get_secret_value().rsplit("/", 1)[-1]
    if not database.endswith("_test"):
        raise SystemExit("Refusing to seed a database not named with the _test suffix.")
    fixture_path = Path(os.environ["INFRASPINE_RETENTION_QA_FIXTURE"])
    fixture = json.loads(fixture_path.read_text(encoding="utf-8-sig"))
    admin = fixture.get("admin") or {}
    if not admin.get("email") or len(admin.get("password", "")) < 24:
        raise SystemExit("Protected synthetic admin fixture is incomplete.")

    engine = create_async_engine(settings.database_url.get_secret_value(), **test_engine_options())
    try:
        async with engine.begin() as conn:
            users = await conn.scalar(select(func.count()).select_from(User))
            if users:
                raise SystemExit("Refusing to seed a non-empty isolated test database.")
            await conn.execute(
                User.__table__.insert().values(
                    email=admin["email"], role="admin", password_hash=hash_password(admin["password"])
                )
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
