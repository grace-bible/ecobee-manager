from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_connect_args = {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}
engine = create_engine(_settings.database_url, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


MIGRATIONS = Path(__file__).parent / "migrations"
BASELINE = "0001"  # the schema v0.1 built with create_all


def alembic_config():
    from alembic.config import Config

    cfg = Config(str(MIGRATIONS / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS))
    return cfg


def init_db() -> None:
    """Bring the schema to head. Runs at every start; a no-op when already current."""
    from alembic import command

    cfg = alembic_config()
    tables = set(inspect(engine).get_table_names())
    if "zone" in tables and "alembic_version" not in tables:
        command.stamp(cfg, BASELINE)  # a dev DB from before migrations existed
    command.upgrade(cfg, "head")


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s
