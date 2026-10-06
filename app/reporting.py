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
# REPLACE VIEW can only append columns).
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
        "Every job the stations have counted, with its ideal cycle time (seconds per piece; empty when not set)",
        """
SELECT
    n.job AS job,
    j.ideal_cycle_s AS ideal_cycle_s,
    j.updated_at AS updated_at_utc
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
