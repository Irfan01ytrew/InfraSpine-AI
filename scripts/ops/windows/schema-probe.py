"""Print live schema facts only: Alembic revision and whether pi_* tables exist.

Never prints connection strings, row data, or secrets. Run from the install root
or with INFRA_ROOT set:

    .venv/Scripts/python.exe scripts/ops/windows/schema-probe.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def _root() -> Path:
    env = os.environ.get("INFRA_ROOT")
    if env:
        return Path(env)
    # scripts/ops/windows/schema-probe.py -> repo root
    return Path(__file__).resolve().parents[3]


async def main() -> int:
    root = _root()
    os.chdir(root)
    sys.path.insert(0, str(root))
    from app.core.config import get_settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    settings = get_settings()
    engine = create_async_engine(settings.database_url.get_secret_value())
    names = [
        "pi_tenants",
        "pi_memberships",
        "pi_processes",
        "pi_sources",
        "pi_source_grants",
        "pi_events",
        "pi_traces",
        "pi_baselines",
        "pi_models",
        "pi_jobs",
    ]
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT version_num FROM alembic_version ORDER BY 1"))).fetchall()
            print("alembic_versions=" + ",".join(r[0] for r in rows))
            present = [
                r[0]
                for r in (
                    await conn.execute(
                        text(
                            "SELECT tablename FROM pg_catalog.pg_tables "
                            "WHERE schemaname = 'public' AND tablename = ANY(:names)"
                        ),
                        {"names": names},
                    )
                ).fetchall()
            ]
            missing = [n for n in names if n not in present]
            print("pi_present=" + (",".join(present) if present else "<none>"))
            print("pi_missing=" + (",".join(missing) if missing else "<none>"))
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public'")
                )
            ).scalar()
            print(f"public_table_count={count}")
    except ModuleNotFoundError as exc:
        print(f"db_probe_error=ModuleNotFoundError:{exc.name or type(exc).__name__}")
        return 2
    except Exception:
        print("db_probe_error=" + "DatabaseProbeFailed")
        return 2
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
