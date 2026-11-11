"""Alembic environment. Runs against the app's own engine (DATABASE_URL).

New migration after changing app/models.py:
    alembic -c app/migrations/alembic.ini revision --autogenerate -m "what changed"
"""

from alembic import context

from app import models  # noqa: F401  (register tables)
from app.db import Base, engine

target_metadata = Base.metadata


def run() -> None:
    conn = context.config.attributes.get("connection")
    if conn is not None:
        _run(conn)
        return
    with engine.begin() as conn:
        _run(conn)


def _run(conn) -> None:
    context.configure(connection=conn, target_metadata=target_metadata,
                      render_as_batch=conn.dialect.name == "sqlite", compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


run()
