"""Rotate only the synthetic Admin/Manager/Employee accounts in the local F07 QA copy."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from app.core.security import hash_password, verify_password
from app.db.models import ROLE_ADMIN, ROLE_MANAGER, ROLE_USER, User, UserSession
from app.db.session import create_engine, create_sessionmaker


EXPECTED_DATABASE = "infraspine_f06roles_a2ae10a9_test"
EXPECTED_ROLE = "postgres"
EXPECTED_HOST = "127.0.0.1"
EXPECTED_PORT = 55888


def _accounts(path: Path) -> dict[str, tuple[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    accounts: dict[str, tuple[str, str]] = {}
    admin = data.get("admin")
    if isinstance(admin, dict):
        accounts["admin"] = (str(admin.get("email", "")).casefold(), str(admin.get("password", "")))
    manager = data.get("manager")
    if isinstance(manager, dict):
        accounts["manager"] = (str(manager.get("email", "")).casefold(), str(manager.get("password", "")))
    else:
        managers = data.get("managers", [])
        if len(managers) == 1:
            account = managers[0]
            accounts["manager"] = (str(account.get("email", "")).casefold(), str(account.get("password", "")))
    employee = data.get("employee")
    if isinstance(employee, dict):
        accounts["employee"] = (str(employee.get("email", "")).casefold(), str(employee.get("password", "")))
    else:
        employees = data.get("employees", [])
        if len(employees) == 1:
            account = employees[0]
            accounts["employee"] = (str(account.get("email", "")).casefold(), str(account.get("password", "")))
    if set(accounts) != {"admin", "manager", "employee"} or any(
        not email or len(password) < 16 for email, password in accounts.values()
    ):
        raise RuntimeError("Expected one complete synthetic account for each of the three roles.")
    if len({email for email, _ in accounts.values()}) != 3:
        raise RuntimeError("Synthetic account emails must be unique.")
    return accounts


async def _rotate(database_url: str, current_path: Path, retired_path: Path) -> None:
    url = make_url(database_url)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != EXPECTED_HOST
        or url.port != EXPECTED_PORT
        or url.database != EXPECTED_DATABASE
        or url.username != EXPECTED_ROLE
        or url.password is not None
    ):
        raise RuntimeError("Password rotation is pinned to the isolated, password-free F07 application database.")

    current = _accounts(current_path)
    retired = _accounts(retired_path)
    if {role: email for role, (email, _) in current.items()} != {
        role: email for role, (email, _) in retired.items()
    }:
        raise RuntimeError("The protected replacement fixture does not match the retained synthetic account identities.")

    # Legacy employee sign-ins are stored with the `user` role; the product UI labels them Employee.
    expected_roles = {"admin": ROLE_ADMIN, "manager": ROLE_MANAGER, "employee": ROLE_USER}
    engine = create_engine(database_url)
    sessionmaker = create_sessionmaker(engine)
    try:
        async with sessionmaker() as session:
            users: dict[str, User] = {}
            for role, (email, new_password) in current.items():
                user = (await session.execute(select(User).where(User.email == email).with_for_update())).scalar_one_or_none()
                if user is None or user.role != expected_roles[role] or not user.is_active:
                    raise RuntimeError("The isolated database is missing an expected active synthetic role account.")
                old_password = retired[role][1]
                if not verify_password(user.password_hash, old_password):
                    raise RuntimeError("The retained synthetic credential did not match the isolated database copy.")
                if old_password == new_password:
                    raise RuntimeError("A replacement synthetic password must differ from the retired value.")
                user.password_hash = hash_password(new_password)
                user.failed_login_attempts = 0
                user.locked_until = None
                users[role] = user
                await session.execute(delete(UserSession).where(UserSession.user_id == user.id))
            await session.commit()

            for role, user in users.items():
                new_password = current[role][1]
                old_password = retired[role][1]
                if not verify_password(user.password_hash, new_password) or verify_password(user.password_hash, old_password):
                    raise RuntimeError("The synthetic credential rotation did not verify as expected.")
    finally:
        await engine.dispose()


def main() -> None:
    current_path = Path(os.environ["INFRASPINE_CURRENT_FIXTURE"]).resolve(strict=True)
    retired_path = Path(os.environ["INFRASPINE_RETIRED_FIXTURE"]).resolve(strict=True)
    asyncio.run(_rotate(os.environ["DATABASE_URL"], current_path, retired_path))
    print("SYNTHETIC_ROLE_PASSWORD_ROTATION=PASS")
    print("ROTATED_ROLE_COUNT=3")
    print("RETIRED_PASSWORDS_REJECTED=3/3")
    print("EXISTING_CLUSTER_AND_LIVE_DATABASE=UNCHANGED")


if __name__ == "__main__":
    main()
