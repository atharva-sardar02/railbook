import json
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        pool_pre_ping=True,
    )
    if settings.db_credentials_file:
        creds_file = Path(settings.db_credentials_file)

        # Runs before every new DB connection. Vault gives out a new user when the old one hits
        # max_ttl and the agent rewrites the file; reading it here means new connections use the
        # new user. Old pooled connections fail pre-ping once Vault drops their user, and are
        # replaced.
        @event.listens_for(engine, "do_connect")
        def _current_credentials(_dialect, _record, _cargs, cparams):
            creds = json.loads(creds_file.read_text())
            cparams["user"] = creds["username"]
            cparams["password"] = creds["password"]

    if settings.db_role:
        role = settings.db_role

        @event.listens_for(engine, "connect")
        def _set_role(dbapi_conn, _record):
            with dbapi_conn.cursor() as cur:
                cur.execute(f'SET ROLE "{role}"')
            dbapi_conn.commit()

    return engine


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_session() -> Iterator[Session]:
    with _session_factory()() as session:
        yield session


def ping() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
