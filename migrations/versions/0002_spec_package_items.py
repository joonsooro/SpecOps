"""add independently governed Spec Package Item storage"""

from alembic import op
from sqlalchemy import inspect

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


ADDITIONS = {
    "ambiguity_findings": (("item_binding", "TEXT"),),
    "spec_package_versions": (("items", "TEXT"), ("item_governance", "TEXT")),
    "projection_plan_versions": (("source_item_bindings", "TEXT"),),
    "projection_items": (("source_item_bindings", "TEXT"),),
}


def upgrade():
    connection = op.get_bind()
    inspector = inspect(connection)
    for table_name, columns in ADDITIONS.items():
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name, sql_type in columns:
            if column_name not in existing:
                op.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {sql_type}")


def downgrade():
    connection = op.get_bind()
    inspector = inspect(connection)
    for table_name, columns in reversed(tuple(ADDITIONS.items())):
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name, _ in reversed(columns):
            if column_name in existing:
                op.drop_column(table_name, column_name)
