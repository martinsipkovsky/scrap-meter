"""Scrap statistics over a date range (the Scrap statistics page).

Parts are counted the same way as the device view's OK/NOK chart: each
reading carries the running totals of its job, so the parts made between two
consecutive readings of the same job are the difference of their totals, and
a job change starts a new baseline. Scrap is fail / (pass + fail).

Parts are left out (and reported separately) when their reading
* was excluded by a user, or
* was taken while the device was not in production (idle or stopped).
  Readings logged before that was recorded fall back to the idle rule: no
  pass increase within the device's idle timeout.

A device set to "exclude" from the statistics (Device.stats_default) still
gets its own per-device and per-job rows, marked ``excluded_by_default``, but
its parts stay out of the overall figures and the per-day rows unless a user
included the reading (Reading.included). Those parts are reported under
left_out.excluded_devices. ``counted_pass`` / ``counted_fail`` on a device row
are its parts that are in the overall figures.

Days are calendar days in the viewer's time zone; the range is inclusive.
"""
from __future__ import annotations

import datetime as dt
import io
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from . import production
from .models import Device, Reading


def zone(name: str | None) -> dt.tzinfo:
    try:
        return ZoneInfo(name) if name else dt.timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc


def _aware(t: dt.datetime) -> dt.datetime:
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t


def _row(**keys) -> dict:
    return {**keys, "pass": 0, "fail": 0}


def _finish(rows: list[dict]) -> list[dict]:
    for r in rows:
        r["total"] = r["pass"] + r["fail"]
        r["scrap_rate"] = round(r["fail"] / r["total"], 4) if r["total"] else 0.0
    return rows


def day_bounds(first_day: dt.date, last_day: dt.date, tz: dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
    """UTC start and (exclusive) end of an inclusive range of local days."""
    start = dt.datetime.combine(first_day, dt.time(), tz).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(last_day + dt.timedelta(days=1), dt.time(), tz).astimezone(dt.timezone.utc)
    return start, end


def compute(db: Session, first_day: dt.date, last_day: dt.date, tz: dt.tzinfo) -> dict:
    start, end = day_bounds(first_day, last_day, tz)

    days = {}
    d = first_day
    while d <= last_day:
        days[d] = _row(day=d.isoformat())
        d += dt.timedelta(days=1)
    overall = _row()
    left_out = {"excluded": _row(readings=0), "not_in_production": _row(), "excluded_devices": _row()}
    per_camera: dict[int, dict] = {}
    per_job: dict[tuple[int, str], dict] = {}

    cols = (Reading.job_name, Reading.total_pass, Reading.total_fail, Reading.created_at,
            Reading.excluded, Reading.in_production, Reading.included)
    for dev in db.query(Device).order_by(Device.name).all():
        by_default = dev.excluded_by_default
        cam = per_camera[dev.id] = _row(camera_id=dev.id, camera=dev.name, excluded_by_default=by_default,
                                        counted_pass=0, counted_fail=0)
        timeout = dt.timedelta(minutes=max(1, dev.idle_timeout_min or production.DEFAULT_IDLE_TIMEOUT_MIN))
        # start one idle timeout early so the idle rule knows the last pass
        # increase before the range
        lead = start - timeout
        q = db.query(*cols).filter(Reading.device_id == dev.id)
        baseline = q.filter(Reading.created_at < lead).order_by(Reading.created_at.desc(), Reading.id.desc()).first()
        rows = (
            q.filter(Reading.created_at >= lead, Reading.created_at < end)
            .order_by(Reading.created_at.asc(), Reading.id.asc())
            .yield_per(5000)
        )
        prev = baseline
        last_increase = None
        for r in rows:
            t = _aware(r.created_at)
            same_job = prev is not None and prev.job_name == r.job_name
            d_ok = max(r.total_pass - prev.total_pass, 0) if same_job else 0
            d_nok = max(r.total_fail - prev.total_fail, 0) if same_job else 0
            prev = r
            if d_ok:
                last_increase = t
            if t < start or not (d_ok or d_nok):
                continue
            if r.excluded:
                targets = (left_out["excluded"],)
                left_out["excluded"]["readings"] += 1
            elif not (r.in_production if r.in_production is not None
                      else last_increase is not None and t - last_increase < timeout):
                targets = (left_out["not_in_production"],)
            else:
                job = r.job_name or "—"
                jrow = per_job.setdefault(
                    (dev.id, job), _row(camera=dev.name, job=job, excluded_by_default=by_default))
                if by_default and not r.included:
                    targets = (cam, jrow, left_out["excluded_devices"])
                else:
                    targets = (overall, cam, jrow, days[t.astimezone(tz).date()])
                    cam["counted_pass"] += d_ok
                    cam["counted_fail"] += d_nok
            for target in targets:
                target["pass"] += d_ok
                target["fail"] += d_nok

    excluded_readings = (
        db.query(Reading.id)
        .filter(Reading.excluded.is_(True), Reading.created_at >= start, Reading.created_at < end)
        .count()
    )
    return {
        "from": first_day.isoformat(),
        "to": last_day.isoformat(),
        "overall": _finish([overall])[0],
        "per_camera": _finish(list(per_camera.values())),
        "per_job": _finish(sorted(per_job.values(), key=lambda r: (r["camera"], r["job"]))),
        "per_day": _finish(list(days.values())),
        "left_out": {
            "excluded": {**_finish([left_out["excluded"]])[0], "readings": excluded_readings},
            "not_in_production": _finish([left_out["not_in_production"]])[0],
            "excluded_devices": _finish([left_out["excluded_devices"]])[0],
        },
    }


def to_xlsx(stats: dict) -> bytes:
    """One sheet per view, with the same figures as the screen."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    period = f"{stats['from']} to {stats['to']}"
    lo = stats["left_out"]
    overall = [
        {**stats["overall"], "what": f"Counted, {period}"},
        {**lo["excluded"], "what": "Left out: excluded readings"},
        {**lo["not_in_production"], "what": "Left out: not in production"},
        {**lo["excluded_devices"], "what": "Left out: devices excluded by default"},
    ]
    sheets = [
        ("Overall", ["Parts"], overall, ["what"]),
        ("Per device", ["Device"], stats["per_camera"], ["camera"]),
        ("Per job", ["Device", "Job"], stats["per_job"], ["camera", "job"]),
        ("Per day", ["Day"], stats["per_day"], ["day"]),
    ]
    for i, (title, heads, rows, keys) in enumerate(sheets):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = title
        marked = "camera" in keys  # device rows say whether they are in the totals
        ws.append(heads + ["Pass", "Fail", "Total", "Scrap %"] + (["In totals"] if marked else []))
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            day = [dt.date.fromisoformat(r["day"])] if keys == ["day"] else []
            ws.append(day + [r[k] for k in keys if k != "day"] + [r["pass"], r["fail"], r["total"], r["scrap_rate"]]
                      + (["No (excluded by default)" if r.get("excluded_by_default") else "Yes"] if marked else []))
        n = len(keys)
        for row in ws.iter_rows(min_row=2):
            row[n + 3].number_format = "0.00%"
            if keys == ["day"]:
                row[0].number_format = "yyyy-mm-dd"
        for col, width in zip("ABCDEFGH", [36 if keys == ["what"] else 24] * n + [12, 12, 12, 10, 24]):
            ws.column_dimensions[col].width = width
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
