"""Backup files of all app data, and restoring them.

A backup is one gzip-compressed file of JSON lines, so it can be written and
read row by row however large the readings history is:

    {"format": "cognex-monitor-backup", "version": 1, "created_at": ..., "tables": {name: rows}}
    {"table": "users", "columns": ["id", "username", ...]}
    [1, "Admin", ...]
    ...
    {"end": true}

It is made with SQLAlchemy from the models (no pg_dump), so it restores into
whatever database the app uses. Restoring replaces every table inside one
transaction: if anything fails, the current data is left as it was.

Backup bookkeeping (when the last one was made, the FTP run results) lives in
a JSON file in DATA_DIR next to the database setting, so it survives restores
and database switches.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import os
import re
import tempfile
import threading
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Date, DateTime, func, select, text
from sqlalchemy.engine import Connection, Engine

from . import database
from .config import settings
from .database import Base

log = logging.getLogger("cognex.backup")

FORMAT = "cognex-monitor-backup"
VERSION = 1
NAME_RE = re.compile(r"^cognex-backup-\d{8}-\d{6}(-[a-z-]+)?\.json\.gz$")
_BATCH = 2000
# backups taken automatically before an import; older ones are deleted
KEEP_SAFETY = 5

# one backup/restore at a time (download, FTP run and import share it)
lock = threading.Lock()


class BackupError(ValueError):
    """The file is not a usable backup."""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def file_name(when: dt.datetime | None = None, suffix: str = "") -> str:
    when = (when or utcnow()).astimezone(dt.timezone.utc)
    return f"cognex-backup-{when:%Y%m%d-%H%M%S}{'-' + suffix if suffix else ''}.json.gz"


def data_dir() -> Path:
    return Path(settings.data_dir)


def backups_dir() -> Path:
    path = data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# Bookkeeping
# --------------------------------------------------------------------------- #


def _state_path() -> Path:
    return data_dir() / "backup_state.json"


def load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update_state(**changes) -> dict:
    state = load_state()
    state.update(changes)
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return state


def record_backup(kind: str, name: str, size: int) -> None:
    """Remember a backup that left the server (download or FTP upload)."""
    update_state(last_backup={"at": utcnow().isoformat(), "kind": kind, "file": name, "size": size})


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def _encode(value):
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    raise TypeError(f"cannot store {type(value).__name__} in a backup")


def _line(obj) -> bytes:
    return (json.dumps(obj, default=_encode, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def write_backup(path: Path, engine: Engine | None = None) -> dict:
    """Write every table to ``path``. Returns {table: rows}."""
    engine = engine or database.engine
    tables = Base.metadata.sorted_tables
    with engine.connect() as conn:
        if conn.dialect.name == "postgresql":
            # one consistent snapshot while the poller keeps writing
            conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with conn.begin():
            counts = {t.name: conn.execute(select(func.count()).select_from(t)).scalar_one() for t in tables}
            with gzip.open(path, "wb", compresslevel=6) as out:
                out.write(_line({
                    "format": FORMAT, "version": VERSION,
                    "created_at": utcnow().isoformat(), "tables": counts,
                }))
                for table in tables:
                    cols = [c.name for c in table.columns]
                    out.write(_line({"table": table.name, "columns": cols}))
                    result = conn.execution_options(stream_results=True).execute(select(table))
                    while batch := result.fetchmany(_BATCH):
                        out.write(b"".join(_line(list(r)) for r in batch))
                out.write(_line({"end": True}))
    return counts


def make_backup(directory: Path | None = None, suffix: str = "") -> Path:
    """Write a new backup file into ``directory`` (a temp dir by default)."""
    directory = Path(directory or tempfile.mkdtemp(prefix="cognex-backup-"))
    path = directory / file_name(suffix=suffix)
    write_backup(path)
    return path


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _lines(path: Path):
    try:
        with gzip.open(path, "rb") as f:
            for raw in f:
                if raw.strip():
                    yield json.loads(raw)
    except (OSError, EOFError, ValueError) as exc:
        raise BackupError(f"The file is not a readable Scrap Meter backup ({exc}).") from exc


def _header(first) -> dict:
    if not isinstance(first, dict) or first.get("format") != FORMAT:
        raise BackupError("The file is not a Scrap Meter backup.")
    if int(first.get("version", 0)) > VERSION:
        raise BackupError("The backup was made by a newer version of the app. Update the app first.")
    return first


def inspect(path: Path) -> dict:
    """Read the whole file and check it is complete. Returns a summary."""
    it = _lines(path)
    try:
        header = _header(next(it))
    except StopIteration:
        raise BackupError("The file is empty.") from None
    rows: dict[str, int] = {}
    current = None
    complete = False
    for item in it:
        if isinstance(item, dict) and "table" in item:
            current = item["table"]
            rows[current] = 0
        elif isinstance(item, dict) and item.get("end"):
            complete = True
        elif isinstance(item, list) and current is not None:
            rows[current] += 1
        else:
            raise BackupError("The backup file is damaged.")
    if not complete:
        raise BackupError("The backup file is incomplete (it was cut off).")
    known = {t.name for t in Base.metadata.sorted_tables}
    return {
        "created_at": header.get("created_at"),
        "tables": rows,
        "unknown_tables": sorted(set(rows) - known),
        "has_users": rows.get("users", 0) > 0,
    }


def reset_sequences(conn: Connection) -> None:
    """Keep new rows from colliding with ids that were copied or restored."""
    if conn.dialect.name != "postgresql":
        return
    for table in Base.metadata.sorted_tables:
        if "id" in table.c:
            conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table.name}), 0) + 1, false)"
            ))


def _converter(column):
    if isinstance(column.type, DateTime):
        def conv(v):
            return dt.datetime.fromisoformat(v) if isinstance(v, str) else v
        return conv
    if isinstance(column.type, Date):
        def conv_day(v):
            return dt.date.fromisoformat(v) if isinstance(v, str) else v
        return conv_day
    return None


def restore(path: Path, engine: Engine | None = None) -> dict:
    """Replace all data with the backup in ``path``. Returns {table: rows}.

    Everything happens in one transaction; on any error nothing is changed.
    """
    summary = inspect(path)  # refuse damaged files before touching anything
    if not summary["has_users"]:
        raise BackupError("The backup contains no user accounts, so nobody could sign in after importing it.")
    engine = engine or database.engine
    tables = Base.metadata.tables
    restored: dict[str, int] = {}
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # fail instead of waiting forever if another session holds a table
            conn.execute(text("SET LOCAL lock_timeout = '30s'"))
            names = ", ".join(t.name for t in Base.metadata.sorted_tables)
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        else:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())

        table = cols = None
        batch: list[dict] = []

        def flush():
            if batch:
                conn.execute(table.insert(), batch)
                restored[table.name] += len(batch)
                batch.clear()

        it = _lines(path)
        next(it)  # header, checked by inspect()
        for item in it:
            if isinstance(item, dict):
                if table is not None:
                    flush()
                name = item.get("table")
                table = tables.get(name) if name else None
                if table is None:
                    if name:
                        log.warning("backup table %s is not used by this version, skipped", name)
                    cols = None
                    continue
                restored[table.name] = 0
                # columns this version no longer has are dropped; columns
                # added since get their defaults
                cols = [(i, c, _converter(table.c[c])) for i, c in enumerate(item["columns"]) if c in table.c]
            elif cols is not None:
                row = {}
                for i, name, conv in cols:
                    v = item[i]
                    row[name] = conv(v) if conv and v is not None else v
                batch.append(row)
                if len(batch) >= _BATCH:
                    flush()
        if table is not None:
            flush()
        reset_sequences(conn)
    # a backup from 1.4 or older: its devices become stations; from 1.6 or
    # older: its station cycle times move to the jobs
    from . import jobs, stations

    stations.upgrade(engine)
    jobs.upgrade(engine)
    return restored


def prune(directory: Path, keep: int, pattern: re.Pattern = NAME_RE) -> None:
    files = sorted(p for p in directory.iterdir() if p.is_file() and pattern.match(p.name))
    for p in files[:-keep] if keep > 0 else files:
        try:
            p.unlink()
        except OSError:
            pass


def safety_backups() -> list[dict]:
    files = sorted((p for p in backups_dir().iterdir() if p.is_file() and NAME_RE.match(p.name)), reverse=True)
    return [
        {"name": p.name, "size": p.stat().st_size,
         "at": dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc).isoformat()}
        for p in files
    ]
