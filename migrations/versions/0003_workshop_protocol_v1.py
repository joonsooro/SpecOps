"""add Workshop Interaction Protocol 1.0.0 Foundation state"""

from alembic import op

from specops_workflow.persistence import WORKSHOP_PROTOCOL_TABLES


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    for table in WORKSHOP_PROTOCOL_TABLES.values():
        table.create(connection, checkfirst=True)


def downgrade():
    connection = op.get_bind()
    for table in reversed(tuple(WORKSHOP_PROTOCOL_TABLES.values())):
        table.drop(connection, checkfirst=True)
