"""add Artifact Quality Audit Protocol 1.0.0 Foundation evidence"""

from alembic import op

from specops_workflow.persistence import ARTIFACT_QUALITY_TABLES


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    for table in ARTIFACT_QUALITY_TABLES.values():
        table.create(connection, checkfirst=True)
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS artifact_quality_admitted_no_update "
        "BEFORE UPDATE ON workshop_artifact_quality_audits "
        "WHEN OLD.state = 'ADMITTED' "
        "BEGIN SELECT RAISE(ABORT, 'ARTIFACT_QUALITY_AUDIT_APPEND_ONLY'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS artifact_quality_admitted_no_delete "
        "BEFORE DELETE ON workshop_artifact_quality_audits "
        "WHEN OLD.state = 'ADMITTED' "
        "BEGIN SELECT RAISE(ABORT, 'ARTIFACT_QUALITY_AUDIT_APPEND_ONLY'); END"
    )


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS artifact_quality_admitted_no_delete")
    op.execute("DROP TRIGGER IF EXISTS artifact_quality_admitted_no_update")
    connection = op.get_bind()
    for table in reversed(tuple(ARTIFACT_QUALITY_TABLES.values())):
        table.drop(connection, checkfirst=True)
