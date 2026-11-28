"""Step AO slice 4: least-privilege database roles + credential rotation.

Five roles, each able to do only what its service needs. The application
never connects as a superuser; the migration role exists only for the
duration of a migration run.

- ``infraspine_app`` — read + write on application tables, no DDL.
- ``infraspine_worker`` — same grants as the application role, a
  separate credential so a worker compromise does not leak the app
  credential. Queue tables are application tables; the separation is
  the credential, not the grant set.
- ``infraspine_reporting`` — SELECT only, for reports and the admin
  read-only views. It cannot write, and it cannot read sequences
  beyond USAGE.
- ``infraspine_backup`` — SELECT on all tables (what pg_dump needs),
  no writes.
- ``infraspine_migrator`` — full DDL on the application schema, used
  only while a migration runs.

Role names and passwords are quoted, never interpolated raw: an
injection string in a role name becomes a harmless quoted identifier,
and a quote inside a password is doubled, not executed.
"""

from __future__ import annotations

import re
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import create_engine

ROLE_APP = "infraspine_app"
ROLE_WORKER = "infraspine_worker"
ROLE_REPORTING = "infraspine_reporting"
ROLE_BACKUP = "infraspine_backup"
ROLE_MIGRATOR = "infraspine_migrator"

ROLES = (ROLE_APP, ROLE_WORKER, ROLE_REPORTING, ROLE_BACKUP, ROLE_MIGRATOR)

#: A role password must be at least this long and contain a letter and a
#: digit. Service credentials are held to a higher bar than the app's
#: user-password minimum (8).
MIN_ROLE_PASSWORD_LENGTH = 16

_ROLE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

#: What each role may do on application tables. "ddl" is the schema
#: CREATE grant (only the migrator holds it); "sequences" is USAGE+SELECT
#: on sequences (the reporting role reads tables only).
_ROLE_GRANTS: dict[str, dict[str, bool]] = {
    ROLE_APP: {"select": True, "insert": True, "update": True, "delete": True, "ddl": False, "sequences": True},
    ROLE_WORKER: {"select": True, "insert": True, "update": True, "delete": True, "ddl": False, "sequences": True},
    ROLE_REPORTING: {"select": True, "insert": False, "update": False, "delete": False, "ddl": False, "sequences": False},
    ROLE_BACKUP: {"select": True, "insert": False, "update": False, "delete": False, "ddl": False, "sequences": True},
    ROLE_MIGRATOR: {"select": True, "insert": True, "update": True, "delete": True, "ddl": True, "sequences": True},
}

_PRIVILEGES = ("select", "insert", "update", "delete")


def validate_role_name(name: str) -> str:
    """A role name must be a safe lowercase identifier and one of the five AO roles."""
    if name not in ROLES:
        raise ValueError(f"unknown AO role {name!r}; expected one of {', '.join(ROLES)}")
    return name


def quote_identifier(name: str) -> str:
    """Quote a PostgreSQL identifier by doubling embedded quotes. Never interpolate raw."""
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """Quote a PostgreSQL string literal by doubling embedded quotes."""
    return "'" + value.replace("'", "''") + "'"


def validate_role_password(password: str) -> list[str]:
    """Problems with a role password; an empty list means acceptable."""
    problems: list[str] = []
    if len(password) < MIN_ROLE_PASSWORD_LENGTH:
        problems.append(f"password must be at least {MIN_ROLE_PASSWORD_LENGTH} characters")
    if not re.search(r"[A-Za-z]", password):
        problems.append("password must contain a letter")
    if not re.search(r"[0-9]", password):
        problems.append("password must contain a digit")
    return problems


def role_statements(role: str, *, schema: str = "public") -> list[str]:
    """The SQL that creates a role and grants it exactly its privileges.

    Pure: returns statements, executes nothing. The CREATE is guarded
    by a pg_roles existence check so re-running is safe. Grants cover
    existing tables in the schema; future tables need ALTER DEFAULT
    PRIVILEGES issued as the table owner during a migration run (plan
    §5) — this function deliberately does not guess the owner.
    """
    validate_role_name(role)
    if not _ROLE_NAME.fullmatch(schema):
        raise ValueError(f"refusing schema {schema!r}")
    grants = _ROLE_GRANTS[role]
    quoted_role = quote_identifier(role)
    quoted_schema = quote_identifier(schema)
    table_privileges = ", ".join(p for p in _PRIVILEGES if grants[p])
    statements = [
        f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {quote_literal(role)}) "
        f"THEN CREATE ROLE {quoted_role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; END IF; END $$",
        f"GRANT USAGE ON SCHEMA {quoted_schema} TO {quoted_role}",
    ]
    if table_privileges:
        statements.append(
            f"GRANT {table_privileges.upper()} ON ALL TABLES IN SCHEMA {quoted_schema} TO {quoted_role}"
        )
    if grants["sequences"]:
        statements.append(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {quoted_schema} TO {quoted_role}"
        )
    if grants["ddl"]:
        statements.append(f"GRANT CREATE ON SCHEMA {quoted_schema} TO {quoted_role}")
    else:
        statements.append(f"REVOKE CREATE ON SCHEMA {quoted_schema} FROM {quoted_role}")
    return statements


async def apply_roles(engine: AsyncEngine, roles: list[str]) -> dict[str, list[str]]:
    """Create the roles (if missing) and apply their grants. Returns the statements run."""
    applied: dict[str, list[str]] = {}
    async with engine.begin() as conn:
        for role in roles:
            statements = role_statements(role)
            for statement in statements:
                await conn.execute(text(statement))
            applied[role] = statements
    return applied


async def drop_roles(engine: AsyncEngine, roles: list[str]) -> None:
    """Revoke every grant, then drop the roles.

    A plain DROP ROLE fails while the role still holds privileges on
    tables (DependentObjectsStillExistError), so the grants are revoked
    first. DROP OWNED BY is deliberately NOT used: it would also drop
    objects the role owns, and decommissioning a credential must never
    delete data.
    """
    for role in roles:
        validate_role_name(role)
    async with engine.begin() as conn:
        existing = {
            row[0]
            for row in (
                await conn.execute(
                    text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:roles)"),
                    {"roles": list(roles)},
                )
            ).all()
        }
        for role in roles:
            if role not in existing:
                continue  # REVOKE FROM a missing role errors; nothing to clean up.
            quoted_role = quote_identifier(role)
            await conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {quoted_role}"))
            await conn.execute(text(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {quoted_role}"))
            await conn.execute(text(f"REVOKE USAGE ON SCHEMA public FROM {quoted_role}"))
            await conn.execute(text(f"REVOKE CREATE ON SCHEMA public FROM {quoted_role}"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {quoted_role}"))


async def verify_role_privileges(engine: AsyncEngine, role: str, table: str) -> dict[str, bool]:
    """What a role may actually do on one table, measured — never assumed."""
    validate_role_name(role)
    if "." in table:
        schema, table_name = table.split(".", 1)
    else:
        schema, table_name = "public", table
    if not _ROLE_NAME.fullmatch(schema) or not _ROLE_NAME.fullmatch(table_name):
        raise ValueError(f"refusing table {table!r}")
    qualified = f"{quote_identifier(schema)}.{quote_identifier(table_name)}"
    async with engine.connect() as conn:
        result = {}
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            result[privilege.lower()] = bool(
                await conn.scalar(
                    text(
                        f"SELECT has_table_privilege({quote_literal(role)}, "
                        f"{quote_literal(qualified)}, {quote_literal(privilege)})"
                    )
                )
            )
    return result


async def rotate_role_password(engine: AsyncEngine, role: str, new_password: str) -> None:
    """Set a new password for a role. The old credential stops working immediately.

    Rotation itself is a privileged operation: the caller must already
    hold a superuser (or CREATEROLE) connection, and in production the
    rotation is approval-bound with separation of duties (plan §5). The
    pool must be recycled after rotation so no pooled connection keeps
    the old credential — the caller recycles by disposing its engine.
    """
    validate_role_name(role)
    problems = validate_role_password(new_password)
    if problems:
        raise ValueError("; ".join(problems))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"ALTER ROLE {quote_identifier(role)} WITH PASSWORD {quote_literal(new_password)}"
            )
        )


async def connect_as_role(admin_url: str, role: str, password: str, *, timeout_seconds: int = 5) -> bool:
    """True when a fresh connection as the role succeeds. Used to prove a rotation."""
    from sqlalchemy.engine import make_url

    url = str(make_url(admin_url).set(username=role, password=password))
    engine = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=timeout_seconds)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


RoleName = Literal[
    "infraspine_app",
    "infraspine_worker",
    "infraspine_reporting",
    "infraspine_backup",
    "infraspine_migrator",
]
