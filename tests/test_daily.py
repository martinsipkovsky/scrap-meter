"""Daily data for reports (app.daily), the powerbi_daily_* views and the
Power BI switch on the Database tab (app.powerbi_access)."""
import datetime as dt
import socket
import threading

from sqlalchemy import text

from app import daily, powerbi_access, settings_store
from app.database import SessionLocal, engine
from app.models import DailyJob, DailyStation, Job, Reading, Station, StationComment

from test_api import login

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
D29, D30 = dt.date(2026, 9, 29), dt.date(2026, 9, 30)


def at(day, hour, minute=0):
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def _seed():
    """Press: J1 (10 s cycle) on the 29th, J2 (no cycle time) on the 30th."""
    db = SessionLocal()
    try:
        st = Station(name="Press", sources={})
        db.add_all([st, Job(name="J1", ideal_cycle_s=10), Job(name="J2")])
        db.flush()
        db.add(Reading(station_id=st.id, job_name="J1", total_pass=0, total_fail=0, in_production=True,
                       created_at=at(28, 23, 50)))  # baseline
        for k in range(1, 14):  # 08:00 .. 10:00, +6 OK / +1 NOK each
            t = at(29, 8) + dt.timedelta(minutes=10 * (k - 1))
            db.add(Reading(station_id=st.id, job_name="J1", total_pass=6 * k, total_fail=k, in_production=True,
                           excluded=t == at(29, 9), created_at=t))
        db.add(Reading(station_id=st.id, job_name="J1", manual=True, raw_pass=4, raw_fail=2, created_at=at(29, 12)))
        db.add(StationComment(station_id=st.id, station_name="Press", text="tool change", created_at=at(29, 10, 30)))
        db.add(Reading(station_id=st.id, job_name="J2", total_pass=5, total_fail=0, in_production=True,
                       created_at=at(30, 8)))  # job change: new baseline
        db.add(Reading(station_id=st.id, job_name="J2", total_pass=10, total_fail=1, in_production=True,
                       created_at=at(30, 8, 10)))
        db.commit()
        return st.id
    finally:
        db.close()


def _reset_settings():
    settings_store.save(daily.DAILY_KEY, {"timezone": "UTC"})


def test_station_and_job_days(client):
    sid = _seed()
    _reset_settings()
    db = SessionLocal()
    try:
        assert daily.refresh(db, now=NOW) == {"days": 3, "rows": 3}  # 28th .. 30th
        d29 = db.query(DailyStation).filter_by(day=D29).one()
        assert (d29.station_id, d29.station_name, d29.timezone) == (sid, "Press", "UTC")
        assert (d29.ok, d29.nok, d29.manual_ok, d29.manual_nok) == (76, 14, 4, 2)
        assert (d29.excluded_ok, d29.excluded_nok, d29.idle_ok, d29.idle_nok) == (6, 1, 0, 0)
        # 30 min (the gap from the baseline, capped at the idle timeout) + 11 x 10 min,
        # the excluded reading's 10 min left out
        assert (d29.production_s, d29.timed_production_s, d29.ideal_s) == (8400, 8400, 900)
        assert (d29.window_s, d29.complete, d29.readings, d29.comments, d29.jobs) == (86400, True, 13, 1, "J1")
        assert d29.first_reading_at.replace(tzinfo=UTC) == at(29, 8)

        d30 = db.query(DailyStation).filter_by(day=D30).one()
        assert (d30.ok, d30.nok, d30.production_s, d30.timed_production_s, d30.ideal_s) == (5, 1, 2400, 0, 0)
        assert (d30.window_s, d30.complete, d30.jobs) == (12 * 3600, False, "J2")

        j = {(r.day, r.job): r for r in db.query(DailyJob)}
        assert (j[D29, "J1"].ok, j[D29, "J1"].manual_ok, j[D29, "J1"].ideal_cycle_s, j[D29, "J1"].production_s) == (76, 4, 10, 8400)
        assert j[D30, "J2"].ideal_cycle_s is None

        with engine.connect() as conn:
            v = conn.execute(text("SELECT * FROM powerbi_daily_stations WHERE day = :d"), {"d": D29}).mappings().one()
            assert (v["ok_count"], v["nok_count"], v["total_count"]) == (76, 14, 90)
            assert float(v["scrap_pct"]) == 15.56 and float(v["production_min"]) == 140
            assert float(v["availability_pct"]) == 9.72 and float(v["performance_pct"]) == 10.71
            assert float(v["quality_pct"]) == 84.44
            assert float(v["oee_pct"]) == round(100 * 8400 / 86400 * 900 / 8400 * 76 / 90, 2)
            v30 = conn.execute(text("SELECT * FROM powerbi_daily_stations WHERE day = :d"), {"d": D30}).mappings().one()
            assert v30["performance_pct"] is None and v30["oee_pct"] is None
            jobs = conn.execute(text("SELECT job, performance_pct FROM powerbi_daily_jobs ORDER BY day")).all()
            assert [(r[0], None if r[1] is None else float(r[1])) for r in jobs] == [("J1", 10.71), ("J2", None)]
            overall = conn.execute(text("SELECT day, stations, ok_count FROM powerbi_daily_overall ORDER BY day")).all()
            assert [tuple(r)[1:] for r in overall][-2:] == [(1, 76), (1, 5)]
    finally:
        db.close()


def test_refresh_only_redoes_today_and_open_days(client):
    sid = _seed()
    _reset_settings()
    db = SessionLocal()
    try:
        daily.refresh(db, now=NOW)
        # a later run the same day: only today
        assert daily.refresh(db, now=NOW + dt.timedelta(minutes=5))["days"] == 1
        # a new day: the last week again (29th .. Oct 1st) and the 30th becomes complete
        later = dt.datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
        assert daily.refresh(db, now=later)["days"] == 4
        assert db.query(DailyStation).filter_by(day=D30).one().complete is True
        # a deleted station keeps its rows when days are computed again
        db.add(Station(name="New", sources={}))  # first, so SQLite doesn't reuse Press's id
        db.commit()
        db.query(Station).filter_by(id=sid).delete()
        db.commit()
        daily.refresh(db, now=dt.datetime(2026, 10, 2, 6, 0, tzinfo=UTC))
        assert [r.station_name for r in db.query(DailyStation).filter_by(day=D29).order_by(DailyStation.id)] == ["Press", "New"]
        # another time zone: every day again, in that zone (a rebuild: rows of
        # deleted stations go)
        daily.set_timezone("Europe/Bratislava")
        daily.refresh(db, now=later)
        assert {r.timezone for r in db.query(DailyStation)} == {"Europe/Bratislava"}
        assert {r.station_name for r in db.query(DailyStation)} == {"New"}
    finally:
        db.close()


def test_database_tab_api(client):
    login(client)
    _seed()
    _reset_settings()
    r = client.post("/api/database/reading/daily/rebuild")
    assert r.status_code == 200 and r.json()["rows"] >= 3
    info = client.get("/api/database/reading").json()
    assert info["daily"]["timezone"] == "UTC" and info["daily"]["first_day"] == "2026-09-28"
    assert info["access"]["enabled"] is False and info["access"]["port"] == 5119
    assert {"powerbi_daily_stations", "powerbi_daily_jobs", "powerbi_daily_overall"} <= {v["name"] for v in info["views"]}
    assert client.put("/api/database/reading/daily", json={"timezone": "Mars/Base"}).status_code == 400
    assert client.put("/api/database/reading/daily", json={"timezone": "Europe/Prague"}).json()["timezone"] == "Europe/Prague"
    # the tests run on SQLite: reports need PostgreSQL
    assert client.put("/api/database/reading/access", json={"enabled": True}).status_code == 400
    # the daily tables are read-only on the Raw data tab
    tables = {t["name"]: t for t in client.get("/api/rawdb/tables").json()}
    assert tables["daily_stations"]["read_only"]


def test_power_bi_port_is_kept_free_for_it(client):
    login(client)
    settings_store.save(powerbi_access.ACCESS_KEY, {"enabled": True, "port": 5110, "password": "x"})
    try:
        body = {"name": "Push", "host": "0.0.0.0", "port": 5110, "protocol": "tcp_listen", "protocol_config": {}}
        r = client.post("/api/devices", json=body)
        assert r.status_code == 409 and "Power BI" in r.text
        assert client.post("/api/devices", json={**body, "port": 5111}).status_code == 201
    finally:
        settings_store.save(powerbi_access.ACCESS_KEY, None)


def _startup(user):
    import struct

    body = struct.pack("!i", 196608) + b"user\0" + user.encode() + b"\0database\0cognex\0\0"
    return struct.pack("!i", len(body) + 4) + body


def test_forwarder_lets_only_the_read_only_login_through():
    import struct

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    got = []

    def upstream():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                while data := conn.recv(1000):
                    got.append(data)
                    conn.sendall(b"R")

    threading.Thread(target=upstream, daemon=True).start()
    fwd = powerbi_access.Forwarder()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    fwd.start(port, srv.getsockname(), "powerbi")
    try:
        assert fwd.running and fwd.error is None
        assert powerbi_access.startup_user(_startup("powerbi")) == "powerbi"
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.sendall(struct.pack("!ii", 8, 80877103))  # SSLRequest: answered by the port
            assert c.recv(1) == b"N"
            c.sendall(_startup("powerbi"))
            assert c.recv(10) == b"R"
            c.sendall(b"Q-select")
            assert c.recv(10) == b"R"
        assert got[0] == _startup("powerbi") and got[1] == b"Q-select"
        # the app's own login is refused with an error message, nothing reaches the database
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.sendall(_startup("cognex"))
            reply = c.recv(1000)
            assert reply[:1] == b"E" and b"read-only login 'powerbi'" in reply
        assert len(got) == 2
    finally:
        fwd.stop()
        srv.close()
    assert not fwd.running


def test_daily_rows_survive_a_backup_round_trip(client):
    import io

    login(client)
    _seed()
    _reset_settings()
    db = SessionLocal()
    try:
        daily.refresh(db, now=NOW)
    finally:
        db.close()
    saved = client.get("/api/database/backup/download").content
    info = client.post("/api/database/backup/inspect",
                       files={"file": ("b.json.gz", io.BytesIO(saved), "application/gzip")}).json()
    assert info["tables"]["daily_stations"] == 3
    r = client.post("/api/database/backup/import", json={"token": info["token"], "confirm": True})
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        assert db.query(DailyStation).filter_by(day=D29).one().ok == 76
    finally:
        db.close()
