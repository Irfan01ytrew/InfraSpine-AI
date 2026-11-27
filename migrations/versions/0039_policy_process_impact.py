"""Map approved policies to processes and assign auditable remediation exceptions.

Revision ID: 0039_policy_process_impact
Revises: 0038_knowledge_conflicts
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0039_policy_process_impact"
down_revision = "0038_knowledge_conflicts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_processes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False, server_default=""),
        sa.Column("scope_group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("access_groups.id", ondelete="RESTRICT")),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 1 AND 180", name="ck_knowledge_processes_name_length"),
        sa.CheckConstraint("length(btrim(description)) <= 2000", name="ck_knowledge_processes_description_length"),
    )
    op.create_index("ix_knowledge_processes_scope", "knowledge_processes", ["scope_group_id", "created_at"])

    op.create_table(
        "knowledge_process_policy_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("process_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_processes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_entries.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("linked_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("process_id", "entry_id", name="uq_knowledge_process_policy_link"),
    )
    op.create_index("ix_knowledge_process_policy_links_entry", "knowledge_process_policy_links", ["entry_id", "active"])

    op.create_table(
        "knowledge_policy_exceptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("process_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_processes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_entries.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_revisions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("assigned_to", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("rationale", sa.String(2000), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('open', 'acknowledged', 'resolved')", name="ck_knowledge_policy_exceptions_status_valid"),
        sa.CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="ck_knowledge_policy_exceptions_rationale_length"),
        sa.CheckConstraint("due_at IS NULL OR due_at > created_at", name="ck_knowledge_policy_exceptions_due_after_created"),
    )
    op.create_index("ix_knowledge_policy_exceptions_assignee", "knowledge_policy_exceptions", ["assigned_to", "status", "created_at"])
    op.create_index("ix_knowledge_policy_exceptions_process", "knowledge_policy_exceptions", ["process_id", "status", "created_at"])

    op.create_table(
        "knowledge_policy_exception_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("exception_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_policy_exceptions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("from_status", sa.String(16)),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("from_assignee_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT")),
        sa.Column("to_assignee_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("rationale", sa.String(2000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("action IN ('assigned', 'reassigned', 'acknowledged', 'resolved')", name="ck_knowledge_policy_exception_events_action_valid"),
        sa.CheckConstraint("from_status IS NULL OR from_status IN ('open', 'acknowledged', 'resolved')", name="ck_knowledge_policy_exception_events_from_status_valid"),
        sa.CheckConstraint("to_status IN ('open', 'acknowledged', 'resolved')", name="ck_knowledge_policy_exception_events_to_status_valid"),
        sa.CheckConstraint("length(btrim(rationale)) BETWEEN 3 AND 2000", name="ck_knowledge_policy_exception_events_rationale_length"),
    )
    op.create_index("ix_knowledge_policy_exception_events_exception", "knowledge_policy_exception_events", ["exception_id", "id"])

    op.execute("""
        CREATE FUNCTION knowledge_process_policy_link_guard() RETURNS trigger AS $$
        DECLARE p knowledge_processes%ROWTYPE; e knowledge_entries%ROWTYPE; r knowledge_revisions%ROWTYPE;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'policy process relationships are retained; deactivate the link' USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF (NEW.id, NEW.process_id, NEW.entry_id, NEW.linked_by, NEW.created_at) IS DISTINCT FROM
                   (OLD.id, OLD.process_id, OLD.entry_id, OLD.linked_by, OLD.created_at) OR
                   (NEW.active = OLD.active) THEN
                    RAISE EXCEPTION 'policy process link identity is immutable' USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            SELECT * INTO p FROM knowledge_processes WHERE id = NEW.process_id AND archived_at IS NULL FOR SHARE;
            SELECT * INTO e FROM knowledge_entries WHERE id = NEW.entry_id FOR SHARE;
            IF p.id IS NULL OR e.id IS NULL THEN
                RAISE EXCEPTION 'policy process link target is unavailable' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.active THEN
                SELECT * INTO r FROM knowledge_revisions
                WHERE entry_id = NEW.entry_id AND status = 'published'
                ORDER BY revision_number DESC LIMIT 1 FOR SHARE;
                IF r.id IS NULL OR r.category NOT IN ('policy', 'legal', 'operational') OR
                   (p.scope_group_id IS NULL AND r.scope_group_id IS NOT NULL) OR
                   (p.scope_group_id IS NOT NULL AND r.scope_group_id IS NOT NULL AND p.scope_group_id <> r.scope_group_id) THEN
                    RAISE EXCEPTION 'only a published, scope-compatible policy can be linked' USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER knowledge_process_policy_link_guard
        BEFORE INSERT OR UPDATE OR DELETE ON knowledge_process_policy_links
        FOR EACH ROW EXECUTE FUNCTION knowledge_process_policy_link_guard()
    """)

    op.execute("""
        CREATE FUNCTION knowledge_policy_exception_insert_guard() RETURNS trigger AS $$
        DECLARE p knowledge_processes%ROWTYPE; r knowledge_revisions%ROWTYPE;
        BEGIN
            SELECT * INTO p FROM knowledge_processes WHERE id = NEW.process_id AND archived_at IS NULL FOR SHARE;
            SELECT * INTO r FROM knowledge_revisions WHERE id = NEW.revision_id FOR SHARE;
            IF p.id IS NULL OR r.id IS NULL OR r.entry_id <> NEW.entry_id OR
               r.status NOT IN ('submitted', 'published') OR r.category NOT IN ('policy', 'legal', 'operational') OR
               NEW.status <> 'open' OR
               NOT EXISTS (SELECT 1 FROM knowledge_process_policy_links l WHERE l.process_id = p.id AND l.entry_id = NEW.entry_id AND l.active) OR
               (p.scope_group_id IS NULL AND r.scope_group_id IS NOT NULL) OR
               (p.scope_group_id IS NOT NULL AND r.scope_group_id IS NOT NULL AND p.scope_group_id <> r.scope_group_id) OR
               NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.assigned_to AND is_active) OR
               NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.created_by AND is_active) THEN
                RAISE EXCEPTION 'policy exception does not match an active linked policy or active assignee' USING ERRCODE = 'check_violation';
            END IF;
            IF p.scope_group_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM access_group_members m WHERE m.group_id = p.scope_group_id AND m.user_id = NEW.assigned_to
                UNION ALL
                SELECT 1 FROM department_managers d WHERE d.group_id = p.scope_group_id AND d.manager_user_id = NEW.assigned_to
            ) THEN
                RAISE EXCEPTION 'policy exception assignee is outside the process department' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER knowledge_policy_exception_insert_guard
        BEFORE INSERT ON knowledge_policy_exceptions
        FOR EACH ROW EXECUTE FUNCTION knowledge_policy_exception_insert_guard()
    """)

    op.execute("""
        CREATE FUNCTION knowledge_policy_exception_event_guard() RETURNS trigger AS $$
        DECLARE x knowledge_policy_exceptions%ROWTYPE;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'policy exception history is append-only' USING ERRCODE = 'check_violation';
            END IF;
            SELECT * INTO x FROM knowledge_policy_exceptions WHERE id = NEW.exception_id FOR UPDATE;
            IF x.id IS NULL OR NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.actor_user_id AND is_active) OR
               NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.to_assignee_id AND is_active) THEN
                RAISE EXCEPTION 'policy exception event actor or assignee is inactive' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.action = 'assigned' THEN
                IF NEW.from_status IS NOT NULL OR NEW.to_status <> 'open' OR NEW.from_assignee_id IS NOT NULL OR
                   x.status <> 'open' OR x.assigned_to <> NEW.to_assignee_id THEN
                    RAISE EXCEPTION 'initial policy exception assignment event does not match' USING ERRCODE = 'check_violation';
                END IF;
            ELSIF NEW.from_status IS DISTINCT FROM x.status OR NEW.from_assignee_id IS DISTINCT FROM x.assigned_to OR
                  (NEW.action = 'reassigned' AND (NEW.to_status <> x.status OR x.status = 'resolved' OR NEW.to_assignee_id = x.assigned_to)) OR
                  (NEW.action = 'acknowledged' AND (x.status <> 'open' OR NEW.to_status <> 'acknowledged' OR NEW.actor_user_id <> x.assigned_to OR NEW.to_assignee_id <> x.assigned_to)) OR
                  (NEW.action = 'resolved' AND (x.status NOT IN ('open', 'acknowledged') OR NEW.to_status <> 'resolved' OR NEW.actor_user_id <> x.assigned_to OR NEW.to_assignee_id <> x.assigned_to)) OR
                  NEW.action NOT IN ('reassigned', 'acknowledged', 'resolved') THEN
                RAISE EXCEPTION 'policy exception event does not match the current assignment or status' USING ERRCODE = 'check_violation';
            END IF;
            IF EXISTS (SELECT 1 FROM knowledge_processes p WHERE p.id = x.process_id AND p.scope_group_id IS NOT NULL)
               AND NOT EXISTS (
                   SELECT 1 FROM knowledge_processes p
                   WHERE p.id = x.process_id AND EXISTS (
                       SELECT 1 FROM access_group_members m WHERE m.group_id = p.scope_group_id AND m.user_id = NEW.to_assignee_id
                       UNION ALL
                       SELECT 1 FROM department_managers d WHERE d.group_id = p.scope_group_id AND d.manager_user_id = NEW.to_assignee_id
                   )
               ) THEN
                RAISE EXCEPTION 'policy exception event assignee is outside the process department' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER knowledge_policy_exception_event_guard
        BEFORE INSERT OR UPDATE OR DELETE ON knowledge_policy_exception_events
        FOR EACH ROW EXECUTE FUNCTION knowledge_policy_exception_event_guard()
    """)

    op.execute("""
        CREATE FUNCTION knowledge_policy_exception_state_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'policy exception assignment history cannot be deleted' USING ERRCODE = 'check_violation';
            END IF;
            IF (NEW.id, NEW.process_id, NEW.entry_id, NEW.revision_id, NEW.created_by, NEW.rationale, NEW.due_at, NEW.created_at) IS DISTINCT FROM
               (OLD.id, OLD.process_id, OLD.entry_id, OLD.revision_id, OLD.created_by, OLD.rationale, OLD.due_at, OLD.created_at) OR
               (NEW.status = OLD.status AND NEW.assigned_to = OLD.assigned_to) OR
               NOT EXISTS (
                   SELECT 1 FROM knowledge_policy_exception_events e
                   WHERE e.exception_id = OLD.id AND e.from_status = OLD.status AND e.to_status = NEW.status
                     AND e.from_assignee_id = OLD.assigned_to AND e.to_assignee_id = NEW.assigned_to
               ) THEN
                RAISE EXCEPTION 'policy exception changes require a matching immutable history event' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER knowledge_policy_exception_state_guard
        BEFORE UPDATE OR DELETE ON knowledge_policy_exceptions
        FOR EACH ROW EXECUTE FUNCTION knowledge_policy_exception_state_guard()
    """)


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM knowledge_processes) OR EXISTS (SELECT 1 FROM knowledge_policy_exceptions) THEN
            RAISE EXCEPTION 'cannot downgrade while process impact or exception history exists';
        END IF;
    END $$;""")
    for table, trigger, function in (
        ("knowledge_policy_exceptions", "knowledge_policy_exception_state_guard", "knowledge_policy_exception_state_guard"),
        ("knowledge_policy_exception_events", "knowledge_policy_exception_event_guard", "knowledge_policy_exception_event_guard"),
        ("knowledge_policy_exceptions", "knowledge_policy_exception_insert_guard", "knowledge_policy_exception_insert_guard"),
        ("knowledge_process_policy_links", "knowledge_process_policy_link_guard", "knowledge_process_policy_link_guard"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
    op.drop_index("ix_knowledge_policy_exception_events_exception", table_name="knowledge_policy_exception_events")
    op.drop_table("knowledge_policy_exception_events")
    op.drop_index("ix_knowledge_policy_exceptions_process", table_name="knowledge_policy_exceptions")
    op.drop_index("ix_knowledge_policy_exceptions_assignee", table_name="knowledge_policy_exceptions")
    op.drop_table("knowledge_policy_exceptions")
    op.drop_index("ix_knowledge_process_policy_links_entry", table_name="knowledge_process_policy_links")
    op.drop_table("knowledge_process_policy_links")
    op.drop_index("ix_knowledge_processes_scope", table_name="knowledge_processes")
    op.drop_table("knowledge_processes")
