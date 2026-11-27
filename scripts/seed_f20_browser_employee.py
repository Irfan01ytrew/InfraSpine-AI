"""Create/rotate one synthetic F20 browser requester without emitting its password."""

import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.security import hash_password
from app.db.models import ROLE_EMPLOYEE, User
from app.db.session import create_engine, create_sessionmaker


FIXTURE = ROOT / ".verification-local-review-20261027-native" / "f18-current-source" / "review-credentials.json"
EMAIL = "f20-browser-requester@synthetic.test"


async def main() -> None:
    if not FIXTURE.is_file():
        raise SystemExit("Protected F18 local synthetic credential fixture is unavailable.")
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("Synthetic fixture structure is invalid.")
    password = secrets.token_urlsafe(32)
    engine = create_engine(os.environ["DATABASE_URL"])
    sessions = create_sessionmaker(engine)
    try:
        async with sessions() as db:
            user = await db.scalar(select(User).where(User.email == EMAIL))
            if user is None:
                user = User(email=EMAIL, password_hash=hash_password(password), role=ROLE_EMPLOYEE)
                db.add(user)
            elif user.role != ROLE_EMPLOYEE:
                raise SystemExit("The reserved synthetic requester email has an unexpected role.")
            else:
                user.password_hash = hash_password(password)
                user.is_active = True
                user.failed_login_attempts = 0
                user.locked_until = None
            await db.commit()
    finally:
        await engine.dispose()

    employees = [item for item in data.get("employees", []) if item.get("email") != EMAIL]
    employees.append({"email": EMAIL, "password": password})
    data["employees"] = employees
    # Writing the existing file in place preserves its already-protected Windows DACL.
    FIXTURE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print("F20 synthetic requester credential rotated and stored in the existing protected local viewer; password omitted.")


if __name__ == "__main__":
    asyncio.run(main())
