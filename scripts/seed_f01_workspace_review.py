"""Seed one synthetic Admin into a fresh, isolated F01 workspace review DB."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.security import hash_password, verify_password
from app.db.models import ROLE_ADMIN, User
from app.db.session import create_engine, create_sessionmaker


EXPECTED_DATABASE = "infraspine_f01workspace_20261028_test"


async def seed() -> None:
    database_url = os.environ["DATABASE_URL"]
    url = make_url(database_url)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != "127.0.0.1"
        or url.port != 55928
        or url.username != "postgres"
        or url.password is not None
        or url.database != EXPECTED_DATABASE
    ):
        raise RuntimeError("F01 seeding is pinned to its isolated loopback review database.")

    fixture_path = Path(os.environ["INFRASPINE_F01_CREDENTIAL_FIXTURE"]).resolve(strict=True)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8-sig"))
    admin = fixture.get("admin")
    if not isinstance(admin, dict) or not admin.get("email") or len(str(admin.get("password", ""))) < 24:
        raise RuntimeError("The protected synthetic Admin fixture is incomplete.")

    engine = create_engine(database_url, pool_size=1, max_overflow=0)
    sessions = create_sessionmaker(engine)
    try:
        async with sessions() as db:
            user_count = await db.scalar(select(func.count()).select_from(User))
            email = str(admin["email"]).casefold()
            if user_count == 0:
                db.add(
                    User(
                        email=email,
                        password_hash=hash_password(str(admin["password"])),
                        role=ROLE_ADMIN,
                        is_active=True,
                    )
                )
                await db.commit()
            elif user_count == 1:
                existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
                if existing is None or existing.role != ROLE_ADMIN or not existing.is_active or not verify_password(
                    existing.password_hash, str(admin["password"])
                ):
                    raise RuntimeError("The retained synthetic Admin does not match the protected fixture.")
            else:
                raise RuntimeError("The F01 review database contains unexpected account rows.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
    print("F01_SYNTHETIC_ADMIN_SEEDED=1")
