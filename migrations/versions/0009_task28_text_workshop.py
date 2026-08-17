"""Add Task 28 text-authoritative Workshop state.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

from specops_workflow.persistence import TASK28_RUNTIME_TABLES
from specops_workflow.workshop_protocol_storage import TASK28_RUNTIME_TABLE_NAMES


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    for name in TASK28_RUNTIME_TABLE_NAMES:
        TASK28_RUNTIME_TABLES[name].create(connection, checkfirst=True)


def downgrade() -> None:
    connection = op.get_bind()
    for name in reversed(TASK28_RUNTIME_TABLE_NAMES):
        TASK28_RUNTIME_TABLES[name].drop(connection, checkfirst=True)
