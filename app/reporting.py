"""Read-only access to the data for reports (Power BI, Excel, SQL clients).

Reports read the powerbi_* views, made on every startup, not the tables: the
views keep their column names when the tables change, add the station name
next to station ids, and leave out secrets (device logins, user passwords,
notification tokens).

With POWERBI_PASSWORD set (Postgres only), a login named POWERBI_USER
("powerbi" by default) is made, or its password updated, on startup. It may
read the powerbi_* views and nothing else.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .config import settings

log = logging.getLogger("cognex.reporting")

# name -> (what it holds, SELECT). Column names stay as they are, since
# reports refer to them: add new columns at the end only (Postgres' CREATE OR
# REPLACE VIEW can only append columns). Corrections (negative manual
# entries) can take a day's OK or NOK below zero: scrap and quality stay
# within 0 - 100 % then, and are empty without parts.
VIEWS: dict[str, tuple[str, str]] = {
    "powerbi_station_comments": (
        "Comments written on the stations, with the job and OK / NOK / scrap at that moment",
        """
SELECT
    c.id AS comment_id,
    c.created_at AS commented_at_utc,
    c.station_id AS station_id,
    c.station_name AS station_name,
    c.job_name AS job,
    c.ok_count AS ok_count,
    c.nok_count AS nok_count,
    ROUND(CAST(c.scrap_rate * 100 AS NUMERIC), 2) AS scrap_pct,
    c.text AS comment,
    c.author AS author
FROM station_comments c
""",
    ),
    "powerbi_stations": (
        "The stations and their settings; ideal_cycle_s is the one of the current job",
        """
SELECT
    s.id AS station_id,
    s.name AS station_name,
    s.current_job AS current_job,
    s.default_job AS default_job,
    s.stats_default AS statistics,
    (SELECT j.ideal_cycle_s FROM jobs j WHERE j.name = s.current_job) AS ideal_cycle_s,
    s.idle_timeout_min AS idle_timeout_min,
    s.manual_stop AS stopped_by_operator,
    s.last_reading_at AS last_reading_at_utc,
    s.created_at AS created_at_utc
FROM stations s
""",
    ),
    "powerbi_job_totals": (
        "OK / NOK per station and job since counting started (device counts plus manual entries)",
        """
SELECT
    t.station_id AS station_id,
    s.name AS station_name,
    t.job_name AS job,
    t.total_pass + COALESCE(t.manual_pass, 0) AS ok_count,
    t.total_fail + COALESCE(t.manual_fail, 0) AS nok_count,
    COALESCE(t.manual_pass, 0) AS manual_ok,
    COALESCE(t.manual_fail, 0) AS manual_nok,
    t.is_active AS is_current_job,
    t.started_at AS started_at_utc,
    t.updated_at AS updated_at_utc,
    j.ideal_cycle_s AS ideal_cycle_s
FROM counter_states t
LEFT JOIN stations s ON s.id = t.station_id
LEFT JOIN jobs j ON j.name = t.job_name
""",
    ),
    "powerbi_jobs": (
        "Every job (counted by the stations or added on the Jobs tab), with its ideal cycle time (seconds per "
        "piece; empty when not set) and the cycle time as set: seconds per shot making pieces_per_shot pieces",
        """
SELECT
    n.job AS job,
    j.ideal_cycle_s AS ideal_cycle_s,
    j.updated_at AS updated_at_utc,
    j.shot_s AS shot_s,
    COALESCE(j.pieces_per_shot, 1) AS pieces_per_shot
FROM (SELECT name AS job FROM jobs UNION SELECT job_name FROM counter_states) n
LEFT JOIN jobs j ON j.name = n.job
""",
    ),
    "powerbi_readings": (
        "Every reading and manual entry (the history behind the chart and the statistics); "
        "ok_added / nok_added: the pieces a reading counted (empty before 1.8)",
        """
SELECT
    r.id AS reading_id,
    r.created_at AS read_at_utc,
    r.station_id AS station_id,
    s.name AS station_name,
    r.job_name AS job,
    r.manual AS manual_entry,
    r.raw_pass AS raw_ok,
    r.raw_fail AS raw_nok,
    r.raw_count AS raw_count,
    r.total_pass AS total_ok,
    r.total_fail AS total_nok,
    r.in_production AS in_production,
    r.excluded AS excluded,
    r.included AS included,
    r.note AS note,
    r.entered_by AS entered_by,
    r.device_id AS device_id,
    r.source_id AS source_id,
    r.ok_added AS ok_added,
    r.nok_added AS nok_added
FROM readings r
LEFT JOIN stations s ON s.id = r.station_id
""",
    ),
    "powerbi_daily_stations": (
        "Every day per station: OK / NOK, scrap, parts left out, production time, availability, "
        "performance, quality and OEE (days in the time zone chosen on the Database tab)",
        """
SELECT
    d.day AS day,
    d.station_id AS station_id,
    d.station_name AS station_name,
    d.ok AS ok_count,
    d.nok AS nok_count,
    d.ok + d.nok AS total_count,
    ROUND(CAST(100.0 * (CASE WHEN d.ok + d.nok <= 0 THEN NULL WHEN d.nok <= 0 THEN 0.0 WHEN d.nok >= d.ok + d.nok THEN 1.0 ELSE 1.0 * d.nok / (d.ok + d.nok) END) AS NUMERIC), 2) AS scrap_pct,
    d.manual_ok AS manual_ok,
    d.manual_nok AS manual_nok,
    d.excluded_ok AS excluded_ok,
    d.excluded_nok AS excluded_nok,
    d.idle_ok AS not_in_production_ok,
    d.idle_nok AS not_in_production_nok,
    ROUND(CAST(d.production_s / 60.0 AS NUMERIC), 1) AS production_min,
    ROUND(CAST(d.window_s / 60.0 AS NUMERIC), 1) AS day_min,
    ROUND(CAST(100.0 * (d.production_s) / NULLIF(d.window_s, 0) AS NUMERIC), 2) AS availability_pct,
    ROUND(CAST(100.0 * (d.ideal_s) / NULLIF(d.timed_production_s, 0) AS NUMERIC), 2) AS performance_pct,
    ROUND(CAST(100.0 * (CASE WHEN d.ok + d.nok <= 0 THEN NULL WHEN d.ok <= 0 THEN 0.0 WHEN d.ok >= d.ok + d.nok THEN 1.0 ELSE 1.0 * d.ok / (d.ok + d.nok) END) AS NUMERIC), 2) AS quality_pct,
    ROUND(CAST(100.0 * (d.production_s) / NULLIF(d.window_s, 0) * (d.ideal_s) / NULLIF(d.timed_production_s, 0) * (CASE WHEN d.ok + d.nok <= 0 THEN NULL WHEN d.ok <= 0 THEN 0.0 WHEN d.ok >= d.ok + d.nok THEN 1.0 ELSE 1.0 * d.ok / (d.ok + d.nok) END) AS NUMERIC), 2) AS oee_pct,
    d.jobs AS jobs,
    d.readings AS readings,
    d.comments AS comments,
    d.in_totals AS in_statistics,
    d.first_reading_at AS first_reading_at_utc,
    d.last_reading_at AS last_reading_at_utc,
    d.complete AS day_complete,
    d.timezone AS timezone,
    d.updated_at AS updated_at_utc
FROM daily_stations d
""",
    ),
    "powerbi_daily_jobs": (
        "Every day per station and job: OK / NOK, scrap, production time and performance",
        """
SELECT
    j.day AS day,
    j.station_id AS station_id,
    j.station_name AS station_name,
    j.job AS job,
    j.ok AS ok_count,
    j.nok AS nok_count,
    j.ok + j.nok AS total_count,
    ROUND(CAST(100.0 * (CASE WHEN j.ok + j.nok <= 0 THEN NULL WHEN j.nok <= 0 THEN 0.0 WHEN j.nok >= j.ok + j.nok THEN 1.0 ELSE 1.0 * j.nok / (j.ok + j.nok) END) AS NUMERIC), 2) AS scrap_pct,
    j.manual_ok AS manual_ok,
    j.manual_nok AS manual_nok,
    ROUND(CAST(j.production_s / 60.0 AS NUMERIC), 1) AS production_min,
    j.ideal_cycle_s AS ideal_cycle_s,
    CASE WHEN j.ideal_cycle_s IS NULL THEN NULL ELSE ROUND(CAST(100.0 * (j.ideal_s) / NULLIF(j.production_s, 0) AS NUMERIC), 2) END AS performance_pct,
    j.updated_at AS updated_at_utc
FROM daily_jobs j
""",
    ),
    "powerbi_daily_overall": (
        "Every day, all stations in the statistics together: OK / NOK, scrap, availability, performance, "
        "quality and OEE",
        """
SELECT
    t.day AS day,
    t.stations AS stations,
    t.ok AS ok_count,
    t.nok AS nok_count,
    t.ok + t.nok AS total_count,
    ROUND(CAST(100.0 * (CASE WHEN t.ok + t.nok <= 0 THEN NULL WHEN t.nok <= 0 THEN 0.0 WHEN t.nok >= t.ok + t.nok THEN 1.0 ELSE 1.0 * t.nok / (t.ok + t.nok) END) AS NUMERIC), 2) AS scrap_pct,
    ROUND(CAST(t.production_s / 60.0 AS NUMERIC), 1) AS production_min,
    ROUND(CAST(100.0 * (t.production_s) / NULLIF(t.window_s, 0) AS NUMERIC), 2) AS availability_pct,
    ROUND(CAST(100.0 * (t.ideal_s) / NULLIF(t.timed_production_s, 0) AS NUMERIC), 2) AS performance_pct,
    ROUND(CAST(100.0 * (CASE WHEN t.ok + t.nok <= 0 THEN NULL WHEN t.ok <= 0 THEN 0.0 WHEN t.ok >= t.ok + t.nok THEN 1.0 ELSE 1.0 * t.ok / (t.ok + t.nok) END) AS NUMERIC), 2) AS quality_pct,
    ROUND(CAST(100.0 * (t.production_s) / NULLIF(t.window_s, 0) * (t.ideal_s) / NULLIF(t.timed_production_s, 0) * (CASE WHEN t.ok + t.nok <= 0 THEN NULL WHEN t.ok <= 0 THEN 0.0 WHEN t.ok >= t.ok + t.nok THEN 1.0 ELSE 1.0 * t.ok / (t.ok + t.nok) END) AS NUMERIC), 2) AS oee_pct,
    t.comments AS comments
FROM (
    SELECT day, COUNT(*) AS stations, SUM(ok) AS ok, SUM(nok) AS nok, SUM(production_s) AS production_s,
           SUM(window_s) AS window_s, SUM(ideal_s) AS ideal_s, SUM(timed_production_s) AS timed_production_s,
           SUM(comments) AS comments
    FROM daily_stations
    WHERE in_totals
    GROUP BY day
) t
""",
    ),
    "powerbi_devices": (
        "The devices (connections), without their login settings",
        """
SELECT
    d.id AS device_id,
    d.name AS device_name,
    d.protocol AS protocol,
    d.host AS host,
    d.port AS port,
    d.enabled AS enabled,
    d.connected AS connected,
    d.last_error AS last_error,
    d.last_poll_at AS last_read_at_utc
FROM devices d
""",
    ),
}


def ensure_views(engine: Engine) -> None:
    """Make the views, and the read-only login when one is set up."""
    with engine.begin() as conn:
        for name, (_, select) in VIEWS.items():
            if conn.dialect.name == "postgresql":
                conn.execute(text(f"CREATE OR REPLACE VIEW {name} AS {select}"))
            else:
                conn.execute(text(f"DROP VIEW IF EXISTS {name}"))
                conn.execute(text(f"CREATE VIEW {name} AS {select}"))
    if reader_configured(engine):
        try:
            ensure_reader(engine, settings.powerbi_user, settings.powerbi_password)
        except Exception as exc:  # noqa: BLE001 - never stop the app over this
            log.warning("could not set up the read-only login '%s': %s", settings.powerbi_user, exc)


def reader_configured(engine: Engine) -> bool:
    return bool(settings.powerbi_password) and engine.dialect.name == "postgresql"


def ensure_reader(engine: Engine, user: str, password: str) -> None:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", user):
        raise ValueError("POWERBI_USER may only hold lower-case letters, digits and _")
    with engine.begin() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :u"), {"u": user}).first()
        # the password can't be a bind parameter in CREATE / ALTER ROLE
        pw = "'" + password.replace("'", "''") + "'"
        conn.execute(text(f"{'ALTER' if exists else 'CREATE'} ROLE {user} WITH LOGIN PASSWORD {pw} "
                          "NOSUPERUSER NOCREATEDB NOCREATEROLE"))
        dbname = conn.execute(text("SELECT current_database()")).scalar_one()
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{dbname}" TO {user}'))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {user}"))
        conn.execute(text(f"GRANT SELECT ON {', '.join(VIEWS)} TO {user}"))
    log.info("read-only login '%s' can read %s", user, ", ".join(VIEWS))
