"""add durable V0 Workshop preparation and Analyzer scheduling"""

from alembic import op

from specops_workflow.persistence import V0_RUNTIME_TABLES
from specops_workflow.workshop_protocol_storage import V0_RUNTIME_TABLE_NAMES


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    for name in V0_RUNTIME_TABLE_NAMES:
        V0_RUNTIME_TABLES[name].create(connection, checkfirst=True)


def downgrade():
    connection = op.get_bind()
    for name in reversed(V0_RUNTIME_TABLE_NAMES):
        V0_RUNTIME_TABLES[name].drop(connection, checkfirst=True)
