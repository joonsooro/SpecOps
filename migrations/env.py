from alembic import context
from sqlalchemy import engine_from_config, pool
from specops_workflow.persistence import metadata

config = context.config
target_metadata = metadata

def run_migrations_offline():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction(): context.run_migrations()

def run_migrations_online():
    connectable = engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        # SQLAlchemy 2 autobegins for PRAGMA; finish that transaction so
        # Alembic owns and commits the revision transaction (including its
        # alembic_version row) instead of rolling the stamp back on close.
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction(): context.run_migrations()

run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
