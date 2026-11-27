"""Seed a protected synthetic C15 workflow/My Work review fixture."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import uuid
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.security import hash_password
from app.db.models import AccessGroup, AccessGroupMember, DepartmentManager, User


def _protect_fixture(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    identity = subprocess.check_output(
        ["powershell.exe", "-NoProfile", "-Command", "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
        text=True,
        stdin=subprocess.DEVNULL,
    ).strip()
    result = subprocess.run(
        ["icacls.exe", str(path), "/inheritance:r", "/grant:r", f"*{identity}:F", "*S-1-5-18:F", "*S-1-5-32-544:F"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode:
        raise RuntimeError("Could not restrict the synthetic credential fixture ACL.")


async def main() -> None:
    expected_root = Path(os.environ["C15_REVIEW_ROOT"]).resolve(strict=True)
    fixture_path = expected_root / "credentials.json"
    if fixture_path.exists():
        raise RuntimeError("The C15 fixture already exists; preserving it.")
    database_url = make_url(os.environ["DATABASE_URL"])
    database_name = database_url.database or ""
    if (
        database_url.drivername != "postgresql+asyncpg"
        or database_url.host not in {"127.0.0.1", "localhost"}
        or database_url.port != 55929
        or database_url.username != "postgres"
        or database_url.password is not None
        or not database_name.startswith("infraspine_c15mywork_")
        or not database_name.endswith("_review")
    ):
        raise RuntimeError("C15 fixture seeding is pinned to a new disposable loopback review database.")

    identities = {name: uuid.uuid4() for name in ("admin", "manager", "employee", "outsider", "department", "other_department")}
    credentials = {
        name: {"email": f"c15-{name}@synthetic.test", "password": secrets.token_urlsafe(32)}
        for name in ("admin", "manager", "employee", "outsider")
    }
    engine = create_async_engine(str(database_url), pool_size=1, max_overflow=0)
    try:
        async with engine.begin() as connection:
            for name, role in (("admin", "admin"), ("manager", "manager"), ("employee", "employee"), ("outsider", "employee")):
                await connection.execute(
                    User.__table__.insert().values(
                        id=identities[name],
                        email=credentials[name]["email"],
                        password_hash=hash_password(credentials[name]["password"]),
                        role=role,
                        is_active=True,
                    )
                )
            await connection.execute(
                AccessGroup.__table__.insert().values(
                    id=identities["department"], name="Synthetic Operations", kind="department",
                    source="local", created_by=identities["admin"], lifecycle_status="active",
                )
            )
            await connection.execute(
                AccessGroup.__table__.insert().values(
                    id=identities["other_department"], name="Synthetic Finance", kind="department",
                    source="local", created_by=identities["admin"], lifecycle_status="active",
                )
            )
            await connection.execute(
                AccessGroupMember.__table__.insert().values(
                    group_id=identities["department"], user_id=identities["employee"], added_by=identities["admin"],
                )
            )
            await connection.execute(
                AccessGroupMember.__table__.insert().values(
                    group_id=identities["other_department"], user_id=identities["outsider"], added_by=identities["admin"],
                )
            )
            await connection.execute(
                DepartmentManager.__table__.insert().values(
                    group_id=identities["department"], manager_user_id=identities["manager"], assigned_by=identities["admin"],
                )
            )
    finally:
        await engine.dispose()

    fixture = {**credentials, "ids": {key: str(value) for key, value in identities.items()}}
    with fixture_path.open("x", encoding="utf-8") as handle:
        json.dump(fixture, handle, indent=2)
        handle.write("\n")
    _protect_fixture(fixture_path)
    Path(os.environ["RAG_DATA_DIR"], "company-profile.json").write_text(
        json.dumps(
            {"company_name": "Synthetic C15 Review", "industry": "General", "brand_name": "InfraSpine QA",
             "primary_color": "#145da0", "setup_complete": True, "template_id": None, "template_version": None},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    print("C15_SYNTHETIC_FIXTURE=READY")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
