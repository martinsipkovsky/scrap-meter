"""Chat commands: a WhatsApp group message such as "!status" or "!status Line 1"
is answered with live figures from the app.

* A message is a command only when it starts with the prefix ("!" by
  default, changeable on the Notifications page), so normal chat never
  triggers anything.
* The word after the prefix is the command's keyword. Anything after it
  narrows the reply to the devices whose name contains it.
* Each command lists the groups it answers in (none = every group the linked
  phone is in). Private chats are never answered.
* "help" is built in (unless a command with that keyword exists) and lists the
  commands allowed in that group.
* Every handled command is logged (CommandLog), also when it was refused.

A reply is the command's header, one line per device and its footer. Their
{placeholders} are listed in PLACEHOLDERS; an unknown one stays as typed.
"""
from __future__ import annotations

import datetime as dt
import logging
import string
import threading
import time

from sqlalchemy.orm import Session

from . import production, scrap_stats, settings_store
from .models import ChatCommand, CommandLog, CounterState, Device, Reading, utcnow

log = logging.getLogger("cognex.commands")

PREFIX_KEY = "command_prefix"
DEFAULT_PREFIX = "!"
SEEDED_KEY = "commands_seeded"
MIN_INTERVAL = 3  # seconds between two answers in the same group
MAX_REPLY = 4000

PERIODS = {
    "dashboard": "Counters shown on the dashboard (current job, since the last reset)",
    "today": "Today, production time only (like Scrap statistics)",
    "hours": "The last N hours",
}

PLACEHOLDERS = {
    "line": {
        "device": "device name ({camera} works too)",
        "state": "▶ in production / ⏸ idle / ⏹ stopped",
        "online": "online / offline",
        "job": "current job",
        "pass": "OK parts in the period",
        "fail": "NOK parts in the period",
        "total": "OK + NOK",
        "scrap": "scrap % in the period",
        "last_data": "time of the last reading",
    },
    "header / footer": {
        "date": "today's date", "time": "current time", "period": "the period in words",
        "devices": "number of devices in the reply ({cameras} works too)",
        "in_production": "how many are in production",
        "total_pass": "OK parts, devices in the totals", "total_fail": "NOK parts, devices in the totals",
        "total": "all parts in the totals", "total_scrap": "scrap %, devices in the totals",
    },
}

DEFAULT_STATUS = {
    "keyword": "status",
    "description": "Production state, OK / NOK and scrap of every device",
    "period": "dashboard",
    "header": "📊 Status {date} {time}",
    "line": "{device}: {state}, job {job}\n   OK {pass} · NOK {fail} · scrap {scrap}",
    "footer": "Total: OK {total_pass} · NOK {total_fail} · scrap {total_scrap}",
}

# added to the line of a device that is excluded from the statistics by default
NOT_IN_TOTALS = " (not in totals)"

_STATE_TEXT = {production.RUNNING: "▶ in production", production.IDLE: "⏸ idle", production.STOPPED: "⏹ stopped"}

_last_answer: dict[str, float] = {}
_rate_lock = threading.Lock()


# ---- settings ---------------------------------------------------------------
def get_prefix() -> str:
    value = settings_store.load(PREFIX_KEY)
    return value if isinstance(value, str) and value else DEFAULT_PREFIX


def valid_prefix(prefix: str) -> bool:
    """1-3 characters, the first one punctuation (the WhatsApp client only
    passes on messages that start with one)."""
    return 1 <= len(prefix) <= 3 and not prefix[0].isalnum() and not any(c.isspace() for c in prefix)


def set_prefix(prefix: str) -> None:
    settings_store.save(PREFIX_KEY, prefix)


def seed_defaults(db: Session) -> None:
    """Add the ready-made "status" command once (a deleted one stays deleted)."""
    if settings_store.load(SEEDED_KEY):
        return
    if not db.query(ChatCommand).count():
        from .backup_ftp import load as ftp_settings

        # times in the reply: the time zone of the browser that saved the FTP
        # backup settings is the best guess; saving the command sets the editor's
        tz = ftp_settings().get("timezone") or "UTC"
        db.add(ChatCommand(**DEFAULT_STATUS, timezone=tz, group_ids=[]))
        db.commit()
    settings_store.save(SEEDED_KEY, True)


# ---- figures ----------------------------------------------------------------
def _pct(fail: int, total: int) -> str:
    return f"{fail / total * 100:.1f}%" if total else "—"


def _window_counts(db: Session, device: Device, start: dt.datetime) -> tuple[int, int]:
    """OK / NOK made since ``start``: differences of consecutive readings of
    the same job (like the camera view's chart)."""
    q = db.query(Reading.job_name, Reading.total_pass, Reading.total_fail).filter(Reading.device_id == device.id)
    prev = q.filter(Reading.created_at < start).order_by(Reading.created_at.desc(), Reading.id.desc()).first()
    ok = nok = 0
    for r in q.filter(Reading.created_at >= start).order_by(Reading.created_at.asc(), Reading.id.asc()):
        if prev is not None and prev.job_name == r.job_name:
            ok += max(r.total_pass - prev.total_pass, 0)
            nok += max(r.total_fail - prev.total_fail, 0)
        prev = r
    return ok, nok


def _local(t: dt.datetime | None, tz: dt.tzinfo) -> str:
    if t is None:
        return "never"
    t = t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t
    return t.astimezone(tz).strftime("%H:%M")


def render(db: Session, cmd: ChatCommand, camera_filter: str = "", now: dt.datetime | None = None) -> str:
    now = now or utcnow()
    tz = scrap_stats.zone(cmd.timezone)
    devices = db.query(Device).order_by(Device.name).all()
    if camera_filter:
        f = camera_filter.lower()
        devices = [d for d in devices if f in d.name.lower()]
        if not devices:
            return f"No device matches '{camera_filter}'."

    if cmd.period == "today":
        today = now.astimezone(tz).date()
        stats = {r["camera_id"]: r for r in scrap_stats.compute(db, today, today, tz)["per_camera"]}
        period = "today (production time)"
    elif cmd.period == "hours":
        hours = max(1, cmd.hours or 8)
        start = now - dt.timedelta(hours=hours)
        period = f"last {hours} h"
    else:
        period = "current job"

    lines, tot_ok, tot_nok, running = [], 0, 0, 0
    for d in devices:
        # a device excluded from the statistics by default keeps its own line
        # but stays out of the totals (for "today": except readings a user
        # included, like Scrap statistics)
        if cmd.period == "today":
            row = stats.get(d.id, {})
            ok, nok = row.get("pass", 0), row.get("fail", 0)
            in_ok, in_nok = row.get("counted_pass", 0), row.get("counted_fail", 0)
        else:
            if cmd.period == "hours":
                ok, nok = _window_counts(db, d, start)
            else:
                state = (db.query(CounterState)
                         .filter(CounterState.device_id == d.id, CounterState.is_active.is_(True)).first())
                ok, nok = (state.shown_pass, state.shown_fail) if state else (0, 0)
            in_ok, in_nok = (0, 0) if d.excluded_by_default else (ok, nok)
        tot_ok, tot_nok = tot_ok + in_ok, tot_nok + in_nok
        prod = production.state(d, now)
        running += prod == production.RUNNING
        values = {
            "device": d.name, "camera": d.name, "state": _STATE_TEXT[prod], "online": "online" if d.connected else "offline",
            "job": d.current_job or "—", "pass": ok, "fail": nok, "total": ok + nok,
            "scrap": _pct(nok, ok + nok), "last_data": _local(d.last_poll_at, tz),
        }
        line = _fill(cmd.line, values)
        lines.append(line + NOT_IN_TOTALS if d.excluded_by_default else line)

    local_now = now.astimezone(tz)
    summary = {
        "date": local_now.strftime("%d.%m.%Y"), "time": local_now.strftime("%H:%M"), "period": period,
        "devices": len(devices), "cameras": len(devices), "in_production": running,
        "total_pass": tot_ok, "total_fail": tot_nok, "total": tot_ok + tot_nok,
        "total_scrap": _pct(tot_nok, tot_ok + tot_nok),
    }
    parts = [_fill(cmd.header, summary)] + (lines or ["No devices."]) + [_fill(cmd.footer, summary)]
    text = "\n".join(p for p in parts if p.strip())
    return text[:MAX_REPLY]


class _Keep(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _fill(template: str, values: dict) -> str:
    try:
        return string.Formatter().vformat(template or "", (), _Keep(values))
    except (ValueError, IndexError):  # e.g. a lone "{" typed in the template
        return template or ""


# ---- handling -----------------------------------------------------------------
def allowed_in(cmd: ChatCommand, chat: str) -> bool:
    return not cmd.group_ids or chat in cmd.group_ids


def parse(text: str, prefix: str) -> tuple[str, str] | None:
    """("status", "line 1") for "!status Line 1", None if not a command."""
    text = (text or "").strip()
    if not text.startswith(prefix):
        return None
    rest = text[len(prefix):].strip()
    if not rest:
        return None
    keyword, _, arg = rest.partition(" ")
    return keyword.lower(), arg.strip()


def answer(db: Session, msg: dict, prefix: str | None = None) -> tuple[str, str | None, str | None]:
    """(outcome, keyword, reply) for one incoming message; reply None = say nothing."""
    parsed = parse(msg.get("text", ""), prefix or get_prefix())
    if parsed is None:
        return "ignored", None, None
    keyword, arg = parsed
    chat = msg.get("chat", "")
    commands = db.query(ChatCommand).filter(ChatCommand.enabled.is_(True)).all()
    here = [c for c in commands if allowed_in(c, chat)]
    cmd = next((c for c in commands if c.keyword == keyword), None)
    p = prefix or get_prefix()
    if cmd is None and keyword == "help":
        if not here:
            return "not_allowed", keyword, None
        listing = "\n".join(f"{p}{c.keyword}" + (f" – {c.description}" if c.description else "")
                            for c in sorted(here, key=lambda c: c.keyword))
        return "answered", keyword, f"Commands (add a device name to see only that device):\n{listing}"
    if cmd is None:
        if not here:
            return "not_allowed", keyword, None
        return "unknown", keyword, f"Unknown command {p}{keyword}. Send {p}help for the list."
    if not allowed_in(cmd, chat):
        return "not_allowed", keyword, None
    return "answered", keyword, render(db, cmd, arg)


def _too_fast(chat: str) -> bool:
    with _rate_lock:
        now = time.monotonic()
        if now - _last_answer.get(chat, -MIN_INTERVAL) < MIN_INTERVAL:
            return True
        _last_answer[chat] = now
        return False


def handle_message(msg: dict) -> None:
    """Called by the WhatsApp client (on its own thread) for each group message
    that may be a command: answer it in the group and log it."""
    from .database import SessionLocal
    from .notifiers.whatsapp_linked import link

    prefix = get_prefix()
    if parse(msg.get("text", ""), prefix) is None:
        return
    db = SessionLocal()
    try:
        try:
            outcome, keyword, reply = answer(db, msg, prefix)
        except Exception as exc:  # noqa: BLE001
            log.exception("command failed: %s", msg.get("text"))
            db.rollback()
            outcome, keyword, reply = "failed", None, None
            error = str(exc)
        else:
            error = None
        if reply and _too_fast(msg["chat"]):
            outcome, reply = "too_fast", None
        if reply:
            try:
                link.send(msg["chat"], reply)
            except Exception as exc:  # noqa: BLE001
                outcome, error = "failed", str(exc)
        db.add(CommandLog(
            chat=msg.get("chat", ""), chat_name=msg.get("chat_name"),
            sender=msg.get("sender_name") or msg.get("sender"), text=msg.get("text", "")[:500],
            keyword=keyword, outcome=outcome, reply=reply or error,
        ))
        db.commit()
    finally:
        db.close()
