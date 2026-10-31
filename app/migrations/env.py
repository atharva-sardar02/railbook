"""Alembic runs this for every command. It connects with the app's own engine (app/db.py), so
migrations use the same Vault credentials and SET ROLE as the API: the tables they create are
owned by railbook_owner."""

from alembic import context

from app.db import get_engine
from app.models import Base

# Autogenerate compares the database against these models to write a new migration.
target_metadata = Base.metadata


def run() -> None:
    # app/admin.py passes its own connection; the alembic CLI doesn't, so open one.
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _migrate(connection)
        return
    with get_engine().begin() as connection:
        _migrate(connection)


def _migrate(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


run()
