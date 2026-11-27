"""Module 10: create the generic integration connector tables defined in app/integrations/models.py.

The models (connectors, jobs, records, events, OAuth attempts) shipped without a migration, so
/api/integrations failed on a migrated database. Tables are created only when absent, because some
development databases may already hold them from the application model.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0096_integration_connectors"
down_revision = "0095_agent_studio_pause"
branch_labels = None
depends_on = None

TABLES = ("integration_oauth_attempts", "integration_events", "integration_records", "integration_jobs", "integration_connectors")


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if 'integration_connectors' not in existing:
        op.create_table('integration_connectors',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('owner_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('base_url', sa.String(length=1000), nullable=False),
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('credentials', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('health', sa.String(length=32), nullable=False),
        sa.Column('last_error', sa.String(length=64), nullable=True),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('next_sync_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('circuit_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failures', sa.Integer(), nullable=False),
        sa.Column('cursor', sa.String(length=2048), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('version > 0', name=op.f('ck_integration_connectors_version')),
        sa.ForeignKeyConstraint(['tenant_id', 'owner_id'], ['company_graph_members.tenant_id', 'company_graph_members.user_id'], name=op.f('fk_integration_connectors_tenant_id_company_graph_members'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_integration_connectors')),
        sa.UniqueConstraint('tenant_id', 'owner_id', 'id', name='uq_integration_connector_scope')
        )
        op.create_index('ix_integration_connector_scope', 'integration_connectors', ['tenant_id', 'owner_id'], unique=False)
    if 'integration_jobs' not in existing:
        op.create_table('integration_jobs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('owner_id', sa.UUID(), nullable=False),
        sa.Column('connector_id', sa.UUID(), nullable=False),
        sa.Column('connector_version', sa.Integer(), nullable=False),
        sa.Column('operation', sa.String(length=40), nullable=False),
        sa.Column('idempotency_key', sa.String(length=120), nullable=False),
        sa.Column('digest', sa.String(length=64), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('claim_token', sa.UUID(), nullable=True),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('approval_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_by', sa.UUID(), nullable=True),
        sa.Column('last_error', sa.String(length=64), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending_approval','queued','processing','dispatching','completed','dead_letter','uncertain','cancelled')", name=op.f('ck_integration_jobs_status')),
        sa.CheckConstraint('attempts BETWEEN 0 AND 5', name=op.f('ck_integration_jobs_attempts')),
        sa.ForeignKeyConstraint(['approved_by'], ['users.id'], name=op.f('fk_integration_jobs_approved_by_users'), ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['tenant_id', 'owner_id', 'connector_id'], ['integration_connectors.tenant_id', 'integration_connectors.owner_id', 'integration_connectors.id'], name=op.f('fk_integration_jobs_tenant_id_integration_connectors'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_integration_jobs')),
        sa.UniqueConstraint('connector_id', 'idempotency_key', name='uq_integration_job_key')
        )
        op.create_index('ix_integration_job_claim', 'integration_jobs', ['status', 'available_at'], unique=False)
        op.create_index('ix_integration_job_scope', 'integration_jobs', ['tenant_id', 'owner_id', 'created_at'], unique=False)
    if 'integration_records' not in existing:
        op.create_table('integration_records',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('owner_id', sa.UUID(), nullable=False),
        sa.Column('connector_id', sa.UUID(), nullable=False),
        sa.Column('external_id', sa.String(length=240), nullable=False),
        sa.Column('digest', sa.String(length=64), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('schema_version', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id', 'owner_id', 'connector_id'], ['integration_connectors.tenant_id', 'integration_connectors.owner_id', 'integration_connectors.id'], name=op.f('fk_integration_records_tenant_id_integration_connectors'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_integration_records')),
        sa.UniqueConstraint('connector_id', 'external_id', name='uq_integration_record_external')
        )
    if 'integration_events' not in existing:
        op.create_table('integration_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('owner_id', sa.UUID(), nullable=False),
        sa.Column('connector_id', sa.UUID(), nullable=False),
        sa.Column('external_id', sa.String(length=120), nullable=False),
        sa.Column('event_type', sa.String(length=80), nullable=False),
        sa.Column('digest', sa.String(length=64), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('target_module', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('consumer_reference', sa.String(length=160), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id', 'owner_id', 'connector_id'], ['integration_connectors.tenant_id', 'integration_connectors.owner_id', 'integration_connectors.id'], name=op.f('fk_integration_events_tenant_id_integration_connectors'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_integration_events')),
        sa.UniqueConstraint('connector_id', 'external_id', name='uq_integration_event_external')
        )
        op.create_index('ix_integration_event_scope', 'integration_events', ['tenant_id', 'owner_id', 'created_at'], unique=False)
    if 'integration_oauth_attempts' not in existing:
        op.create_table('integration_oauth_attempts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('owner_id', sa.UUID(), nullable=False),
        sa.Column('connector_id', sa.UUID(), nullable=False),
        sa.Column('state_hash', sa.String(length=64), nullable=False),
        sa.Column('verifier', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('connector_version', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id', 'owner_id', 'connector_id'], ['integration_connectors.tenant_id', 'integration_connectors.owner_id', 'integration_connectors.id'], name=op.f('fk_integration_oauth_attempts_tenant_id_integration_connectors'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_integration_oauth_attempts')),
        sa.UniqueConstraint('state_hash', name=op.f('uq_integration_oauth_attempts_state_hash'))
        )


def downgrade():
    for table in TABLES:  # dependents first
        op.execute(f"DROP TABLE IF EXISTS {table}")
