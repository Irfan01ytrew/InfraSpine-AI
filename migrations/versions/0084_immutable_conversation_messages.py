"""Make committed conversation messages append-only (feedback remains mutable).

Revision ID: 0084_immutable_conversation_messages
Revises: 0083_document_text_formats
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0084_immutable_conversation_messages"
down_revision: str | None = "0083_document_text_formats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION prevent_conversation_message_rewrite() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(
                NEW.id, NEW.conversation_id, NEW.role, NEW.content, NEW.provider, NEW.model,
                NEW.strategy, NEW.insufficient_evidence, NEW.payload, NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.id, OLD.conversation_id, OLD.role, OLD.content, OLD.provider, OLD.model,
                OLD.strategy, OLD.insufficient_evidence, OLD.payload, OLD.created_at
            ) THEN
                RAISE EXCEPTION 'conversation messages are immutable after insert'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_conversation_messages_immutable
        BEFORE UPDATE ON conversation_messages
        FOR EACH ROW EXECUTE FUNCTION prevent_conversation_message_rewrite();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_conversation_messages_immutable ON conversation_messages")
    op.execute("DROP FUNCTION IF EXISTS prevent_conversation_message_rewrite()")
