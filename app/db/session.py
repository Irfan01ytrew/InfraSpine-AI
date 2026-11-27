import os
import re

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

_TEST_SCHEMA = re.compile(r"^infraspine_test_[0-9a-f]{12}$")


def test_engine_options() -> dict:
    """Route explicitly isolated integration tests to their own PostgreSQL schema."""
    schema = os.environ.get("INFASPINE_TEST_SCHEMA")
    if not schema:
        return {}
    if os.environ.get("APP_ENV") != "testing" or _TEST_SCHEMA.fullmatch(schema) is None:
        raise RuntimeError("INFASPINE_TEST_SCHEMA is only valid for a uniquely named testing schema.")
    return {"connect_args": {"server_settings": {"search_path": f"{schema},public"}}}


def create_engine(
    database_url: str, *, pool_size: int = 5, max_overflow: int = 10,
    pool_timeout: int = 30, pool_recycle: int = 1800,
) -> AsyncEngine:
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_recycle=pool_recycle,
        pool_use_lifo=True,
        # Keep bound parameter values out of exception messages and logs.
        hide_parameters=True,
        **test_engine_options(),
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
