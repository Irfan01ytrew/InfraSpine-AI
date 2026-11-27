"""Add isolated synthetic F03 reviewer/employee accounts to the F02 review database."""

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
import sys

sys.path.insert(0, str(ROOT))

from app.core.security import hash_password
from app.db.models import (
    ROLE_ADMIN,
    ROLE_EMPLOYEE,
    ROLE_MANAGER,
    AccessGroup,
    AccessGroupMember,
    DepartmentManager,
    User,
)


def _write_f03_fixture(admin_fixture: dict, path: Path) -> dict:
    admin = admin_fixture.get("admin")
    if not isinstance(admin, dict) or not admin.get("email") or not admin.get("password"):
        raise RuntimeError("The protected F02 Admin fixture is incomplete.")
    if path.exists():
        fixture = json.loads(path.read_text(encoding="utf-8-sig"))
        if (
            fixture.get("admin") != admin
            or len(fixture.get("managers", [])) != 1
            or len(fixture.get("employees", [])) != 2
        ):
            raise RuntimeError("The retained F03 credential fixture does not match this synthetic review.")
        return fixture

    fixture = {
        "admin": admin,
        "managers": [{"email": "f03-review-manager@synthetic.test", "password": secrets.token_urlsafe(36)}],
        "employees": [
            {"email": "f03-interview-author@synthetic.test", "password": secrets.token_urlsafe(36)},
            {"email": "f03-outside-employee@synthetic.test", "password": secrets.token_urlsafe(36)},
        ],
    }
    path.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    return fixture


def _protect_fixture(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if not powershell:
        raise RuntimeError("PowerShell is required to protect the synthetic F03 fixture ACL.")
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
        raise RuntimeError("Windows could not restrict the F03 synthetic credential fixture ACL.")


async def main() -> None:
    url = make_url(os.environ["DATABASE_URL"])
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != "127.0.0.1"
        or url.port != 55928
        or url.username != "postgres"
        or url.password is not None
        or url.database != "infraspine_f02meetai_20261028_test"
    ):
        raise RuntimeError("F03 account seeding is pinned to the isolated F02 review database.")

    admin_path = Path(os.environ["INFRASPINE_F02_CREDENTIAL_FIXTURE"]).resolve(strict=True)
    f03_path = Path(os.environ["INFRASPINE_F03_CREDENTIAL_FIXTURE"]).resolve()
    admin_fixture = json.loads(admin_path.read_text(encoding="utf-8-sig"))
    fixture = _write_f03_fixture(admin_fixture, f03_path)
    _protect_fixture(f03_path)
    engine = create_async_engine(str(url), pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            admin = await db.scalar(select(User).where(User.email == fixture["admin"]["email"].casefold()))
            user_count = await db.scalar(select(func.count()).select_from(User))
            expected_emails = {
                fixture["managers"][0]["email"].casefold(),
                *(row["email"].casefold() for row in fixture["employees"]),
            }
            if admin is None or admin.role != ROLE_ADMIN or not admin.is_active:
                raise RuntimeError("The existing isolated synthetic Admin account is unavailable.")

            existing = {
                row.email: row
                for row in (
                    await db.scalars(select(User).where(User.email.in_(expected_emails)))
                ).all()
            }
            if not existing and user_count != 1:
                raise RuntimeError("F03 seed expected exactly its existing Admin before adding role accounts.")
            if existing and set(existing) != expected_emails:
                raise RuntimeError("F03 role-account seed is partial; preserving it for inspection.")

            role_rows: dict[str, User] = {}
            to_add = [(fixture["managers"][0], ROLE_MANAGER), *[(row, ROLE_EMPLOYEE) for row in fixture["employees"]]]
            for record, role in to_add:
                email = record["email"].casefold()
                user = existing.get(email)
                if user is None:
                    user = User(
                        email=email,
                        password_hash=hash_password(record["password"]),
                        role=role,
                        is_active=True,
                    )
                    db.add(user)
                elif user.role != role or not user.is_active:
                    raise RuntimeError("An existing F03 synthetic role account has an unexpected role or state.")
                role_rows[email] = user
            await db.flush()

            group_name = "F03 Knowledge Interview Operations"
            group = await db.scalar(select(AccessGroup).where(AccessGroup.name == group_name))
            if group is None:
                group = AccessGroup(name=group_name, kind="department", created_by=admin.id, owner_id=admin.id)
                db.add(group)
                await db.flush()
                manager = role_rows[fixture["managers"][0]["email"].casefold()]
                author = role_rows[fixture["employees"][0]["email"].casefold()]
                db.add(DepartmentManager(group_id=group.id, manager_user_id=manager.id, assigned_by=admin.id))
                db.add(AccessGroupMember(group_id=group.id, user_id=manager.id, added_by=admin.id))
                db.add(AccessGroupMember(group_id=group.id, user_id=author.id, added_by=admin.id))
            else:
                manager = role_rows[fixture["managers"][0]["email"].casefold()]
                author = role_rows[fixture["employees"][0]["email"].casefold()]
                manager_id = await db.scalar(
                    select(DepartmentManager.manager_user_id).where(
                        DepartmentManager.group_id == group.id,
                        DepartmentManager.manager_user_id == manager.id,
                    )
                )
                member_id = await db.scalar(
                    select(AccessGroupMember.user_id).where(
                        AccessGroupMember.group_id == group.id,
                        AccessGroupMember.user_id == author.id,
                    )
                )
                manager_member_id = await db.scalar(
                    select(AccessGroupMember.user_id).where(
                        AccessGroupMember.group_id == group.id,
                        AccessGroupMember.user_id == manager.id,
                    )
                )
                if manager_id != manager.id or member_id != author.id:
                    raise RuntimeError("Existing F03 department scope differs from its protected test fixture.")
                if manager_member_id is None:
                    db.add(AccessGroupMember(group_id=group.id, user_id=manager.id, added_by=admin.id))
            await db.commit()
    finally:
        await engine.dispose()

    print("F03_SYNTHETIC_ROLE_FIXTURE_AND_DEPARTMENT=READY")


if __name__ == "__main__":
    asyncio.run(main())
