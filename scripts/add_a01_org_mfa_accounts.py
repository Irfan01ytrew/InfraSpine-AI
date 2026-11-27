"""Add new synthetic accounts to the retained A01 test DB; never overwrite QA rows."""

import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.security import hash_password
from app.db.models import ROLE_ADMIN, ROLE_EMPLOYEE, User
from app.db.session import create_engine, create_sessionmaker


EXPECTED_DATABASE = "infraspine_a01orgmfa_20261027_test"
EMAILS = {"admin": "a01-org-admin@synthetic.test", "employee": "a01-org-employee@synthetic.test"}


async def main(output: Path) -> None:
    url = make_url(os.environ["DATABASE_URL"])
    if url.host != "127.0.0.1" or url.port != 55888 or url.database != EXPECTED_DATABASE:
        raise RuntimeError("Refusing to seed a non-matching isolated A01 database endpoint.")
    if output.exists():
        raise RuntimeError("Refusing to overwrite the A01 credential fixture.")
    output.parent.mkdir(parents=True, exist_ok=True)
    credentials = {
        "admin": {"email": EMAILS["admin"], "password": secrets.token_urlsafe(32)},
        "employees": [{"email": EMAILS["employee"], "password": secrets.token_urlsafe(32)}],
    }
    engine = create_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    sessions = create_sessionmaker(engine)
    try:
        async with sessions() as db:
            existing = (await db.scalars(select(User).where(User.email.in_(list(EMAILS.values()))))).all()
            if existing:
                raise RuntimeError("A01 synthetic account email already exists; no account was changed.")
            db.add_all(
                [
                    User(email=credentials["admin"]["email"], password_hash=hash_password(credentials["admin"]["password"]), role=ROLE_ADMIN, is_active=True),
                    User(email=credentials["employees"][0]["email"], password_hash=hash_password(credentials["employees"][0]["password"]), role=ROLE_EMPLOYEE, is_active=True),
                ]
            )
            await db.commit()
        output.write_text(json.dumps(credentials, indent=2) + "\n", encoding="utf-8")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: add_a01_org_mfa_accounts.py <protected-fixture-path>")
    asyncio.run(main(Path(sys.argv[1])))
    print("New A01 synthetic Admin/Employee accounts added; credentials stored in the protected review fixture.")
