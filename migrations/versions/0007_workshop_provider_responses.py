"""track every stored post-bootstrap provider Response for cleanup"""

from alembic import op

from specops_workflow.persistence import V0_RUNTIME_TABLES


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    V0_RUNTIME_TABLES["workshop_provider_responses"].create(
        op.get_bind(), checkfirst=True
    )


def downgrade():
    V0_RUNTIME_TABLES["workshop_provider_responses"].drop(
        op.get_bind(), checkfirst=True
    )
