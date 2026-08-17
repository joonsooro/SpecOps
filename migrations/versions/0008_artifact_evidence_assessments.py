"""retain exact evidence assessments outside canonical artifacts"""

from alembic import op

from specops_workflow.persistence import EVIDENCE_ASSESSMENT_TABLES


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    for table in EVIDENCE_ASSESSMENT_TABLES.values():
        table.create(connection, checkfirst=True)
    for table_name in EVIDENCE_ASSESSMENT_TABLES:
        op.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table_name}_no_update "
            f"BEFORE UPDATE ON {table_name} "
            "BEGIN SELECT RAISE(ABORT, 'EVIDENCE_ASSESSMENT_APPEND_ONLY'); END"
        )
        op.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table_name}_no_delete "
            f"BEFORE DELETE ON {table_name} "
            "BEGIN SELECT RAISE(ABORT, 'EVIDENCE_ASSESSMENT_APPEND_ONLY'); END"
        )


def downgrade():
    for table_name in reversed(tuple(EVIDENCE_ASSESSMENT_TABLES)):
        op.execute(f"DROP TRIGGER IF EXISTS {table_name}_no_delete")
        op.execute(f"DROP TRIGGER IF EXISTS {table_name}_no_update")
    connection = op.get_bind()
    for table in reversed(tuple(EVIDENCE_ASSESSMENT_TABLES.values())):
        table.drop(connection, checkfirst=True)
