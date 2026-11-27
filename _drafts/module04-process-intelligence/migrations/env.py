"""Alembic environment. Uses the async engine and reads DATABASE_URL from settings."""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import DatabaseSettings
from app.db import models  # noqa: F401  (registers tables on the metadata)
from app.db.base import Base
from app.process_intelligence import models as process_models  # noqa: F401
from app.db.session import test_engine_options

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    # Tests may inject a URL; otherwise use the application's DATABASE_URL.
    return config.attributes.get("database_url") or DatabaseSettings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=os.environ.get("INFASPINE_TEST_SCHEMA"),
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        version_table_schema=os.environ.get("INFASPINE_TEST_SCHEMA"),
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), hide_parameters=True, **test_engine_options())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_sync)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
