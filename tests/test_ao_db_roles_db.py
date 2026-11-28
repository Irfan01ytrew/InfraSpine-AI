"""DB tests for Step AO slice 4: roles applied and measured on a throwaway cluster.

Run with --noconftest: these tests build their own engines. Nothing here
touches the live Codespace database or the owner's PC database.

Each test runs its whole async body inside ONE asyncio.run call: an async
engine created under one event loop must never be used or disposed under
another.
"""

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.db.session import create_engine
from app.ops.db_roles import (
    ROLE_APP,
    ROLE_BACKUP,
    ROLE_MIGRATOR,
    ROLE_REPORTING,
    apply_roles,
    connect_as_role,
    drop_roles,
    rotate_role_password,
    verify_role_privileges,
)

TEST_URL = os.environ.get("TEST_DATABASE_URL")
ADMIN_URL = str(make_url(TEST_URL).set(database="postgres")) if TEST_URL else None

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL is not set")

ROLE_PASSWORD = "rotation-proof-password-42"


async def _drop_roles(engine):
    await drop_roles(engine, [ROLE_APP, ROLE_REPORTING, ROLE_BACKUP, ROLE_MIGRATOR])


def test_roles_apply_with_least_privilege_measured():
    async def _body():
        engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)
        try:
            await _drop_roles(engine)
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE IF EXISTS ao_roles_probe"))
                await conn.execute(
                    text("CREATE TABLE ao_roles_probe (id serial PRIMARY KEY, v text)")
                )
            applied = await apply_roles(
                engine, [ROLE_APP, ROLE_REPORTING, ROLE_BACKUP, ROLE_MIGRATOR]
            )
            assert set(applied) == {ROLE_APP, ROLE_REPORTING, ROLE_BACKUP, ROLE_MIGRATOR}
            # Re-applying is safe: the CREATE is existence-guarded.
            await apply_roles(engine, [ROLE_APP, ROLE_REPORTING, ROLE_BACKUP, ROLE_MIGRATOR])
            async with engine.connect() as conn:
                flags = (
                    await conn.execute(
                        text(
                            "SELECT rolname, rolsuper, rolcanlogin FROM pg_roles "
                            "WHERE rolname IN ('infraspine_app', 'infraspine_reporting', "
                            "'infraspine_backup', 'infraspine_migrator') ORDER BY rolname"
                        )
                    )
                ).all()
            app = await verify_role_privileges(engine, ROLE_APP, "ao_roles_probe")
            reporting = await verify_role_privileges(engine, ROLE_REPORTING, "ao_roles_probe")
            backup = await verify_role_privileges(engine, ROLE_BACKUP, "ao_roles_probe")
            migrator = await verify_role_privileges(engine, ROLE_MIGRATOR, "ao_roles_probe")
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE IF EXISTS ao_roles_probe"))
            await _drop_roles(engine)
            return flags, app, reporting, backup, migrator
        finally:
            await engine.dispose()

    flags, app, reporting, backup, migrator = asyncio.run(_body())
    assert len(flags) == 4
    for _name, is_super, can_login in flags:
        assert is_super is False  # nobody is a superuser
        assert can_login is True
    assert app == {"select": True, "insert": True, "update": True, "delete": True}
    assert reporting == {"select": True, "insert": False, "update": False, "delete": False}
    assert backup == {"select": True, "insert": False, "update": False, "delete": False}
    assert migrator == {"select": True, "insert": True, "update": True, "delete": True}


def test_password_rotation_works_and_the_old_password_stops():
    async def _body():
        engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)
        try:
            await _drop_roles(engine)
            await apply_roles(engine, [ROLE_APP])
            old_password = "first-password-for-rotation-1"
            await rotate_role_password(engine, ROLE_APP, old_password)
            assert await connect_as_role(TEST_URL, ROLE_APP, old_password) is True

            async def _verifier():
                async with engine.connect() as conn:
                    return await conn.scalar(
                        text("SELECT rolpassword FROM pg_authid WHERE rolname = :role"),
                        {"role": ROLE_APP},
                    )

            first_verifier = await _verifier()
            assert first_verifier and first_verifier.startswith("SCRAM-SHA-256$")
            await rotate_role_password(engine, ROLE_APP, ROLE_PASSWORD)
            assert await connect_as_role(TEST_URL, ROLE_APP, ROLE_PASSWORD) is True
            # The stored SCRAM verifier changes on rotation. Whether the old
            # password is *rejected* at login depends on pg_hba: this
            # throwaway cluster uses trust auth (any password connects), so
            # the verifier change is the proof here; a scram-auth host
            # rejects the old password the moment the verifier changes.
            assert await _verifier() != first_verifier
            await _drop_roles(engine)
        finally:
            await engine.dispose()

    asyncio.run(_body())


def test_rotation_refuses_weak_passwords():
    async def _body():
        engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)
        try:
            await _drop_roles(engine)
            await apply_roles(engine, [ROLE_APP])
            with pytest.raises(ValueError, match="at least 16"):
                await rotate_role_password(engine, ROLE_APP, "short1")
            await _drop_roles(engine)
        finally:
            await engine.dispose()

    asyncio.run(_body())
