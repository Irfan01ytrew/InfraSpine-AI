"""Join independently developed module migrations with the shared application."""

revision = "0090_merge_module_heads"
down_revision = (
    "0089_company_intelligence_graph",
    "pi04_0001",
    "wf05_0001",
    "0085_governance_security",
    "0085_module07_recovery",
    "bc08_0001",
    "qa12_001_quality_evidence",
)
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
