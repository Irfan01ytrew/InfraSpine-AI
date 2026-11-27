"""Module 06 tenant boundary, immutable policies, decisions and chained security events.

Development migration on 0084. Reconcile with the primary 0085-0088 branch before integration.
"""
from alembic import op

revision = "0085_governance_security"
down_revision = "0084_immutable_conversation_messages"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("\nCREATE TABLE security_tenants (\n\tid UUID NOT NULL, \n\tname VARCHAR(160) NOT NULL, \n\tenabled BOOLEAN DEFAULT true NOT NULL, \n\tpolicy_version INTEGER DEFAULT '0' NOT NULL, \n\taudit_sequence INTEGER DEFAULT '0' NOT NULL, \n\taudit_head VARCHAR(64) DEFAULT '0000000000000000000000000000000000000000000000000000000000000000' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_security_tenants PRIMARY KEY (id)\n)\n\n")
    op.execute('\nCREATE TABLE security_departments (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tname VARCHAR(120) NOT NULL, \n\tparent_id UUID, \n\tCONSTRAINT pk_security_departments PRIMARY KEY (id), \n\tCONSTRAINT uq_security_departments_tenant_id UNIQUE (tenant_id, id), \n\tCONSTRAINT uq_security_departments_tenant_id UNIQUE (tenant_id, name), \n\tCONSTRAINT fk_security_departments_tenant_id_security_departments FOREIGN KEY(tenant_id, parent_id) REFERENCES security_departments (tenant_id, id), \n\tCONSTRAINT ck_security_departments_not_self_parent CHECK (id <> parent_id), \n\tCONSTRAINT fk_security_departments_tenant_id_security_tenants FOREIGN KEY(tenant_id) REFERENCES security_tenants (id)\n)\n\n')
    op.execute("\nCREATE TABLE security_events (\n\ttenant_id UUID NOT NULL, \n\tsequence INTEGER DEFAULT '0' NOT NULL, \n\tbody JSONB NOT NULL, \n\tprevious_hash VARCHAR(64) DEFAULT '' NOT NULL, \n\tevent_hash VARCHAR(64) DEFAULT '' NOT NULL, \n\tCONSTRAINT pk_security_events PRIMARY KEY (tenant_id, sequence), \n\tCONSTRAINT fk_security_events_tenant_id_security_tenants FOREIGN KEY(tenant_id) REFERENCES security_tenants (id)\n)\n\n")
    op.execute('\nCREATE TABLE security_policy_versions (\n\ttenant_id UUID NOT NULL, \n\tversion INTEGER NOT NULL, \n\tbase_version INTEGER NOT NULL, \n\trules JSONB NOT NULL, \n\tcreated_by UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_security_policy_versions PRIMARY KEY (tenant_id, version), \n\tCONSTRAINT ck_security_policy_versions_version_order CHECK (version > base_version AND base_version >= 0), \n\tCONSTRAINT fk_security_policy_versions_tenant_id_security_tenants FOREIGN KEY(tenant_id) REFERENCES security_tenants (id), \n\tCONSTRAINT fk_security_policy_versions_created_by_users FOREIGN KEY(created_by) REFERENCES users (id)\n)\n\n')
    op.execute("\nCREATE TABLE security_roles (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tname VARCHAR(120) NOT NULL, \n\tpermissions JSONB DEFAULT '[]'::jsonb NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_security_roles PRIMARY KEY (id), \n\tCONSTRAINT uq_security_roles_tenant_id UNIQUE (tenant_id, id), \n\tCONSTRAINT uq_security_roles_tenant_id UNIQUE (tenant_id, name), \n\tCONSTRAINT fk_security_roles_tenant_id_security_tenants FOREIGN KEY(tenant_id) REFERENCES security_tenants (id)\n)\n\n")
    op.execute("\nCREATE TABLE security_members (\n\ttenant_id UUID NOT NULL, \n\tuser_id UUID NOT NULL, \n\tdepartment_id UUID, \n\tposition_title VARCHAR(120) DEFAULT '' NOT NULL, \n\tall_departments BOOLEAN DEFAULT false NOT NULL, \n\tactive BOOLEAN DEFAULT true NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_security_members PRIMARY KEY (tenant_id, user_id), \n\tCONSTRAINT uq_security_members_user_id UNIQUE (user_id), \n\tCONSTRAINT fk_security_members_tenant_id_security_departments FOREIGN KEY(tenant_id, department_id) REFERENCES security_departments (tenant_id, id), \n\tCONSTRAINT fk_security_members_tenant_id_security_tenants FOREIGN KEY(tenant_id) REFERENCES security_tenants (id), \n\tCONSTRAINT fk_security_members_user_id_users FOREIGN KEY(user_id) REFERENCES users (id)\n)\n\n")
    op.execute("\nCREATE TABLE security_resources (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tkind VARCHAR(32) NOT NULL, \n\texternal_id UUID NOT NULL, \n\tdepartment_id UUID, \n\tclassification VARCHAR(24) NOT NULL, \n\tlabel VARCHAR(160) NOT NULL, \n\tCONSTRAINT pk_security_resources PRIMARY KEY (id), \n\tCONSTRAINT uq_security_resources_tenant_id UNIQUE (tenant_id, id), \n\tCONSTRAINT uq_security_resources_kind UNIQUE (kind, external_id), \n\tCONSTRAINT fk_security_resources_tenant_id_security_departments FOREIGN KEY(tenant_id, department_id) REFERENCES security_departments (tenant_id, id), \n\tCONSTRAINT ck_security_resources_kind CHECK (kind IN ('document', 'agent')), \n\tCONSTRAINT ck_security_resources_classification CHECK (classification IN ('internal','confidential','restricted')), \n\tCONSTRAINT fk_security_resources_tenant_id_security_tenants FOREIGN KEY(tenant_id) REFERENCES security_tenants (id)\n)\n\n")
    op.execute("\nCREATE TABLE security_agents (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\towner_user_id UUID NOT NULL, \n\tname VARCHAR(120) NOT NULL, \n\ttools JSONB NOT NULL, \n\tenabled BOOLEAN DEFAULT true NOT NULL, \n\tmax_calls INTEGER NOT NULL, \n\twindow_seconds INTEGER NOT NULL, \n\ttimeout_seconds INTEGER NOT NULL, \n\tcalls_used INTEGER DEFAULT '0' NOT NULL, \n\twindow_started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_security_agents PRIMARY KEY (id), \n\tCONSTRAINT uq_security_agents_tenant_id UNIQUE (tenant_id, id), \n\tCONSTRAINT fk_security_agents_tenant_id_security_members FOREIGN KEY(tenant_id, owner_user_id) REFERENCES security_members (tenant_id, user_id), \n\tCONSTRAINT ck_security_agents_budget CHECK (max_calls BETWEEN 1 AND 1000 AND calls_used >= 0), \n\tCONSTRAINT ck_security_agents_limits CHECK (window_seconds BETWEEN 60 AND 86400 AND timeout_seconds BETWEEN 1 AND 120), \n\tCONSTRAINT fk_security_agents_tenant_id_security_tenants FOREIGN KEY(tenant_id) REFERENCES security_tenants (id)\n)\n\n")
    op.execute('\nCREATE TABLE security_role_assignments (\n\ttenant_id UUID NOT NULL, \n\tuser_id UUID NOT NULL, \n\trole_id UUID NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE, \n\trevoked_at TIMESTAMP WITH TIME ZONE, \n\tCONSTRAINT pk_security_role_assignments PRIMARY KEY (tenant_id, user_id, role_id), \n\tCONSTRAINT fk_security_role_assignments_tenant_id_security_members FOREIGN KEY(tenant_id, user_id) REFERENCES security_members (tenant_id, user_id), \n\tCONSTRAINT fk_security_role_assignments_tenant_id_security_roles FOREIGN KEY(tenant_id, role_id) REFERENCES security_roles (tenant_id, id)\n)\n\n')
    op.execute("\nCREATE TABLE security_action_requests (\n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\trequester_user_id UUID NOT NULL, \n\tresource_id UUID NOT NULL, \n\tagent_id UUID, \n\taction VARCHAR(64) NOT NULL, \n\taction_hash VARCHAR(64) NOT NULL, \n\tpolicy_version INTEGER NOT NULL, \n\trisk VARCHAR(16) NOT NULL, \n\trequired_approvals INTEGER NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\texecuted_at TIMESTAMP WITH TIME ZONE, \n\tresult JSONB, \n\tCONSTRAINT pk_security_action_requests PRIMARY KEY (id), \n\tCONSTRAINT uq_security_action_requests_tenant_id UNIQUE (tenant_id, id), \n\tCONSTRAINT fk_security_action_requests_tenant_id_security_members FOREIGN KEY(tenant_id, requester_user_id) REFERENCES security_members (tenant_id, user_id), \n\tCONSTRAINT fk_security_action_requests_tenant_id_security_resources FOREIGN KEY(tenant_id, resource_id) REFERENCES security_resources (tenant_id, id), \n\tCONSTRAINT fk_security_action_requests_tenant_id_security_agents FOREIGN KEY(tenant_id, agent_id) REFERENCES security_agents (tenant_id, id), \n\tCONSTRAINT ck_security_action_requests_status CHECK (status IN ('pending','ready','rejected','executed','expired')), \n\tCONSTRAINT ck_security_action_requests_risk CHECK (risk IN ('low','medium','high','critical') AND required_approvals BETWEEN 0 AND 2), \n\tCONSTRAINT fk_security_action_requests_tenant_id_security_tenants FOREIGN KEY(tenant_id) REFERENCES security_tenants (id)\n)\n\n")
    op.execute('CREATE INDEX ix_security_action_requests_tenant_id ON security_action_requests (tenant_id)')
    op.execute("\nCREATE TABLE security_approval_decisions (\n\ttenant_id UUID NOT NULL, \n\trequest_id UUID NOT NULL, \n\tactor_id UUID NOT NULL, \n\tdecision VARCHAR(16) NOT NULL, \n\treason VARCHAR(1000) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_security_approval_decisions PRIMARY KEY (tenant_id, request_id, actor_id), \n\tCONSTRAINT fk_security_approval_decisions_tenant_id_security_actio_69e4 FOREIGN KEY(tenant_id, request_id) REFERENCES security_action_requests (tenant_id, id), \n\tCONSTRAINT fk_security_approval_decisions_tenant_id_security_members FOREIGN KEY(tenant_id, actor_id) REFERENCES security_members (tenant_id, user_id), \n\tCONSTRAINT ck_security_approval_decisions_decision CHECK (decision IN ('approve','reject'))\n)\n\n")

    op.execute("""
        CREATE FUNCTION security_append_event() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE prior text; next_sequence integer;
        BEGIN
            SELECT audit_head, audit_sequence + 1 INTO prior, next_sequence
              FROM security_tenants WHERE id = NEW.tenant_id FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'unknown security tenant'; END IF;
            NEW.sequence := next_sequence;
            NEW.previous_hash := prior;
            NEW.event_hash := encode(sha256(convert_to(prior || NEW.body::text, 'UTF8')), 'hex');
            UPDATE security_tenants SET audit_sequence = next_sequence, audit_head = NEW.event_hash
              WHERE id = NEW.tenant_id;
            RETURN NEW;
        END $$;
    """)
    op.execute("CREATE TRIGGER security_event_chain BEFORE INSERT ON security_events FOR EACH ROW EXECUTE FUNCTION security_append_event()")
    op.execute("""
        CREATE FUNCTION security_block_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'security evidence is immutable' USING ERRCODE = '55000'; END $$;
    """)
    for table in ('security_events', 'security_policy_versions', 'security_approval_decisions', 'security_resources'):
        op.execute(f"CREATE TRIGGER security_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION security_block_mutation()")
    op.execute("""
        CREATE FUNCTION security_member_binding() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'revoke memberships; never delete tenant ownership'; END IF;
            IF ROW(NEW.tenant_id, NEW.user_id, NEW.department_id) IS DISTINCT FROM ROW(OLD.tenant_id, OLD.user_id, OLD.department_id)
            THEN RAISE EXCEPTION 'tenant ownership and department binding are immutable'; END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("CREATE TRIGGER security_member_boundary BEFORE UPDATE OR DELETE ON security_members FOR EACH ROW EXECUTE FUNCTION security_member_binding()")
    op.execute("""
        CREATE FUNCTION security_action_binding() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (to_jsonb(NEW) - ARRAY['status','result','executed_at']) IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['status','result','executed_at'])
            THEN RAISE EXCEPTION 'approved action binding is immutable'; END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("CREATE TRIGGER security_action_immutable BEFORE UPDATE ON security_action_requests FOR EACH ROW EXECUTE FUNCTION security_action_binding()")


def downgrade():
    # Evidence retention is intentional. A destructive downgrade requires an operator
    # to export and remove the tenant data first; migrations never erase it silently.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM security_tenants) THEN
            RAISE EXCEPTION 'export and retain governance evidence before downgrade';
        END IF;
    END $$;""")
    op.drop_table('security_approval_decisions')
    op.drop_table('security_action_requests')
    op.drop_table('security_role_assignments')
    op.drop_table('security_agents')
    op.drop_table('security_resources')
    op.drop_table('security_members')
    op.drop_table('security_roles')
    op.drop_table('security_policy_versions')
    op.drop_table('security_events')
    op.drop_table('security_departments')
    op.drop_table('security_tenants')

    for name in ('security_action_binding', 'security_member_binding', 'security_block_mutation', 'security_append_event'):
        op.execute(f"DROP FUNCTION {name}()")
