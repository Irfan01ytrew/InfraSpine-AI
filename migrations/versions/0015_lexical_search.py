"""Lexical search for hybrid retrieval: a full-text vector per chunk and the corpus vocabulary.

* `document_chunks.search_vector`: a generated `tsvector` (`simple` configuration, so every language's tokens are
  kept as written) with a GIN index. Adding it rewrites the table once.
* `rag_terms`: each term with the number of chunks that contain it (document frequency, for IDF weighting and
  typo tolerance), kept current by statement-level triggers on `document_chunks`, with a trigram index.
* `pg_trgm`: a trusted extension, so the database owner can create it.

See docs/hybrid-retrieval.md.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COUNT = """
    SELECT term, count(*) AS n
    FROM {rows}, unnest(tsvector_to_array({rows}.search_vector)) AS term
    GROUP BY term
"""
# Upserts in term order, so concurrent writers lock vocabulary rows in the same order (no deadlocks).
_APPLY = """
    INSERT INTO rag_terms (term, chunk_count)
    SELECT term, {sign} n FROM ({counts}) AS counted ORDER BY term
    ON CONFLICT (term) DO UPDATE SET chunk_count = rag_terms.chunk_count + EXCLUDED.chunk_count;
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "ALTER TABLE document_chunks ADD COLUMN search_vector tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED"
    )
    op.execute("CREATE INDEX ix_document_chunks_search_vector ON document_chunks USING gin (search_vector)")
    op.execute(
        "CREATE TABLE rag_terms (term text NOT NULL, chunk_count integer NOT NULL, "
        "CONSTRAINT pk_rag_terms PRIMARY KEY (term))"
    )
    op.execute("CREATE INDEX ix_rag_terms_term_trgm ON rag_terms USING gin (term gin_trgm_ops)")
    added = _APPLY.format(sign="", counts=_COUNT.format(rows="new_rows"))
    removed = _APPLY.format(sign="-", counts=_COUNT.format(rows="old_rows"))
    op.execute(f"""
        CREATE FUNCTION rag_terms_on_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN {added} RETURN NULL; END $$
    """)
    op.execute(f"""
        CREATE FUNCTION rag_terms_on_delete() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN {removed} DELETE FROM rag_terms WHERE chunk_count <= 0; RETURN NULL; END $$
    """)
    op.execute(f"""
        CREATE FUNCTION rag_terms_on_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN {removed} {added} DELETE FROM rag_terms WHERE chunk_count <= 0; RETURN NULL; END $$
    """)
    op.execute(
        "CREATE TRIGGER document_chunks_terms_insert AFTER INSERT ON document_chunks "
        "REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION rag_terms_on_insert()"
    )
    op.execute(
        "CREATE TRIGGER document_chunks_terms_delete AFTER DELETE ON document_chunks "
        "REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION rag_terms_on_delete()"
    )
    op.execute(
        "CREATE TRIGGER document_chunks_terms_update AFTER UPDATE ON document_chunks "
        "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION rag_terms_on_update()"
    )
    # TRUNCATE fires no row or DELETE triggers: an emptied chunk table empties the vocabulary.
    op.execute("""
        CREATE FUNCTION rag_terms_on_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN TRUNCATE rag_terms; RETURN NULL; END $$
    """)
    op.execute(
        "CREATE TRIGGER document_chunks_terms_truncate AFTER TRUNCATE ON document_chunks "
        "FOR EACH STATEMENT EXECUTE FUNCTION rag_terms_on_truncate()"
    )
    op.execute(f"INSERT INTO rag_terms (term, chunk_count) {_COUNT.format(rows='document_chunks')}")


def downgrade() -> None:
    for trigger in ("insert", "delete", "update", "truncate"):
        op.execute(f"DROP TRIGGER IF EXISTS document_chunks_terms_{trigger} ON document_chunks")
        op.execute(f"DROP FUNCTION IF EXISTS rag_terms_on_{trigger}()")
    op.execute("DROP TABLE IF EXISTS rag_terms")
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_search_vector")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS search_vector")
