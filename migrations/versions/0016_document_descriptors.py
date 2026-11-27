"""Document descriptors for document discovery: one normalised identity per document, lexically indexed.

* `documents.descriptor`: the filename split into words, dates written out, title, headings, sheet, slide and table
  titles (`app.rag.discovery.build_descriptor`). Identity only: never sent to the model or cited.
* `documents.descriptor_version`: the builder version that wrote it; older or missing descriptors are rebuilt from
  stored chunks at worker startup (`app.rag.discovery.refresh_descriptors`), without re-parsing.
* `documents.descriptor_search`: a generated `tsvector` (`simple` configuration, as for chunks) with a GIN index.

See docs/document-discovery.md.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE documents ADD COLUMN descriptor text")
    op.execute("ALTER TABLE documents ADD COLUMN descriptor_version smallint")
    op.execute(
        "ALTER TABLE documents ADD COLUMN descriptor_search tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', coalesce(descriptor, ''))) STORED"
    )
    op.execute("CREATE INDEX ix_documents_descriptor_search ON documents USING gin (descriptor_search)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_documents_descriptor_search")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS descriptor_search")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS descriptor_version")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS descriptor")
