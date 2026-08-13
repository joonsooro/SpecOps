"""add durable provider resource retention and release lifecycle"""

from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


_COLUMNS = (
    "cleanup_reason",
    "last_client_disconnected_at",
    "restart_grace_until",
    "workshop_complete_at",
    "cleanup_available_at",
    "cleanup_last_error_code",
)


def upgrade():
    connection = op.get_bind()
    existing = {
        item["name"]
        for item in sa.inspect(connection).get_columns("workshop_preparations")
    }
    for name in _COLUMNS:
        if name not in existing:
            op.add_column("workshop_preparations", sa.Column(name, sa.Text(), nullable=True))


def downgrade():
    connection = op.get_bind()
    existing = {
        item["name"]
        for item in sa.inspect(connection).get_columns("workshop_preparations")
    }
    with op.batch_alter_table("workshop_preparations") as batch:
        for name in reversed(_COLUMNS):
            if name in existing:
                batch.drop_column(name)
