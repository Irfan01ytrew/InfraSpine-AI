"""Module 08 continuity foundation, frozen PostgreSQL DDL.

Independent branch from the audited 0084 baseline. Merge Alembic heads with other
module migrations at integration; never renumber or replace existing revisions.
"""
from alembic import op

revision = "bc08_0001"
down_revision = "0084_immutable_conversation_messages"
branch_labels = ("continuity08",)
depends_on = None


def upgrade():
    op.execute('CREATE TABLE bc_tenants (\n\tid UUID NOT NULL, \n\tname VARCHAR(160) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tevent_head VARCHAR(64) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_bc_tenants PRIMARY KEY (id)\n)')
    op.execute('CREATE TABLE bc_events (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tsequence INTEGER NOT NULL, \n\tactor_id UUID NOT NULL, \n\taction VARCHAR(64) NOT NULL, \n\tsubject_id UUID NOT NULL, \n\tdata JSONB NOT NULL, \n\tprevious_hash VARCHAR(64) NOT NULL, \n\tdigest VARCHAR(64) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_bc_events PRIMARY KEY (id), \n\tCONSTRAINT uq_bc_events_tenant_id UNIQUE (tenant_id, sequence), \n\tCONSTRAINT fk_bc_events_tenant_id_bc_tenants FOREIGN KEY(tenant_id) REFERENCES bc_tenants (id), \n\tCONSTRAINT fk_bc_events_actor_id_users FOREIGN KEY(actor_id) REFERENCES users (id)\n)')
    op.execute('CREATE INDEX ix_bc_events_tenant_id ON bc_events (tenant_id)')
    op.execute('CREATE TABLE bc_grant_requests (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tdata JSONB NOT NULL, \n\tdigest VARCHAR(64) NOT NULL, \n\trequested_by UUID NOT NULL, \n\tdecided_by UUID, \n\tstatus VARCHAR(16) NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_bc_grant_requests PRIMARY KEY (id), \n\tCONSTRAINT fk_bc_grant_requests_tenant_id_bc_tenants FOREIGN KEY(tenant_id) REFERENCES bc_tenants (id), \n\tCONSTRAINT fk_bc_grant_requests_requested_by_users FOREIGN KEY(requested_by) REFERENCES users (id), \n\tCONSTRAINT fk_bc_grant_requests_decided_by_users FOREIGN KEY(decided_by) REFERENCES users (id)\n)')
    op.execute('CREATE INDEX ix_bc_grant_requests_tenant_id ON bc_grant_requests (tenant_id)')
    op.execute("CREATE TABLE bc_members (\n\ttenant_id UUID NOT NULL, \n\tuser_id UUID NOT NULL, \n\trole VARCHAR(20) NOT NULL, \n\tdepartments JSONB NOT NULL, \n\tall_departments BOOLEAN NOT NULL, \n\tactive BOOLEAN NOT NULL, \n\tCONSTRAINT pk_bc_members PRIMARY KEY (tenant_id, user_id), \n\tCONSTRAINT ck_bc_members_role CHECK (role IN ('owner','approver','planner','executor','viewer')), \n\tCONSTRAINT fk_bc_members_tenant_id_bc_tenants FOREIGN KEY(tenant_id) REFERENCES bc_tenants (id), \n\tCONSTRAINT fk_bc_members_user_id_users FOREIGN KEY(user_id) REFERENCES users (id)\n)")
    op.execute('CREATE TABLE bc_scenarios (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tname VARCHAR(180) NOT NULL, \n\tkind VARCHAR(40) NOT NULL, \n\tsource_key VARCHAR(180), \n\tdata JSONB NOT NULL, \n\tcreated_by UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_bc_scenarios PRIMARY KEY (id), \n\tCONSTRAINT uq_bc_scenarios_tenant_id UNIQUE (tenant_id, id), \n\tCONSTRAINT uq_bc_scenarios_tenant_id UNIQUE (tenant_id, source_key), \n\tCONSTRAINT fk_bc_scenarios_tenant_id_bc_tenants FOREIGN KEY(tenant_id) REFERENCES bc_tenants (id), \n\tCONSTRAINT fk_bc_scenarios_created_by_users FOREIGN KEY(created_by) REFERENCES users (id)\n)')
    op.execute('CREATE INDEX ix_bc_scenarios_tenant_id ON bc_scenarios (tenant_id)')
    op.execute('CREATE TABLE bc_nodes (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tdepartment_id UUID, \n\taccess_group_id UUID, \n\tuser_id UUID, \n\texternal_key VARCHAR(180) NOT NULL, \n\tkind VARCHAR(32) NOT NULL, \n\tname VARCHAR(180) NOT NULL, \n\tdata JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_bc_nodes PRIMARY KEY (id), \n\tCONSTRAINT uq_bc_nodes_tenant_id UNIQUE (tenant_id, id), \n\tCONSTRAINT uq_bc_nodes_tenant_id UNIQUE (tenant_id, external_key), \n\tCONSTRAINT uq_bc_nodes_access_group_id UNIQUE (access_group_id), \n\tCONSTRAINT fk_bc_nodes_tenant_id_bc_nodes FOREIGN KEY(tenant_id, department_id) REFERENCES bc_nodes (tenant_id, id), \n\tCONSTRAINT fk_bc_nodes_tenant_id_bc_tenants FOREIGN KEY(tenant_id) REFERENCES bc_tenants (id), \n\tCONSTRAINT fk_bc_nodes_access_group_id_access_groups FOREIGN KEY(access_group_id) REFERENCES access_groups (id), \n\tCONSTRAINT fk_bc_nodes_user_id_users FOREIGN KEY(user_id) REFERENCES users (id)\n)')
    op.execute('CREATE INDEX ix_bc_nodes_tenant_id ON bc_nodes (tenant_id)')
    op.execute('CREATE TABLE bc_assessments (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tnode_id UUID NOT NULL, \n\tdata JSONB NOT NULL, \n\tcreated_by UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_bc_assessments PRIMARY KEY (id), \n\tCONSTRAINT fk_bc_assessments_tenant_id_bc_nodes FOREIGN KEY(tenant_id, node_id) REFERENCES bc_nodes (tenant_id, id), \n\tCONSTRAINT fk_bc_assessments_tenant_id_bc_tenants FOREIGN KEY(tenant_id) REFERENCES bc_tenants (id), \n\tCONSTRAINT fk_bc_assessments_created_by_users FOREIGN KEY(created_by) REFERENCES users (id)\n)')
    op.execute('CREATE INDEX ix_bc_assessments_tenant_id ON bc_assessments (tenant_id)')
    op.execute('CREATE TABLE bc_links (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tsource_id UUID NOT NULL, \n\ttarget_id UUID NOT NULL, \n\tkind VARCHAR(32) NOT NULL, \n\tdata JSONB NOT NULL, \n\tCONSTRAINT pk_bc_links PRIMARY KEY (id), \n\tCONSTRAINT fk_bc_link_source FOREIGN KEY(tenant_id, source_id) REFERENCES bc_nodes (tenant_id, id), \n\tCONSTRAINT fk_bc_link_target FOREIGN KEY(tenant_id, target_id) REFERENCES bc_nodes (tenant_id, id), \n\tCONSTRAINT uq_bc_links_tenant_id UNIQUE (tenant_id, source_id, target_id, kind), \n\tCONSTRAINT ck_bc_links_distinct_endpoints CHECK (source_id <> target_id), \n\tCONSTRAINT fk_bc_links_tenant_id_bc_tenants FOREIGN KEY(tenant_id) REFERENCES bc_tenants (id)\n)')
    op.execute('CREATE INDEX ix_bc_links_tenant_id ON bc_links (tenant_id)')
    op.execute("CREATE TABLE bc_plans (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tfamily_id UUID NOT NULL, \n\tversion INTEGER NOT NULL, \n\tdepartment_id UUID, \n\ttitle VARCHAR(180) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tdata JSONB NOT NULL, \n\tdigest VARCHAR(64) NOT NULL, \n\trequested_by UUID NOT NULL, \n\tdecided_by UUID, \n\tdecision_reason TEXT, \n\tapproval_expires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\treview_due_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_bc_plans PRIMARY KEY (id), \n\tCONSTRAINT uq_bc_plans_tenant_id UNIQUE (tenant_id, id), \n\tCONSTRAINT uq_bc_plans_tenant_id UNIQUE (tenant_id, family_id, version), \n\tCONSTRAINT fk_bc_plans_tenant_id_bc_nodes FOREIGN KEY(tenant_id, department_id) REFERENCES bc_nodes (tenant_id, id), \n\tCONSTRAINT ck_bc_plans_status CHECK (status IN ('pending','approved','rejected')), \n\tCONSTRAINT fk_bc_plans_tenant_id_bc_tenants FOREIGN KEY(tenant_id) REFERENCES bc_tenants (id), \n\tCONSTRAINT fk_bc_plans_requested_by_users FOREIGN KEY(requested_by) REFERENCES users (id), \n\tCONSTRAINT fk_bc_plans_decided_by_users FOREIGN KEY(decided_by) REFERENCES users (id)\n)")
    op.execute('CREATE INDEX ix_bc_plans_tenant_id ON bc_plans (tenant_id)')
    op.execute("CREATE TABLE bc_runs (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tplan_id UUID NOT NULL, \n\tscenario_id UUID, \n\tidempotency_key VARCHAR(100) NOT NULL, \n\tdigest VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tmode VARCHAR(16) NOT NULL, \n\trequested_by UUID NOT NULL, \n\tdecided_by UUID, \n\tdecision_reason TEXT, \n\tapproval_expires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tdisruption_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tactivated_at TIMESTAMP WITH TIME ZONE, \n\tclosed_at TIMESTAMP WITH TIME ZONE, \n\treview JSONB, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_bc_runs PRIMARY KEY (id), \n\tCONSTRAINT uq_bc_runs_tenant_id UNIQUE (tenant_id, id), \n\tCONSTRAINT uq_bc_runs_tenant_id UNIQUE (tenant_id, idempotency_key), \n\tCONSTRAINT fk_bc_runs_tenant_id_bc_plans FOREIGN KEY(tenant_id, plan_id) REFERENCES bc_plans (tenant_id, id), \n\tCONSTRAINT fk_bc_runs_tenant_id_bc_scenarios FOREIGN KEY(tenant_id, scenario_id) REFERENCES bc_scenarios (tenant_id, id), \n\tCONSTRAINT ck_bc_runs_status CHECK (status IN ('pending','active','suspended','rejected','closed')), \n\tCONSTRAINT fk_bc_runs_tenant_id_bc_tenants FOREIGN KEY(tenant_id) REFERENCES bc_tenants (id), \n\tCONSTRAINT fk_bc_runs_requested_by_users FOREIGN KEY(requested_by) REFERENCES users (id), \n\tCONSTRAINT fk_bc_runs_decided_by_users FOREIGN KEY(decided_by) REFERENCES users (id)\n)")
    op.execute('CREATE INDEX ix_bc_runs_tenant_id ON bc_runs (tenant_id)')
    op.execute("CREATE TABLE bc_tasks (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\trun_id UUID NOT NULL, \n\tstep_key VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\tworkflow_run_id UUID, \n\tevidence JSONB, \n\tsubmitted_by UUID, \n\tverified_by UUID, \n\tverified_at TIMESTAMP WITH TIME ZONE, \n\tCONSTRAINT pk_bc_tasks PRIMARY KEY (id), \n\tCONSTRAINT fk_bc_tasks_tenant_id_bc_runs FOREIGN KEY(tenant_id, run_id) REFERENCES bc_runs (tenant_id, id), \n\tCONSTRAINT uq_bc_tasks_tenant_id UNIQUE (tenant_id, run_id, step_key), \n\tCONSTRAINT uq_bc_tasks_workflow_run_id UNIQUE (workflow_run_id), \n\tCONSTRAINT ck_bc_tasks_status CHECK (status IN ('pending','dispatched','evidence_submitted','verified')), \n\tCONSTRAINT fk_bc_tasks_tenant_id_bc_tenants FOREIGN KEY(tenant_id) REFERENCES bc_tenants (id), \n\tCONSTRAINT fk_bc_tasks_workflow_run_id_automation_runs FOREIGN KEY(workflow_run_id) REFERENCES automation_runs (id), \n\tCONSTRAINT fk_bc_tasks_submitted_by_users FOREIGN KEY(submitted_by) REFERENCES users (id), \n\tCONSTRAINT fk_bc_tasks_verified_by_users FOREIGN KEY(verified_by) REFERENCES users (id)\n)")
    op.execute('CREATE INDEX ix_bc_tasks_tenant_id ON bc_tasks (tenant_id)')
    op.execute("""CREATE FUNCTION bc_deny_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'continuity evidence history is immutable' USING ERRCODE = '55000'; END;
        $$""")
    for table in ('bc_events', 'bc_assessments', 'bc_scenarios'):
        op.execute(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION bc_deny_rewrite()')
    op.execute("""CREATE FUNCTION bc_plan_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(NEW.tenant_id, NEW.family_id, NEW.version, NEW.department_id, NEW.title, NEW.data, NEW.digest, NEW.requested_by, NEW.review_due_at, NEW.approval_expires_at)
             IS DISTINCT FROM ROW(OLD.tenant_id, OLD.family_id, OLD.version, OLD.department_id, OLD.title, OLD.data, OLD.digest, OLD.requested_by, OLD.review_due_at, OLD.approval_expires_at)
          THEN RAISE EXCEPTION 'continuity plan version is immutable' USING ERRCODE = '55000'; END IF;
          IF OLD.status <> 'pending' AND ROW(NEW.status, NEW.decided_by, NEW.decision_reason) IS DISTINCT FROM ROW(OLD.status, OLD.decided_by, OLD.decision_reason)
          THEN RAISE EXCEPTION 'continuity decision is immutable' USING ERRCODE = '55000'; END IF;
          RETURN NEW;
        END; $$""")
    op.execute('CREATE TRIGGER bc_plans_immutable BEFORE UPDATE ON bc_plans FOR EACH ROW EXECUTE FUNCTION bc_plan_immutable()')


def downgrade():
    # Refuse destructive rollback once customer continuity records exist.
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM bc_tenants) THEN RAISE EXCEPTION 'Archive continuity evidence before an explicitly authorized destructive rollback'; END IF; END $$")
    op.drop_table('bc_tasks')
    op.drop_table('bc_runs')
    op.drop_table('bc_plans')
    op.drop_table('bc_links')
    op.drop_table('bc_assessments')
    op.drop_table('bc_nodes')
    op.drop_table('bc_scenarios')
    op.drop_table('bc_members')
    op.drop_table('bc_grant_requests')
    op.drop_table('bc_events')
    op.drop_table('bc_tenants')
    op.execute('DROP FUNCTION bc_plan_immutable()')
    op.execute('DROP FUNCTION bc_deny_rewrite()')
