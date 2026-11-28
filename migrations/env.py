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
from app.db import agent_studio_models  # noqa: F401
from app.company_graph import models as company_graph_models  # noqa: F401
from app.governance import models as governance_models  # noqa: F401
from app.recovery import models as recovery_models  # noqa: F401
from app.continuity import models as continuity_models  # noqa: F401
from app.integrations import models as integrations_models  # noqa: F401
from app.db import workflow_engine_models as workflow_models  # noqa: F401
from app.db import voice_models  # noqa: F401
from app.db import finance_models  # noqa: F401
from app.qa import models as quality_models  # noqa: F401
from app.platform import models as platform_models  # noqa: F401
from app.db.session import test_engine_options

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Objects created by raw SQL in migrations that SQLAlchemy models cannot express faithfully (generated
# tsvector columns, GIN/trigram/expression and partial indexes, and the lexical vocabulary table). They are
# owned by their migrations; autogenerate must neither drop them nor report them as drift. Keep this list
# exact: anything else that differs between the models and the database is real drift.
MIGRATION_MANAGED = {
    "table": {"rag_terms"},
    "column": {("document_chunks", "search_vector"), ("documents", "descriptor_search")},
    "index": {
        "ix_rag_terms_term_trgm",
        "ix_document_chunks_search_vector",
        "ix_documents_descriptor_search",
        "ix_documents_quarantined",
        "ix_knowledge_revisions_published_search",
        "ix_knowledge_revisions_review_due",
        "ix_request_approval_steps_reminders",
        "ix_users_expertise_tags_gin",
    },
}


def include_object(obj, name, type_, reflected, compare_to):
    if not reflected or compare_to is not None:
        return True
    if type_ == "column":
        return (obj.table.name, name) not in MIGRATION_MANAGED["column"]
    return name not in MIGRATION_MANAGED.get(type_, set())


def _database_url() -> str:
    # Tests may inject a URL; otherwise use the application's DATABASE_URL.
    return config.attributes.get("database_url") or DatabaseSettings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        version_table_schema=os.environ.get("INFASPINE_TEST_SCHEMA"),
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
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
