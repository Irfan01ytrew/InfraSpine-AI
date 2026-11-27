"""Seed one protected synthetic Admin into the dedicated C09 review database."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_DATABASE = "infraspine_c09semanticreview_20261028_test"
EXPECTED_REVIEW_ROOT = ROOT / ".verification-local-review-20261028-c09-semantic"

import sys

sys.path.insert(0, str(ROOT))

from app.core.security import hash_password
from app.db.models import User


def _protect_fixture(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if not powershell:
        raise RuntimeError("PowerShell is required to protect the synthetic Admin fixture ACL.")
    sid = subprocess.check_output(
        [powershell, "-NoProfile", "-Command", "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
        text=True,
        stdin=subprocess.DEVNULL,
    ).strip()
    result = subprocess.run(
        ["icacls.exe", str(path), "/inheritance:r", "/grant:r", f"*{sid}:F", "*S-1-5-18:F", "*S-1-5-32-544:F"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError("Windows could not restrict the synthetic Admin fixture ACL.")


async def main() -> None:
    url = make_url(os.environ["DATABASE_URL"])
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != "127.0.0.1"
        or url.port != 55928
        or url.username != "postgres"
        or url.password is not None
        or url.database != EXPECTED_DATABASE
    ):
        raise RuntimeError("C09 Admin seeding is pinned to its dedicated loopback-only disposable database.")
    review_root = Path(os.environ["INFRASPINE_C09_REVIEW_ROOT"]).resolve(strict=True)
    if review_root != EXPECTED_REVIEW_ROOT.resolve(strict=True):
        raise RuntimeError("C09 Admin seeding is pinned to its protected review folder.")
    fixture_path = review_root / "credentials.json"
    if fixture_path.exists():
        raise RuntimeError("The retained C09 credential fixture already exists; preserving it.")

    password = secrets.token_urlsafe(32)
    fixture = {"admin": {"email": "c09-semantic-admin@synthetic.test", "password": password}}
    with fixture_path.open("x", encoding="utf-8") as handle:
        json.dump(fixture, handle, indent=2)
        handle.write("\n")
    _protect_fixture(fixture_path)

    engine = create_async_engine(str(url), pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            user_count = await db.scalar(select(func.count()).select_from(User))
            if user_count:
                raise RuntimeError("The C09 review database is not empty; preserving its users.")
            db.add(
                User(
                    email=fixture["admin"]["email"],
                    password_hash=hash_password(password),
                    role="admin",
                    is_active=True,
                )
            )
            await db.commit()
    finally:
        await engine.dispose()
    print("C09_SYNTHETIC_ADMIN_SEEDED")


if __name__ == "__main__":
    asyncio.run(main())
