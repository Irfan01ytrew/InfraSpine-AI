"""Independent encrypted Baileys QR sessions and durable self-chat inbox."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from alembic import op

revision = "0087_agent_studio_baileys"
down_revision = "0086_agent_studio_whatsapp"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_studio_baileys_connections",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("agent_id", pg.UUID(as_uuid=True), sa.ForeignKey("agent_studio_agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("thread_id", pg.UUID(as_uuid=True), sa.ForeignKey("agent_studio_threads.id", ondelete="SET NULL")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(40), nullable=False, server_default="starting"),
        sa.Column("auth_sealed", sa.Text()), sa.Column("qr_sealed", sa.Text()),
        sa.Column("qr_expires_at", sa.DateTime(timezone=True)),
        sa.Column("phone_suffix", sa.String(8)), sa.Column("last_error", sa.String(200)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table("agent_studio_baileys_messages",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("connection_id", pg.UUID(as_uuid=True), sa.ForeignKey("agent_studio_baileys_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_id", sa.String(160), nullable=False, unique=True),
        sa.Column("payload_sealed", sa.Text(), nullable=False), sa.Column("reply_sealed", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("last_error", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_studio_baileys_messages_connection_id", "agent_studio_baileys_messages", ["connection_id"])
    op.create_index("ix_agent_studio_baileys_messages_status", "agent_studio_baileys_messages", ["status"])


def downgrade():
    op.drop_table("agent_studio_baileys_messages")
    op.drop_table("agent_studio_baileys_connections")
