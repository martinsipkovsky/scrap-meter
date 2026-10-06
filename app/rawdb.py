"""Raw access to the app's tables for admins (the Raw data tab).

* ``tables`` / ``rows``: every table of the app's models, page by page, with
  search, filters and sorting.
* ``update_row`` / ``insert_row`` / ``delete_row``: type-checked changes of
  one row, each written to the audit log (DbAuditLog: who, when, table, row,
  old and new values) in the same transaction; ``undo`` reverses one entry.
* ``run_select``: a read-only SQL box. One SELECT (or WITH ... SELECT)
  statement, run in a read-only transaction, at most MAX_SQL_ROWS rows.

Secrets never leave the server: password hashes and the notification
providers' settings (tokens) are hidden and can't be edited, passwords in
JSON settings (a device's OPC UA login) are masked, and the SQL box refuses
the tables that hold secrets and masks hash-like values. The audit log itself
is read-only.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from sqlalchemy import (JSON, Boolean, DateTime, Float, Integer, Numeric, String, Table, Text, and_, cast, func,
                        or_, select, text)
from sqlalchemy.orm import Session

from .database import Base
from .models import DbAuditLog

HIDDEN = "•••• hidden"
MASK = "********"
# whole columns that are never shown or edited
SECRET_COLUMNS = {("users", "password_hash"), ("notification_providers", "config")}
# keys masked inside JSON values
SECRET_KEYS = {"password", "bot_token", "token", "api_key", "secret"}
# tables where rows can't be added here (they need a secret, or are the log)
NO_INSERT = {"users": "Add users on the Users tab", "notification_providers": "Add providers on the Notifications tab",
             DbAuditLog.__tablename__: "The audit log is read-only"}
READ_ONLY = {DbAuditLog.__tablename__}
PAGE_SIZES = (25, 50, 100, 200)
MAX_SQL_ROWS = 500
OPS = ("=", "!=", "<", "<=", ">", ">=", "contains", "starts", "empty", "not_empty")
# columns that label a row in a foreign-key dropdown, best first
_LABELS = ("name", "username", "station_name", "job_name", "key", "keyword")


class RawError(ValueError):
    """A request the raw editor refuses; the message is shown to the user."""


# --------------------------------------------------------------------------- #
# Tables and columns
# --------------------------------------------------------------------------- #


def _tables() -> dict[str, Table]:
    return {t.name: t for t in Base.metadata.sorted_tables}


def table(name: str) -> Table:
    t = _tables().get(name)
    if t is None:
        raise RawError(f"Unknown table '{name}'")
    return t


def _pk(t: Table):
    cols = list(t.primary_key.columns)
    return cols[0] if len(cols) == 1 else None


def kind(col) -> str:
    """number / integer / boolean / datetime / json / text"""
    ty = col.type
    if isinstance(ty, Boolean):
        return "boolean"
    if isinstance(ty, Integer):
        return "integer"
    if isinstance(ty, (Float, Numeric)):
        return "number"
    if isinstance(ty, DateTime):
        return "datetime"
    if isinstance(ty, JSON):
        return "json"
    return "text"


def _hidden(t: Table, col) -> bool:
    return (t.name, col.name) in SECRET_COLUMNS


def _fk(col) -> dict | None:
    for fk in col.foreign_keys:
        return {"table": fk.column.table.name, "column": fk.column.name}
    return None


def columns(t: Table) -> list[dict]:
    pk = _pk(t)
    out = []
    for c in t.columns:
        out.append({
            "name": c.name, "kind": kind(c), "nullable": bool(c.nullable), "primary_key": c is pk,
            "hidden": _hidden(t, c), "foreign_key": _fk(c),
            "max_length": getattr(c.type, "length", None) if isinstance(c.type, String) and not isinstance(c.type, Text) else None,
            "editable": t.name not in READ_ONLY and not _hidden(t, c) and c is not pk,
        })
    return out


def tables(db: Session) -> list[dict]:
    out = []
    for name, t in sorted(_tables().items()):
        out.append({"name": name, "rows": db.execute(select(func.count()).select_from(t)).scalar_one(),
                    "primary_key": _pk(t).name if _pk(t) is not None else None,
                    "read_only": name in READ_ONLY, "can_insert": name not in NO_INSERT,
                    "insert_note": NO_INSERT.get(name)})
    return out


# --------------------------------------------------------------------------- #
# Values in and out
# --------------------------------------------------------------------------- #


def _mask_json(value):
    if isinstance(value, dict):
        return {k: (MASK if k.lower() in SECRET_KEYS and v not in (None, "") else _mask_json(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_json(v) for v in value]
    return value


def _unmask_json(new, old):
    """A masked secret in an edited JSON value keeps the stored one."""
    if isinstance(new, dict):
        old = old if isinstance(old, dict) else {}
        return {k: (old.get(k) if v == MASK and k.lower() in SECRET_KEYS else _unmask_json(v, old.get(k)))
                for k, v in new.items()}
    if isinstance(new, list) and isinstance(old, list) and len(new) == len(old):
        return [_unmask_json(n, o) for n, o in zip(new, old)]
    return new


def _out(t: Table, col, value):
    if _hidden(t, col):
        return HIDDEN if value not in (None, "") else None
    if isinstance(value, dt.datetime):
        return (value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if kind(col) == "json":
        return _mask_json(value)
    return value


def row_out(t: Table, row) -> dict:
    m = row._mapping
    return {c.name: _out(t, c, m[c]) for c in t.columns}


def convert(col, raw: Any):
    """A value from the editor as the column's type. Raises RawError."""
    what = f"{col.name}: "
    if raw is None or (isinstance(raw, str) and raw.strip() == "" and kind(col) != "text"):
        if not col.nullable:
            raise RawError(what + "a value is needed")
        return None
    k = kind(col)
    try:
        if k == "boolean":
            if isinstance(raw, bool):
                return raw
            s = str(raw).strip().lower()
            if s in ("true", "1", "yes", "on"):
                return True
            if s in ("false", "0", "no", "off"):
                return False
            raise ValueError
        if k == "integer":
            if isinstance(raw, bool):
                raise ValueError
            f = float(raw)
            if f != int(f):
                raise ValueError
            return int(f)
        if k == "number":
            return float(raw)
        if k == "datetime":
            t = raw if isinstance(raw, dt.datetime) else dt.datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
            return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
        if k == "json":
            return json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        raise RawError(what + {"boolean": "true or false", "integer": "a whole number", "number": "a number",
                               "datetime": "a date and time, e.g. 2026-10-06T14:30",
                               "json": "valid JSON"}[k]) from None
    s = str(raw)
    length = getattr(col.type, "length", None)
    if length and len(s) > length:
        raise RawError(what + f"at most {length} characters")
    return s


def _audit_value(t: Table, col, value):
    """How a value is kept in the audit log (secrets stay out of it)."""
    if _hidden(t, col):
        return value  # needed to undo a delete; never shown (see audit_out)
    if isinstance(value, dt.datetime):
        return (value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)).isoformat()
    return value


# --------------------------------------------------------------------------- #
# Reading rows
# --------------------------------------------------------------------------- #


def _filter(t: Table, f: dict):
    col = t.columns.get(f.get("column") or "")
    op = f.get("op")
    if col is None or _hidden(t, col):
        raise RawError("Pick a column to filter on")
    if op not in OPS:
        raise RawError(f"Unknown filter '{op}'")
    if op == "empty":
        return or_(col.is_(None), cast(col, String) == "") if kind(col) == "text" else col.is_(None)
    if op == "not_empty":
        return and_(col.isnot(None), cast(col, String) != "") if kind(col) == "text" else col.isnot(None)
    value = f.get("value")
    if op in ("contains", "starts"):
        pattern = str(value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{pattern}%" if op == "contains" else f"{pattern}%"
        return func.lower(cast(col, String)).like(pattern.lower(), escape="\\")
    v = convert(col, value) if kind(col) != "json" else str(value)
    target = cast(col, String) if kind(col) == "json" else col
    return {"=": target == v, "!=": target != v, "<": target < v, "<=": target <= v,
            ">": target > v, ">=": target >= v}[op]


def rows(db: Session, name: str, page: int = 1, size: int = 50, sort: str | None = None, desc: bool = False,
         q: str | None = None, filters: list[dict] | None = None) -> dict:
    t = table(name)
    size = size if size in PAGE_SIZES else 50
    page = max(1, page)
    conds = [_filter(t, f) for f in (filters or [])]
    if q and q.strip():
        like = "%" + q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conds.append(or_(*(func.lower(cast(c, String)).like(like, escape="\\")
                           for c in t.columns if not _hidden(t, c) and kind(c) != "json")))
    where = and_(*conds) if conds else None
    total_q = select(func.count()).select_from(t)
    stmt = select(t)
    if where is not None:
        total_q, stmt = total_q.where(where), stmt.where(where)
    pk = _pk(t)
    order = t.columns.get(sort) if sort else None
    if order is None or _hidden(t, order):
        order = pk if pk is not None else list(t.columns)[0]
        desc = desc if sort else True  # newest first by default
    stmt = stmt.order_by(order.desc() if desc else order.asc())
    if pk is not None and order is not pk:
        stmt = stmt.order_by(pk.desc())
    total = db.execute(total_q).scalar_one()
    data = db.execute(stmt.limit(size).offset((page - 1) * size)).all()
    return {"table": name, "columns": columns(t), "rows": [row_out(t, r) for r in data], "total": total,
            "page": page, "size": size, "sort": order.name, "desc": desc,
            "primary_key": pk.name if pk is not None else None}


def fk_options(db: Session, name: str, column: str, limit: int = 500) -> list[dict]:
    t = table(name)
    col = t.columns.get(column)
    fk = _fk(col) if col is not None else None
    if fk is None:
        raise RawError(f"{column} is not a reference to another table")
    target = table(fk["table"])
    key = target.columns[fk["column"]]
    label = next((target.columns[c] for c in _LABELS if c in target.columns), None)
    stmt = select(key, label) if label is not None else select(key)
    out = []
    for r in db.execute(stmt.order_by(key).limit(limit)).all():
        out.append({"value": r[0], "label": f"{r[0]} · {r[1]}" if label is not None else str(r[0])})
    return out


# --------------------------------------------------------------------------- #
# Changes, with the audit log
# --------------------------------------------------------------------------- #


def _writable(t: Table):
    if t.name in READ_ONLY:
        raise RawError(NO_INSERT.get(t.name, "This table is read-only"))
    pk = _pk(t)
    if pk is None:
        raise RawError("This table has no single primary key, so its rows can't be edited here")
    return pk


def _key(pk, raw):
    try:
        return convert(pk, raw)
    except RawError:
        raise RawError("Unknown row") from None


def _get(db: Session, t: Table, pk, key):
    row = db.execute(select(t).where(pk == key)).first()
    if row is None:
        raise RawError("The row is not there any more")
    return row


def _log(db: Session, user: str, t: Table, key, action: str, old: dict | None, new: dict | None,
         undo_of: int | None = None) -> DbAuditLog:
    entry = DbAuditLog(username=user, table_name=t.name, row_key=str(key), action=action,
                       old_values=old, new_values=new, undo_of=undo_of)
    db.add(entry)
    return entry


def _values(t: Table, values: dict, old_row=None) -> dict:
    out = {}
    for name, raw in (values or {}).items():
        col = t.columns.get(name)
        if col is None:
            raise RawError(f"Unknown column '{name}'")
        if _hidden(t, col):
            raise RawError(f"{name} is hidden and can't be changed here")
        if col.primary_key and old_row is not None:
            raise RawError(f"{name} is the row's key and can't be changed")
        v = convert(col, raw)
        if kind(col) == "json" and old_row is not None:
            v = _unmask_json(v, old_row._mapping[col])
        out[name] = v
    return out


def update_row(db: Session, user: str, name: str, key_raw, values: dict) -> dict:
    t = table(name)
    pk = _writable(t)
    key = _key(pk, key_raw)
    row = _get(db, t, pk, key)
    new = _values(t, values, row)
    m = row._mapping
    changed = {k: v for k, v in new.items()
               if _audit_value(t, t.columns[k], m[t.columns[k]]) != _audit_value(t, t.columns[k], v)}
    if not changed:
        return row_out(t, row)
    db.execute(t.update().where(pk == key).values(**changed))
    _log(db, user, t, key, "update",
         {k: _audit_value(t, t.columns[k], m[t.columns[k]]) for k in changed},
         {k: _audit_value(t, t.columns[k], v) for k, v in changed.items()})
    db.commit()
    return row_out(t, _get(db, t, pk, key))


def insert_row(db: Session, user: str, name: str, values: dict) -> dict:
    t = table(name)
    pk = _writable(t)
    if t.name in NO_INSERT:
        raise RawError(NO_INSERT[t.name])
    vals = _values(t, {k: v for k, v in (values or {}).items() if not (t.columns.get(k) is pk and v in (None, ""))})
    res = db.execute(t.insert().values(**vals))
    key = vals.get(pk.name)
    if key is None:
        key = res.inserted_primary_key[0]
    row = _get(db, t, pk, key)
    _log(db, user, t, key, "insert", None, {c.name: _audit_value(t, c, row._mapping[c]) for c in t.columns})
    db.commit()
    return row_out(t, row)


def delete_row(db: Session, user: str, name: str, key_raw, current_user_id: int | None = None) -> None:
    t = table(name)
    pk = _writable(t)
    key = _key(pk, key_raw)
    if t.name == "users" and key == current_user_id:
        raise RawError("You can't delete your own account")
    row = _get(db, t, pk, key)
    old = {c.name: _audit_value(t, c, row._mapping[c]) for c in t.columns}
    db.execute(t.delete().where(pk == key))
    _log(db, user, t, key, "delete", old, None)
    db.commit()


def _audit_show(t: Table | None, values: dict | None):
    if values is None:
        return None
    out = {}
    for k, v in values.items():
        col = t.columns.get(k) if t is not None else None
        if col is not None and _hidden(t, col):
            out[k] = HIDDEN if v not in (None, "") else None
        elif col is not None and kind(col) == "json":
            out[k] = _mask_json(v)
        else:
            out[k] = v
    return out


def audit_out(e: DbAuditLog) -> dict:
    t = _tables().get(e.table_name)
    return {"id": e.id, "at": (e.created_at if e.created_at.tzinfo else e.created_at.replace(tzinfo=dt.timezone.utc)),
            "user": e.username, "table": e.table_name, "row": e.row_key, "action": e.action,
            "old": _audit_show(t, e.old_values), "new": _audit_show(t, e.new_values),
            "undo_of": e.undo_of, "undone_by": e.undone_by,
            "can_undo": e.action in ("update", "insert", "delete") and e.undone_by is None}


def audit(db: Session, limit: int = 100, table_name: str | None = None) -> list[dict]:
    q = db.query(DbAuditLog)
    if table_name:
        q = q.filter(DbAuditLog.table_name == table_name)
    return [audit_out(e) for e in q.order_by(DbAuditLog.id.desc()).limit(min(limit, 1000))]


def _restore_value(t: Table, name: str, v):
    col = t.columns.get(name)
    if col is None:
        return None, False
    if v is not None and kind(col) == "datetime" and isinstance(v, str):
        v = dt.datetime.fromisoformat(v)
    return v, True


def undo(db: Session, user: str, entry_id: int) -> dict:
    e = db.get(DbAuditLog, entry_id)
    if e is None:
        raise RawError("Unknown change")
    if e.undone_by is not None:
        raise RawError("This change was undone already")
    if e.action not in ("update", "insert", "delete"):
        raise RawError("An undo can't be undone; change the value again instead")
    t = table(e.table_name)
    pk = _writable(t)
    key = _key(pk, e.row_key)
    if e.action == "update":
        row = _get(db, t, pk, key)
        m = row._mapping
        now = {k: _audit_value(t, t.columns[k], m[t.columns[k]]) for k in e.new_values if k in t.columns}
        if now != {k: v for k, v in e.new_values.items() if k in t.columns}:
            raise RawError("The row was changed again since; undo the later change first")
        back = {}
        for k, v in e.old_values.items():
            v, ok = _restore_value(t, k, v)
            if ok:
                back[k] = v
        db.execute(t.update().where(pk == key).values(**back))
        entry = _log(db, user, t, key, "undo", e.new_values, e.old_values, undo_of=e.id)
    elif e.action == "insert":
        _get(db, t, pk, key)
        db.execute(t.delete().where(pk == key))
        entry = _log(db, user, t, key, "undo", e.new_values, None, undo_of=e.id)
    else:
        if db.execute(select(pk).where(pk == key)).first() is not None:
            raise RawError("A row with the same key exists again, so the deleted row can't be put back")
        back = {}
        for k, v in e.old_values.items():
            v, ok = _restore_value(t, k, v)
            if ok:
                back[k] = v
        db.execute(t.insert().values(**back))
        entry = _log(db, user, t, key, "undo", None, e.old_values, undo_of=e.id)
    db.flush()
    e.undone_by = entry.id
    db.commit()
    if e.action == "delete" and db.bind.dialect.name == "postgresql":
        from .backup import reset_sequences  # the row came back with its old id

        with db.bind.begin() as conn:
            reset_sequences(conn)
    return audit_out(entry)


# --------------------------------------------------------------------------- #
# Read-only SQL
# --------------------------------------------------------------------------- #

# tables that hold secrets, and server functions that reach outside the data
_SQL_DENY = re.compile(
    r"\b(password_hash|notification_providers|app_settings|whatsmeow_\w*|pg_authid|pg_shadow|pg_user_mappings?|"
    r"pg_read_file|pg_read_binary_file|pg_ls_\w+|pg_stat_file|lo_\w+|dblink\w*|pg_terminate_backend|"
    r"pg_cancel_backend|pg_reload_conf|pg_sleep\w*|set_config|current_setting|pg_file_\w+|pg_logdir_ls|"
    r"copy|into|load_extension|readfile|writefile|attach|pragma)\b", re.I)
_HASH = re.compile(r"^\$(pbkdf2[-\w]*|2[aby]?|argon2\w*|scrypt)\$")


def _strip_sql(sql: str) -> str:
    """The statement without comments and string literals (for the checks)."""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def check_select(sql: str) -> str:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise RawError("Type a SELECT query")
    bare = _strip_sql(sql)
    if ";" in bare:
        raise RawError("Only one statement at a time")
    if not re.match(r"^\s*(select|with)\b", bare, re.I):
        raise RawError("Only SELECT queries can run here; change data in the table grid, so the change is logged")
    if re.search(r"\b(insert|update|delete|merge|alter|drop|create|truncate|grant|revoke|vacuum|reindex|call|do|"
                 r"execute|prepare|listen|notify|lock|refresh|security|reset|set)\b", bare, re.I):
        raise RawError("Only reading is allowed here; change data in the table grid, so the change is logged")
    m = _SQL_DENY.search(bare)
    if m:
        raise RawError(f"'{m.group(0)}' is not available here (it holds secrets or reaches outside the data)")
    if '"' in bare:
        raise RawError("Quoted names are not allowed here; use plain table and column names")
    return sql


def _cell(v):
    if isinstance(v, str) and _HASH.match(v):
        return HIDDEN
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        return _mask_json(v)
    if isinstance(v, (bytes, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.dumps(_mask_json(json.loads(v)))
        except ValueError:
            return v
    if v is None or isinstance(v, (int, float, bool, str)):
        return v
    return str(v)


def run_select(db: Session, sql: str) -> dict:
    sql = check_select(sql)
    db.rollback()  # start a fresh transaction, so it can be made read-only
    conn = db.connection()
    dialect = conn.dialect.name
    try:
        if dialect == "postgresql":
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout = 10000"))
        elif dialect == "sqlite":
            conn.exec_driver_sql("PRAGMA query_only = ON")
        try:
            res = conn.execute(text(sql))
        except Exception as exc:  # noqa: BLE001 - the database's message is the answer
            msg = str(getattr(exc, "orig", exc)).strip().splitlines()[0]
            raise RawError(f"The database says: {msg}") from None
        cols = list(res.keys())
        hidden = {i for i, c in enumerate(cols) if c.lower() in ("password_hash",)}
        data = res.fetchmany(MAX_SQL_ROWS + 1)
    finally:
        if dialect == "sqlite":
            conn.exec_driver_sql("PRAGMA query_only = OFF")
        db.rollback()
    out = [[HIDDEN if i in hidden else _cell(v) for i, v in enumerate(r)] for r in data[:MAX_SQL_ROWS]]
    return {"columns": cols, "rows": out, "truncated": len(data) > MAX_SQL_ROWS, "limit": MAX_SQL_ROWS}
