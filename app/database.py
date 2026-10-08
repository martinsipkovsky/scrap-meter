"""SQLAlchemy engine, session factory and declarative base.

The app has full control over its schema: tables are created on startup
(see app.main.lifespan), columns added in later versions are migrated in by
``migrate_schema``, and all reads/writes go through these sessions.

Which database is used: the one an admin saved on the Database page
(``app.dbconfig``, a file in DATA_DIR) if it is reachable, otherwise
DATABASE_URL from the environment. A saved database that cannot be reached
never stops the app from starting; it is retried for about a minute (after an
update the database server may still be starting), then the app falls back and
the Database page says so.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import dbconfig
from .config import settings

log = logging.getLogger("cognex.database")


def make_engine(url: str) -> Engine:
    # sqlite needs check_same_thread=False because the poller runs in a
    # background thread; Postgres ignores the argument.
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


SAVED_DB_ATTEMPTS = 6
SAVED_DB_RETRY_DELAY = 5  # seconds between attempts (each also waits up to 5 s)


def _choose_url() -> tuple[str, str, str | None]:
    """(url, source, fallback_error). source is 'saved' or 'environment'."""
    if dbconfig.restore_missing():
        log.warning("database setting was missing from DATA_DIR; restored it from the database copy")
    saved = dbconfig.load()
    if saved:
        url = dbconfig.build_url(saved)
        for attempt in range(1, SAVED_DB_ATTEMPTS + 1):
            ok, err = dbconfig.test_url(url)
            if ok:
                return url, "saved", None
            log.warning("saved database unreachable (attempt %d of %d): %s", attempt, SAVED_DB_ATTEMPTS, err)
            if attempt < SAVED_DB_ATTEMPTS:
                time.sleep(SAVED_DB_RETRY_DELAY)
        log.warning("saved database unreachable, using DATABASE_URL: %s", err)
        return settings.database_url, "environment", err
    return settings.database_url, "environment", None


active_url, active_source, fallback_error = _choose_url()
engine = make_engine(active_url)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def migrate_schema(bind: Engine | None = None) -> list[str]:
    """Add columns that exist on the models but not yet in the database.

    ``create_all`` only creates missing tables; an existing install keeps its
    old tables, so new nullable/defaulted columns and new indexes are added
    here. Returns the list of "table.column" / "table.index" entries that
    were added.
    """
    bind = bind or engine
    added: list[str] = []
    insp = inspect(bind)
    with bind.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(dialect=bind.dialect)}'
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if isinstance(default, bool):
                    ddl += " DEFAULT " + ("TRUE" if default else "FALSE")
                elif isinstance(default, (int, float)):
                    ddl += f" DEFAULT {default}"
                elif isinstance(default, str):
                    ddl += " DEFAULT '" + default.replace("'", "''") + "'"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
            # indexes added to a model later (create_all skips existing tables)
            have = {i["name"] for i in insp.get_indexes(table.name)}
            for index in table.indexes:
                if index.name not in have:
                    index.create(conn)
                    added.append(f"{table.name}.{index.name}")
    for name in added:
        log.info("schema migration: added %s", name)
    return added


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
