"""Versioned organizational knowledge, passage provenance and the index outbox (ADR-004 WP4).

Revisions are immutable. New revisions can only be drafts: the `published` state exists and is
rejected, so nothing from this migration is allowed into the knowledge vector index. Evidence
cites one indexed document revision; a later file change, deletion or passage removal orphans it
and enqueues an idempotent unindex. PostgreSQL stays authoritative; FAISS is derived.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORIES = "('policy', 'financial', 'legal', 'client', 'operational', 'general')"


def upgrade() -> None:
    op.create_table(
        "knowledge_entries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("author_user_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("current_revision_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["author_user_id"], ["users.id"], name=op.f("fk_knowledge_entries_author_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_entries")),
        sa.UniqueConstraint("id", "author_user_id", name="uq_knowledge_entries_author"),
    )

    op.create_table(
        "knowledge_revisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("entry_id", sa.UUID(), nullable=False),
        sa.Column("author_user_id", sa.UUID(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("scope_group_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision_number > 0", name=op.f("ck_knowledge_revisions_revision_number_positive")),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'superseded', 'withdrawn')",
            name=op.f("ck_knowledge_revisions_status_valid"),
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'restricted', 'shared')", name=op.f("ck_knowledge_revisions_visibility_valid")
        ),
        sa.CheckConstraint(f"category IN {CATEGORIES}", name=op.f("ck_knowledge_revisions_category_valid")),
        sa.CheckConstraint(
            "length(btrim(statement)) BETWEEN 1 AND 4000", name=op.f("ck_knowledge_revisions_statement_length")
        ),
        sa.CheckConstraint(
            "(visibility = 'restricted') = (scope_group_id IS NOT NULL)",
            name=op.f("ck_knowledge_revisions_scope_matches_visibility"),
        ),
        sa.ForeignKeyConstraint(
            ["entry_id", "author_user_id"],
            ["knowledge_entries.id", "knowledge_entries.author_user_id"],
            name="fk_knowledge_revisions_entry_author",
        ),
        sa.ForeignKeyConstraint(
            ["scope_group_id"], ["access_groups.id"], name=op.f("fk_knowledge_revisions_scope_group_id_access_groups")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_revisions")),
        sa.UniqueConstraint("entry_id", "revision_number", name="uq_knowledge_revisions_number"),
    )
    op.create_index(
        "uq_knowledge_revisions_one_draft",
        "knowledge_revisions",
        ["entry_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
    )
    op.create_foreign_key(
        "fk_knowledge_entries_current_revision",
        "knowledge_entries",
        "knowledge_revisions",
        ["current_revision_id"],
        ["id"],
    )

    op.create_table(
        "knowledge_evidence",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("revision_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=True),
        sa.Column("document_key", sa.UUID(), nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("document_sha256", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("chunk_id", sa.BigInteger(), nullable=True),
        sa.Column("chunk_key", sa.BigInteger(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("suffix", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("orphan_reason", sa.String(length=40), nullable=True),
        sa.Column("orphaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset", name=op.f("ck_knowledge_evidence_offsets_ordered")
        ),
        sa.CheckConstraint(
            "char_length(quote) = end_offset - start_offset", name=op.f("ck_knowledge_evidence_quote_matches_offsets")
        ),
        sa.CheckConstraint(
            "char_length(quote) BETWEEN 1 AND 2000", name=op.f("ck_knowledge_evidence_quote_length")
        ),
        sa.CheckConstraint(
            "char_length(prefix) <= 240 AND char_length(suffix) <= 240",
            name=op.f("ck_knowledge_evidence_context_length"),
        ),
        sa.CheckConstraint(
            "orphan_reason IS NULL OR orphan_reason IN "
            "('document_revision_changed', 'source_deleted', 'passage_removed')",
            name=op.f("ck_knowledge_evidence_orphan_reason_valid"),
        ),
        sa.CheckConstraint(
            "(orphan_reason IS NULL) = (orphaned_at IS NULL)", name=op.f("ck_knowledge_evidence_orphan_has_time")
        ),
        sa.CheckConstraint(
            "document_id IS NULL OR document_id = document_key", name=op.f("ck_knowledge_evidence_document_key_matches")
        ),
        sa.CheckConstraint(
            "chunk_id IS NULL OR chunk_id = chunk_key", name=op.f("ck_knowledge_evidence_chunk_key_matches")
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["knowledge_revisions.id"], name=op.f("fk_knowledge_evidence_revision_id_knowledge_revisions")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name=op.f("fk_knowledge_evidence_document_id_documents"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["document_chunks.id"], name=op.f("fk_knowledge_evidence_chunk_id_document_chunks"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_evidence")),
    )
    op.create_index("ix_knowledge_evidence_revision", "knowledge_evidence", ["revision_id"])
    op.create_index("ix_knowledge_evidence_document", "knowledge_evidence", ["document_key"])

    op.create_table(
        "knowledge_index_vectors",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("revision_id", sa.UUID(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["knowledge_revisions.id"],
            name=op.f("fk_knowledge_index_vectors_revision_id_knowledge_revisions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_index_vectors")),
        sa.UniqueConstraint("revision_id", name="uq_knowledge_index_vectors_revision_id"),
    )

    op.create_table(
        "index_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.UUID(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("result", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "event_type IN ('knowledge.index', 'knowledge.unindex', 'knowledge.reconcile')",
            name=op.f("ck_index_outbox_event_type_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')", name=op.f("ck_index_outbox_status_valid")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_index_outbox_attempts_nonnegative")),
        sa.CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)", name=op.f("ck_index_outbox_completed_has_time")
        ),
        sa.CheckConstraint(
            "(status = 'completed') = (result IS NOT NULL)", name=op.f("ck_index_outbox_completed_has_result")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_index_outbox")),
        sa.UniqueConstraint("idempotency_key", name="uq_index_outbox_idempotency_key"),
    )
    op.create_index("ix_index_outbox_due", "index_outbox", ["status", "available_at"])

    op.execute(
        """
        CREATE FUNCTION enqueue_index_outbox(p_key text, p_type text, p_aggregate uuid, p_payload jsonb)
        RETURNS void AS $$
        BEGIN
            INSERT INTO index_outbox (idempotency_key, event_type, aggregate_id, payload)
            VALUES (p_key, p_type, p_aggregate, COALESCE(p_payload, '{}'::jsonb))
            ON CONFLICT (idempotency_key) DO NOTHING;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_entries_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'knowledge entries are never deleted' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'UPDATE' AND (NEW.id, NEW.author_user_id, NEW.created_at)
               IS DISTINCT FROM (OLD.id, OLD.author_user_id, OLD.created_at) THEN
                RAISE EXCEPTION 'knowledge entry identity is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.current_revision_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM knowledge_revisions
                WHERE id = NEW.current_revision_id AND entry_id = NEW.id
            ) THEN
                RAISE EXCEPTION 'current revision does not belong to this entry' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_entries_guard
        BEFORE INSERT OR UPDATE OR DELETE ON knowledge_entries
        FOR EACH ROW EXECUTE FUNCTION knowledge_entries_guard();
        """
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_revisions_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'knowledge revisions are never deleted' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'published' THEN
                RAISE EXCEPTION 'knowledge cannot be published by this workflow' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'draft' THEN
                    RAISE EXCEPTION 'a knowledge revision must be created as a draft' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF (NEW.id, NEW.entry_id, NEW.author_user_id, NEW.revision_number, NEW.statement, NEW.category,
                NEW.visibility, NEW.scope_group_id, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.entry_id, OLD.author_user_id, OLD.revision_number, OLD.statement, OLD.category,
                OLD.visibility, OLD.scope_group_id, OLD.created_at) THEN
                RAISE EXCEPTION 'knowledge revision content is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                OLD.status = 'draft' AND NEW.status IN ('superseded', 'withdrawn')
            ) THEN
                RAISE EXCEPTION 'a draft revision can only be superseded or withdrawn' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_revisions_guard
        BEFORE INSERT OR UPDATE OR DELETE ON knowledge_revisions
        FOR EACH ROW EXECUTE FUNCTION knowledge_revisions_guard();
        """
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_revisions_enqueue() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                UPDATE knowledge_entries SET current_revision_id = NEW.id WHERE id = NEW.entry_id;
                PERFORM enqueue_index_outbox(
                    'knowledge.index.' || NEW.id, 'knowledge.index', NEW.id,
                    jsonb_build_object('entry_id', NEW.entry_id, 'revision_number', NEW.revision_number)
                );
            ELSIF NEW.status IS DISTINCT FROM OLD.status AND NEW.status IN ('withdrawn', 'superseded') THEN
                PERFORM enqueue_index_outbox(
                    'knowledge.unindex.' || NEW.id || '.' || NEW.status, 'knowledge.unindex', NEW.id,
                    jsonb_build_object('reason', NEW.status)
                );
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_revisions_enqueue
        AFTER INSERT OR UPDATE ON knowledge_revisions
        FOR EACH ROW EXECUTE FUNCTION knowledge_revisions_enqueue();
        """
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_evidence_guard() RETURNS trigger AS $$
        DECLARE
            rev knowledge_revisions%ROWTYPE;
            doc documents%ROWTYPE;
            chunk document_chunks%ROWTYPE;
            expected text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'knowledge evidence is never deleted' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF (NEW.revision_id, NEW.document_key, NEW.document_version, NEW.document_sha256, NEW.filename,
                    NEW.chunk_key, NEW.page_number, NEW.start_offset, NEW.end_offset, NEW.quote, NEW.prefix,
                    NEW.suffix, NEW.created_at)
                   IS DISTINCT FROM
                   (OLD.revision_id, OLD.document_key, OLD.document_version, OLD.document_sha256, OLD.filename,
                    OLD.chunk_key, OLD.page_number, OLD.start_offset, OLD.end_offset, OLD.quote, OLD.prefix,
                    OLD.suffix, OLD.created_at) THEN
                    RAISE EXCEPTION 'knowledge evidence provenance is immutable' USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.document_id IS DISTINCT FROM OLD.document_id AND NEW.document_id IS NOT NULL THEN
                    RAISE EXCEPTION 'evidence cannot be retargeted to another document' USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.chunk_id IS DISTINCT FROM OLD.chunk_id AND NEW.chunk_id IS NOT NULL THEN
                    RAISE EXCEPTION 'evidence cannot be retargeted to another passage' USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.orphan_reason IS DISTINCT FROM OLD.orphan_reason
                   OR NEW.orphaned_at IS DISTINCT FROM OLD.orphaned_at THEN
                    IF OLD.orphan_reason IS NOT NULL THEN
                        RAISE EXCEPTION 'evidence is already orphaned' USING ERRCODE = 'check_violation';
                    END IF;
                    IF NEW.orphan_reason IS NULL OR NEW.orphaned_at IS NULL THEN
                        RAISE EXCEPTION 'orphan reason and time are set together' USING ERRCODE = 'check_violation';
                    END IF;
                END IF;
                RETURN NEW;
            END IF;

            SELECT * INTO rev FROM knowledge_revisions WHERE id = NEW.revision_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'evidence revision is missing' USING ERRCODE = 'check_violation';
            END IF;
            SELECT * INTO doc FROM documents WHERE id = NEW.document_key;
            IF NOT FOUND OR doc.acl_status <> 'active' THEN
                RAISE EXCEPTION 'evidence source is not readable' USING ERRCODE = 'check_violation';
            END IF;
            IF doc.version IS DISTINCT FROM NEW.document_version
               OR doc.indexed_version IS DISTINCT FROM NEW.document_version
               OR doc.sha256 IS DISTINCT FROM NEW.document_sha256
               OR doc.filename IS DISTINCT FROM NEW.filename THEN
                RAISE EXCEPTION 'evidence must cite the current indexed document revision'
                    USING ERRCODE = 'check_violation';
            END IF;
            SELECT * INTO chunk FROM document_chunks WHERE id = NEW.chunk_key;
            IF NOT FOUND OR chunk.document_id IS DISTINCT FROM doc.id OR chunk.page_number IS DISTINCT FROM NEW.page_number THEN
                RAISE EXCEPTION 'evidence passage does not belong to the source document' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.end_offset > char_length(chunk.content)
               OR NEW.start_offset < char_length(NEW.prefix)
               OR NEW.end_offset + char_length(NEW.suffix) > char_length(chunk.content) THEN
                RAISE EXCEPTION 'evidence offsets fall outside the source passage' USING ERRCODE = 'check_violation';
            END IF;
            expected := substring(chunk.content FROM NEW.start_offset + 1 FOR NEW.end_offset - NEW.start_offset);
            IF expected IS DISTINCT FROM NEW.quote THEN
                RAISE EXCEPTION 'evidence quote does not match the source passage' USING ERRCODE = 'check_violation';
            END IF;
            expected := substring(chunk.content FROM NEW.start_offset - char_length(NEW.prefix) + 1 FOR char_length(NEW.prefix));
            IF expected IS DISTINCT FROM NEW.prefix THEN
                RAISE EXCEPTION 'evidence prefix does not match the source passage' USING ERRCODE = 'check_violation';
            END IF;
            expected := substring(chunk.content FROM NEW.end_offset + 1 FOR char_length(NEW.suffix));
            IF expected IS DISTINCT FROM NEW.suffix THEN
                RAISE EXCEPTION 'evidence suffix does not match the source passage' USING ERRCODE = 'check_violation';
            END IF;
            IF doc.visibility = 'private' AND (rev.visibility <> 'private' OR rev.author_user_id <> doc.owner_id) THEN
                RAISE EXCEPTION 'knowledge visibility cannot exceed the source' USING ERRCODE = 'check_violation';
            END IF;
            IF doc.visibility = 'restricted' AND rev.visibility = 'shared' THEN
                RAISE EXCEPTION 'knowledge visibility cannot exceed the source' USING ERRCODE = 'check_violation';
            END IF;
            IF doc.visibility = 'restricted' AND rev.visibility = 'restricted' AND NOT EXISTS (
                SELECT 1 FROM resource_acl
                WHERE document_id = doc.id AND principal_group_id = rev.scope_group_id
            ) THEN
                RAISE EXCEPTION 'knowledge scope is broader than the source ACL' USING ERRCODE = 'check_violation';
            END IF;
            IF doc.visibility = 'restricted' AND rev.visibility = 'private'
               AND rev.author_user_id <> doc.owner_id
               AND NOT EXISTS (
                    SELECT 1 FROM resource_acl
                    WHERE document_id = doc.id AND principal_user_id = rev.author_user_id
               )
               AND NOT EXISTS (
                    SELECT 1 FROM resource_acl acl
                    JOIN access_group_members mem ON mem.group_id = acl.principal_group_id
                    WHERE acl.document_id = doc.id AND mem.user_id = rev.author_user_id
               ) THEN
                RAISE EXCEPTION 'author cannot read the evidence source' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_evidence_guard
        BEFORE INSERT OR UPDATE OR DELETE ON knowledge_evidence
        FOR EACH ROW EXECUTE FUNCTION knowledge_evidence_guard();
        """
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_revision_has_evidence() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM knowledge_evidence WHERE revision_id = NEW.id) THEN
                RAISE EXCEPTION 'a knowledge revision requires evidence' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER knowledge_revision_has_evidence
        AFTER INSERT ON knowledge_revisions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION knowledge_revision_has_evidence();
        """
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_index_vectors_guard() RETURNS trigger AS $$
        DECLARE
            rev_status text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            SELECT status INTO rev_status FROM knowledge_revisions WHERE id = NEW.revision_id;
            IF rev_status IS DISTINCT FROM 'published' THEN
                RAISE EXCEPTION 'unpublished knowledge cannot be indexed' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_index_vectors_guard
        BEFORE INSERT OR UPDATE ON knowledge_index_vectors
        FOR EACH ROW EXECUTE FUNCTION knowledge_index_vectors_guard();
        """
    )
    op.execute(
        """
        CREATE FUNCTION index_outbox_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'index outbox events are never deleted' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'pending' OR NEW.attempts <> 0 OR NEW.completed_at IS NOT NULL OR NEW.result IS NOT NULL THEN
                    RAISE EXCEPTION 'outbox events are created pending' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF (NEW.id, NEW.idempotency_key, NEW.event_type, NEW.aggregate_id, NEW.payload, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.idempotency_key, OLD.event_type, OLD.aggregate_id, OLD.payload, OLD.created_at) THEN
                RAISE EXCEPTION 'outbox event identity is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.attempts < OLD.attempts THEN
                RAISE EXCEPTION 'outbox attempts cannot decrease' USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.status = 'completed' THEN
                RAISE EXCEPTION 'outbox event is already completed' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'completed' AND OLD.status <> 'processing' THEN
                RAISE EXCEPTION 'only a claimed outbox event can be completed' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'failed' AND OLD.status <> 'processing' THEN
                RAISE EXCEPTION 'only a claimed outbox event can fail' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'pending' AND OLD.status NOT IN ('processing', 'failed') THEN
                RAISE EXCEPTION 'outbox event cannot return to pending' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'processing' AND OLD.status NOT IN ('pending', 'processing', 'failed') THEN
                RAISE EXCEPTION 'outbox event cannot be claimed' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER index_outbox_guard
        BEFORE INSERT OR UPDATE OR DELETE ON index_outbox
        FOR EACH ROW EXECUTE FUNCTION index_outbox_guard();
        """
    )
    op.execute(
        """
        CREATE FUNCTION documents_orphan_evidence() RETURNS trigger AS $$
        DECLARE
            rev uuid;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                UPDATE knowledge_evidence
                SET orphan_reason = 'source_deleted', orphaned_at = now()
                WHERE document_key = OLD.id AND orphan_reason IS NULL;
                FOR rev IN
                    SELECT DISTINCT revision_id FROM knowledge_evidence WHERE document_key = OLD.id
                LOOP
                    PERFORM enqueue_index_outbox(
                        'knowledge.unindex.' || rev || '.deleted.' || OLD.id,
                        'knowledge.unindex', rev,
                        jsonb_build_object('reason', 'source_deleted', 'document_id', OLD.id)
                    );
                END LOOP;
                RETURN OLD;
            END IF;
            IF NEW.version IS DISTINCT FROM OLD.version THEN
                UPDATE knowledge_evidence
                SET orphan_reason = 'document_revision_changed', orphaned_at = now()
                WHERE document_key = NEW.id AND document_version IS DISTINCT FROM NEW.version AND orphan_reason IS NULL;
                FOR rev IN
                    SELECT DISTINCT revision_id FROM knowledge_evidence
                    WHERE document_key = NEW.id AND document_version IS DISTINCT FROM NEW.version
                LOOP
                    PERFORM enqueue_index_outbox(
                        'knowledge.unindex.' || rev || '.document.' || NEW.id || '.v' || NEW.version,
                        'knowledge.unindex', rev,
                        jsonb_build_object('reason', 'document_revision_changed', 'document_id', NEW.id, 'version', NEW.version)
                    );
                END LOOP;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER documents_orphan_evidence
        BEFORE UPDATE OF version OR DELETE ON documents
        FOR EACH ROW EXECUTE FUNCTION documents_orphan_evidence();
        """
    )
    op.execute(
        """
        CREATE FUNCTION document_chunks_orphan_evidence() RETURNS trigger AS $$
        DECLARE
            rev uuid;
        BEGIN
            UPDATE knowledge_evidence
            SET orphan_reason = 'passage_removed', orphaned_at = now()
            WHERE chunk_key = OLD.id AND orphan_reason IS NULL;
            FOR rev IN
                SELECT DISTINCT revision_id FROM knowledge_evidence WHERE chunk_key = OLD.id
            LOOP
                PERFORM enqueue_index_outbox(
                    'knowledge.unindex.' || rev || '.chunk.' || OLD.id,
                    'knowledge.unindex', rev,
                    jsonb_build_object('reason', 'passage_removed', 'chunk_id', OLD.id)
                );
            END LOOP;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER document_chunks_orphan_evidence
        BEFORE DELETE ON document_chunks
        FOR EACH ROW EXECUTE FUNCTION document_chunks_orphan_evidence();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS document_chunks_orphan_evidence ON document_chunks")
    op.execute("DROP FUNCTION IF EXISTS document_chunks_orphan_evidence()")
    op.execute("DROP TRIGGER IF EXISTS documents_orphan_evidence ON documents")
    op.execute("DROP FUNCTION IF EXISTS documents_orphan_evidence()")
    op.execute("DROP TRIGGER IF EXISTS index_outbox_guard ON index_outbox")
    op.execute("DROP FUNCTION IF EXISTS index_outbox_guard()")
    op.execute("DROP TRIGGER IF EXISTS knowledge_index_vectors_guard ON knowledge_index_vectors")
    op.execute("DROP FUNCTION IF EXISTS knowledge_index_vectors_guard()")
    op.execute("DROP TRIGGER IF EXISTS knowledge_revision_has_evidence ON knowledge_revisions")
    op.execute("DROP FUNCTION IF EXISTS knowledge_revision_has_evidence()")
    op.execute("DROP TRIGGER IF EXISTS knowledge_evidence_guard ON knowledge_evidence")
    op.execute("DROP FUNCTION IF EXISTS knowledge_evidence_guard()")
    op.execute("DROP TRIGGER IF EXISTS knowledge_revisions_enqueue ON knowledge_revisions")
    op.execute("DROP FUNCTION IF EXISTS knowledge_revisions_enqueue()")
    op.execute("DROP TRIGGER IF EXISTS knowledge_revisions_guard ON knowledge_revisions")
    op.execute("DROP FUNCTION IF EXISTS knowledge_revisions_guard()")
    op.execute("DROP TRIGGER IF EXISTS knowledge_entries_guard ON knowledge_entries")
    op.execute("DROP FUNCTION IF EXISTS knowledge_entries_guard()")
    op.execute("DROP FUNCTION IF EXISTS enqueue_index_outbox(text, text, uuid, jsonb)")
    op.drop_table("index_outbox")
    op.drop_table("knowledge_index_vectors")
    op.drop_table("knowledge_evidence")
    op.drop_constraint("fk_knowledge_entries_current_revision", "knowledge_entries", type_="foreignkey")
    op.drop_table("knowledge_revisions")
    op.drop_table("knowledge_entries")
