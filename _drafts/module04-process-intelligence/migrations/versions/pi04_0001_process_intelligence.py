"""Module 04 isolated event discovery. Does not alter existing module tables.

Branch integration: this revision branches from 0084. If another module adds a
head, add an explicit Alembic merge revision after reviewing both migrations.
"""
from alembic import op

revision = "pi04_0001"
down_revision = "0084_immutable_conversation_messages"
branch_labels = ("process_intelligence",)
depends_on = None


def upgrade():
    op.execute('CREATE TABLE pi_tenants (\n\tname VARCHAR(160) NOT NULL, \n\tcreated_by UUID NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_pi_tenants PRIMARY KEY (id), \n\tCONSTRAINT fk_pi_tenants_created_by_users FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT\n)')
    op.execute("CREATE TABLE pi_memberships (\n\ttenant_id UUID NOT NULL, \n\tuser_id UUID NOT NULL, \n\trole VARCHAR(16) NOT NULL, \n\tCONSTRAINT pk_pi_memberships PRIMARY KEY (tenant_id, user_id), \n\tCONSTRAINT ck_pi_memberships_role CHECK (role IN ('viewer','analyst','steward')), \n\tCONSTRAINT fk_pi_memberships_tenant_id_pi_tenants FOREIGN KEY(tenant_id) REFERENCES pi_tenants (id) ON DELETE CASCADE, \n\tCONSTRAINT fk_pi_memberships_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE\n)")
    op.execute('CREATE TABLE pi_processes (\n\ttenant_id UUID NOT NULL, \n\tname VARCHAR(160) NOT NULL, \n\tdescription VARCHAR(2000) NOT NULL, \n\towner_id UUID NOT NULL, \n\trevision INTEGER NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_pi_processes PRIMARY KEY (id), \n\tCONSTRAINT uq_pi_processes_tenant_id UNIQUE (tenant_id, id), \n\tCONSTRAINT ck_pi_processes_revision CHECK (revision >= 0), \n\tCONSTRAINT fk_pi_processes_tenant_id_pi_tenants FOREIGN KEY(tenant_id) REFERENCES pi_tenants (id) ON DELETE CASCADE, \n\tCONSTRAINT fk_pi_processes_owner_id_users FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE RESTRICT\n)')
    op.execute("CREATE TABLE pi_sources (\n\ttenant_id UUID NOT NULL, \n\tprocess_id UUID NOT NULL, \n\towner_id UUID NOT NULL, \n\tname VARCHAR(160) NOT NULL, \n\tkind VARCHAR(24) NOT NULL, \n\tdocument_id UUID, \n\tactive BOOLEAN NOT NULL, \n\tretention_days INTEGER NOT NULL, \n\tcursor VARCHAR(256), \n\tsynced_at TIMESTAMP WITH TIME ZONE, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_pi_sources PRIMARY KEY (id), \n\tCONSTRAINT fk_pi_sources_tenant_id_pi_processes FOREIGN KEY(tenant_id, process_id) REFERENCES pi_processes (tenant_id, id) ON DELETE CASCADE, \n\tCONSTRAINT uq_pi_sources_process_identity UNIQUE (tenant_id, process_id, id), \n\tCONSTRAINT uq_pi_sources_identity UNIQUE (tenant_id, id), \n\tCONSTRAINT ck_pi_sources_kind CHECK (kind IN ('erp','crm','hrms','accounting','email','document','ticket','workflow','api','event_log')), \n\tCONSTRAINT ck_pi_sources_retention CHECK (retention_days BETWEEN 1 AND 3650), \n\tCONSTRAINT fk_pi_sources_owner_id_users FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_pi_sources_document_id_documents FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE RESTRICT\n)")
    op.execute('CREATE TABLE pi_source_grants (\n\ttenant_id UUID NOT NULL, \n\tsource_id UUID NOT NULL, \n\tuser_id UUID NOT NULL, \n\tCONSTRAINT pk_pi_source_grants PRIMARY KEY (tenant_id, source_id, user_id), \n\tCONSTRAINT fk_pi_source_grants_tenant_id_pi_sources FOREIGN KEY(tenant_id, source_id) REFERENCES pi_sources (tenant_id, id) ON DELETE CASCADE, \n\tCONSTRAINT fk_pi_source_grants_tenant_id_pi_memberships FOREIGN KEY(tenant_id, user_id) REFERENCES pi_memberships (tenant_id, user_id) ON DELETE CASCADE\n)')
    op.execute('CREATE TABLE pi_events (\n\ttenant_id UUID NOT NULL, \n\tprocess_id UUID NOT NULL, \n\tsource_id UUID NOT NULL, \n\texternal_id VARCHAR(160) NOT NULL, \n\tcase_id VARCHAR(160) NOT NULL, \n\tactivity VARCHAR(160) NOT NULL, \n\tactor VARCHAR(160), \n\tdepartment VARCHAR(160), \n\toutcome VARCHAR(80), \n\toccurred_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\tterminal BOOLEAN NOT NULL, \n\tevidence_ref VARCHAR(320) NOT NULL, \n\tpayload_hash VARCHAR(64) NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_pi_events PRIMARY KEY (id), \n\tCONSTRAINT fk_pi_events_tenant_id_pi_sources FOREIGN KEY(tenant_id, process_id, source_id) REFERENCES pi_sources (tenant_id, process_id, id) ON DELETE CASCADE, \n\tCONSTRAINT uq_pi_events_source_id UNIQUE (source_id, external_id), \n\tCONSTRAINT ck_pi_events_duration CHECK (finished_at IS NULL OR finished_at >= occurred_at)\n)')
    op.execute('CREATE INDEX ix_pi_events_case ON pi_events (tenant_id, process_id, source_id, case_id, occurred_at)')
    op.execute('CREATE TABLE pi_traces (\n\ttenant_id UUID NOT NULL, \n\tprocess_id UUID NOT NULL, \n\tsource_id UUID NOT NULL, \n\tcase_id VARCHAR(160) NOT NULL, \n\tdata JSONB NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_pi_traces PRIMARY KEY (id), \n\tCONSTRAINT fk_pi_traces_tenant_id_pi_sources FOREIGN KEY(tenant_id, process_id, source_id) REFERENCES pi_sources (tenant_id, process_id, id) ON DELETE CASCADE, \n\tCONSTRAINT uq_pi_traces_source_id UNIQUE (source_id, case_id)\n)')
    op.execute("CREATE TABLE pi_baselines (\n\ttenant_id UUID NOT NULL, \n\tprocess_id UUID NOT NULL, \n\tsource_id UUID NOT NULL, \n\tcreated_by UUID NOT NULL, \n\treviewed_by UUID, \n\tstatus VARCHAR(16) NOT NULL, \n\torigin VARCHAR(24) NOT NULL, \n\tdata JSONB NOT NULL, \n\treviewed_at TIMESTAMP WITH TIME ZONE, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_pi_baselines PRIMARY KEY (id), \n\tCONSTRAINT fk_pi_baselines_tenant_id_pi_sources FOREIGN KEY(tenant_id, process_id, source_id) REFERENCES pi_sources (tenant_id, process_id, id) ON DELETE CASCADE, \n\tCONSTRAINT ck_pi_baselines_status CHECK (status IN ('proposed','approved','rejected')), \n\tCONSTRAINT ck_pi_baselines_independent_review CHECK (reviewed_by IS NULL OR reviewed_by <> created_by), \n\tCONSTRAINT fk_pi_baselines_created_by_users FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_pi_baselines_reviewed_by_users FOREIGN KEY(reviewed_by) REFERENCES users (id) ON DELETE RESTRICT\n)")
    op.execute('CREATE TABLE pi_models (\n\ttenant_id UUID NOT NULL, \n\tprocess_id UUID NOT NULL, \n\towner_id UUID NOT NULL, \n\trevision INTEGER NOT NULL, \n\tinput_hash VARCHAR(64) NOT NULL, \n\tsource_ids JSONB NOT NULL, \n\tdata JSONB NOT NULL, \n\treview_status VARCHAR(16) NOT NULL, \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_pi_models PRIMARY KEY (id), \n\tCONSTRAINT fk_pi_models_tenant_id_pi_processes FOREIGN KEY(tenant_id, process_id) REFERENCES pi_processes (tenant_id, id) ON DELETE CASCADE, \n\tCONSTRAINT uq_pi_models_process_id UNIQUE (process_id, owner_id, input_hash), \n\tCONSTRAINT fk_pi_models_owner_id_users FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE RESTRICT\n)')
    op.execute('CREATE INDEX ix_pi_models_scope ON pi_models (tenant_id, process_id, owner_id, created_at)')
    op.execute("CREATE TABLE pi_jobs (\n\ttenant_id UUID NOT NULL, \n\tprocess_id UUID NOT NULL, \n\towner_id UUID NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tclaim UUID, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tmodel_id UUID, \n\terror_code VARCHAR(64), \n\tid UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_pi_jobs PRIMARY KEY (id), \n\tCONSTRAINT fk_pi_jobs_tenant_id_pi_processes FOREIGN KEY(tenant_id, process_id) REFERENCES pi_processes (tenant_id, id) ON DELETE CASCADE, \n\tCONSTRAINT ck_pi_jobs_status CHECK (status IN ('queued','processing','completed','failed')), \n\tCONSTRAINT fk_pi_jobs_owner_id_users FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_pi_jobs_model_id_pi_models FOREIGN KEY(model_id) REFERENCES pi_models (id) ON DELETE SET NULL\n)")
    op.execute('CREATE INDEX ix_pi_jobs_queue ON pi_jobs (status, lease_until, created_at)')
    op.execute("""
    CREATE FUNCTION pi_prevent_event_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'process events are immutable' USING ERRCODE = '55000'; END; $$;
    """)
    op.execute("""
    CREATE TRIGGER pi_events_immutable BEFORE UPDATE ON pi_events
    FOR EACH ROW EXECUTE FUNCTION pi_prevent_event_rewrite();
    """)
    op.execute("""
    CREATE FUNCTION pi_prevent_snapshot_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF ROW(NEW.tenant_id, NEW.process_id, NEW.owner_id, NEW.revision, NEW.input_hash, NEW.source_ids, NEW.data, NEW.created_at)
           IS DISTINCT FROM ROW(OLD.tenant_id, OLD.process_id, OLD.owner_id, OLD.revision, OLD.input_hash, OLD.source_ids, OLD.data, OLD.created_at)
        THEN RAISE EXCEPTION 'process snapshots are immutable' USING ERRCODE = '55000'; END IF;
        RETURN NEW;
    END; $$;
    """)
    op.execute("""
    CREATE TRIGGER pi_models_immutable BEFORE UPDATE ON pi_models
    FOR EACH ROW EXECUTE FUNCTION pi_prevent_snapshot_rewrite();
    """)


def downgrade():
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pi_events) THEN RAISE EXCEPTION 'archive process evidence before downgrading'; END IF; END $$")
    op.execute("DROP TRIGGER pi_models_immutable ON pi_models")
    op.execute("DROP FUNCTION pi_prevent_snapshot_rewrite()")
    op.execute("DROP TRIGGER pi_events_immutable ON pi_events")
    op.execute("DROP FUNCTION pi_prevent_event_rewrite()")
    op.drop_table('pi_jobs')
    op.drop_table('pi_models')
    op.drop_table('pi_baselines')
    op.drop_table('pi_traces')
    op.drop_table('pi_events')
    op.drop_table('pi_source_grants')
    op.drop_table('pi_sources')
    op.drop_table('pi_processes')
    op.drop_table('pi_memberships')
    op.drop_table('pi_tenants')
