"""Alembic environment. Migrations run automatically at startup (db.init_engine);
``alembic -c archivetv/migrations/alembic.ini revision --autogenerate -m "..."``
creates new ones."""

from alembic import context
from sqlalchemy import engine_from_config, pool

from archivetv import models  # noqa: F401  (register tables)
from archivetv.config import settings
from archivetv.db import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    url = config.get_main_option("sqlalchemy.url") or f"sqlite:///{settings.db_path}"
    engine = engine_from_config({"sqlalchemy.url": url}, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
