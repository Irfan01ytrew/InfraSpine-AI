"""Shared platform core (step C): scopes, roles, memberships, channel identities, approvals bound to the exact
action, the exactly-once execution ledger, the outbox and users.auth_version. Additive; modules adopt it in step D.

Revision ID: 0097_platform_core
Revises: 0096_integration_connectors
Create Date: 2026-11-26 00:35:33.595581
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0097_platform_core'
down_revision: str | None = '0096_integration_connectors'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('platform_outbox',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('event_type', sa.String(length=80), nullable=False),
    sa.Column('scope_id', sa.UUID(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('trace_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_platform_outbox'))
    )
    op.create_index('ix_platform_outbox_pending', 'platform_outbox', ['id'], unique=False, postgresql_where=sa.text('dispatched_at IS NULL'))
    op.create_table('platform_roles',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('system', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_platform_roles')),
    sa.UniqueConstraint('key', name=op.f('uq_platform_roles_key'))
    )
    op.create_table('platform_scopes',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('parent_id', sa.UUID(), nullable=True),
    sa.Column('module', sa.String(length=40), nullable=True),
    sa.Column('legacy_table', sa.String(length=80), nullable=True),
    sa.Column('legacy_id', sa.UUID(), nullable=True),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(kind = 'org') = (parent_id IS NULL)", name=op.f('ck_platform_scopes_only_org_is_root')),
    sa.CheckConstraint("kind IN ('org', 'department', 'workspace', 'module_space')", name=op.f('ck_platform_scopes_kind_valid')),
    sa.ForeignKeyConstraint(['parent_id'], ['platform_scopes.id'], name=op.f('fk_platform_scopes_parent_id_platform_scopes'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_platform_scopes')),
    sa.UniqueConstraint('legacy_table', 'legacy_id', name='uq_platform_scopes_legacy')
    )
    op.create_index('ix_platform_scopes_parent_id', 'platform_scopes', ['parent_id'], unique=False)
    op.create_index('uq_platform_scopes_single_org', 'platform_scopes', ['kind'], unique=True, postgresql_where=sa.text("kind = 'org'"))
    op.create_table('platform_approval_requests',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('scope_id', sa.UUID(), nullable=False),
    sa.Column('action_type', sa.String(length=80), nullable=False),
    sa.Column('action_hash', sa.String(length=64), nullable=False),
    sa.Column('action', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=True),
    sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('required_permission', sa.String(length=80), nullable=False),
    sa.Column('requested_by', sa.UUID(), nullable=False),
    sa.Column('requested_via', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_by', sa.UUID(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_note', sa.String(length=500), nullable=True),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('trace_id', sa.UUID(), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'expired', 'consumed', 'cancelled')", name=op.f('ck_platform_approval_requests_status_valid')),
    sa.CheckConstraint('char_length(action_hash) = 64', name=op.f('ck_platform_approval_requests_hash_length')),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], name=op.f('fk_platform_approval_requests_decided_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], name=op.f('fk_platform_approval_requests_requested_by_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['scope_id'], ['platform_scopes.id'], name=op.f('fk_platform_approval_requests_scope_id_platform_scopes'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_platform_approval_requests'))
    )
    op.create_index('ix_platform_approval_requests_scope_status', 'platform_approval_requests', ['scope_id', 'status'], unique=False)
    op.create_index('ix_platform_approval_requests_subject', 'platform_approval_requests', ['subject'], unique=False)
    op.create_table('platform_channel_identities',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('channel', sa.String(length=32), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=False),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('verification_method', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("channel IN ('whatsapp_cloud', 'whatsapp_baileys', 'api')", name=op.f('ck_platform_channel_identities_channel_valid')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_platform_channel_identities_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_platform_channel_identities'))
    )
    op.create_index('ix_platform_channel_identities_user_id', 'platform_channel_identities', ['user_id'], unique=False)
    op.create_index('uq_platform_channel_identities_active', 'platform_channel_identities', ['channel', 'external_id'], unique=True, postgresql_where=sa.text('revoked_at IS NULL'))
    op.create_table('platform_memberships',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('scope_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('role_id', sa.UUID(), nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True),
    sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('granted_by', sa.UUID(), nullable=True),
    sa.Column('granted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from', name=op.f('ck_platform_memberships_valid_range')),
    sa.ForeignKeyConstraint(['granted_by'], ['users.id'], name=op.f('fk_platform_memberships_granted_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['role_id'], ['platform_roles.id'], name=op.f('fk_platform_memberships_role_id_platform_roles'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['scope_id'], ['platform_scopes.id'], name=op.f('fk_platform_memberships_scope_id_platform_scopes'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_platform_memberships_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_platform_memberships'))
    )
    op.create_index('ix_platform_memberships_user_id', 'platform_memberships', ['user_id'], unique=False)
    op.create_index('uq_platform_memberships_active', 'platform_memberships', ['scope_id', 'user_id', 'role_id'], unique=True, postgresql_where=sa.text('revoked_at IS NULL'))
    op.create_table('platform_role_permissions',
    sa.Column('role_id', sa.UUID(), nullable=False),
    sa.Column('permission', sa.String(length=80), nullable=False),
    sa.ForeignKeyConstraint(['role_id'], ['platform_roles.id'], name=op.f('fk_platform_role_permissions_role_id_platform_roles'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('role_id', 'permission', name=op.f('pk_platform_role_permissions'))
    )
    op.create_table('platform_action_executions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=200), nullable=False),
    sa.Column('action_type', sa.String(length=80), nullable=False),
    sa.Column('action_hash', sa.String(length=64), nullable=False),
    sa.Column('duty', sa.String(length=80), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=True),
    sa.Column('scope_id', sa.UUID(), nullable=False),
    sa.Column('approval_id', sa.UUID(), nullable=True),
    sa.Column('executor_user_id', sa.UUID(), nullable=False),
    sa.Column('executor_kind', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), server_default=sa.text("'claimed'"), nullable=False),
    sa.Column('attempt', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('external_ref', sa.String(length=500), nullable=True),
    sa.Column('verified_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('failure_code', sa.String(length=80), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('trace_id', sa.UUID(), nullable=False),
    sa.CheckConstraint("status IN ('claimed', 'dispatched', 'verified', 'failed')", name=op.f('ck_platform_action_executions_status_valid')),
    sa.ForeignKeyConstraint(['approval_id'], ['platform_approval_requests.id'], name=op.f('fk_platform_action_executions_approval_id_platform_approval_requests'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['executor_user_id'], ['users.id'], name=op.f('fk_platform_action_executions_executor_user_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['scope_id'], ['platform_scopes.id'], name=op.f('fk_platform_action_executions_scope_id_platform_scopes'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_platform_action_executions')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_platform_action_executions_idempotency_key'))
    )
    op.create_index('ix_platform_action_executions_subject', 'platform_action_executions', ['subject'], unique=False)
    op.add_column('users', sa.Column('auth_version', sa.Integer(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'auth_version')
    op.drop_index('ix_platform_action_executions_subject', table_name='platform_action_executions')
    op.drop_table('platform_action_executions')
    op.drop_table('platform_role_permissions')
    op.drop_index('uq_platform_memberships_active', table_name='platform_memberships', postgresql_where=sa.text('revoked_at IS NULL'))
    op.drop_index('ix_platform_memberships_user_id', table_name='platform_memberships')
    op.drop_table('platform_memberships')
    op.drop_index('uq_platform_channel_identities_active', table_name='platform_channel_identities', postgresql_where=sa.text('revoked_at IS NULL'))
    op.drop_index('ix_platform_channel_identities_user_id', table_name='platform_channel_identities')
    op.drop_table('platform_channel_identities')
    op.drop_index('ix_platform_approval_requests_subject', table_name='platform_approval_requests')
    op.drop_index('ix_platform_approval_requests_scope_status', table_name='platform_approval_requests')
    op.drop_table('platform_approval_requests')
    op.drop_index('uq_platform_scopes_single_org', table_name='platform_scopes', postgresql_where=sa.text("kind = 'org'"))
    op.drop_index('ix_platform_scopes_parent_id', table_name='platform_scopes')
    op.drop_table('platform_scopes')
    op.drop_table('platform_roles')
    op.drop_index('ix_platform_outbox_pending', table_name='platform_outbox', postgresql_where=sa.text('dispatched_at IS NULL'))
    op.drop_table('platform_outbox')
