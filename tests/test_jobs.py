"""Ideal cycle times per job: the Jobs list, OEE performance weighed by the
job of each reading, the upgrade from per-station cycle times (1.6), backups,
export / import and the Power BI view."""
import datetime as dt
import logging

from sqlalchemy import text

from app import backup, jobs, oee
from app.database import SessionLocal, engine
from app.models import CounterState, Job, Meta, Reading, Station

from test_api import add_station_device, login, poll

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 6, 12, tzinfo=UTC)


def _run(db, st, job, start, minutes, parts_per_10min, p0=0):
    """Readings every 10 minutes for ``minutes`` of one job; returns the last
    reading time and the parts made."""
    p = p0
    t = start
    for i in range(minutes // 10 + 1):
        t = start + dt.timedelta(minutes=10 * i)
        db.add(Reading(station_id=st.id, job_name=job, total_pass=p, total_fail=0, in_production=True,
                       created_at=t))
        p += parts_per_10min
    return t, parts_per_10min * (minutes // 10)


def test_performance_uses_the_cycle_time_of_each_job(client):
    """A station on J1 (10 s) for 2 h, then J2 (20 s) for 2 h, then J3 (no
    cycle time) for 1 h: J3's pieces and time stay out of performance."""
    db = SessionLocal()
    try:
        db.query(Reading).delete()
        db.query(Station).delete()
        db.query(Job).delete()
        st = Station(name="Mix", sources={}, idle_timeout_min=30, current_job="J3")
        db.add_all([st, Job(name="J1", ideal_cycle_s=10), Job(name="J2", ideal_cycle_s=20), Job(name="J3")])
        db.flush()
        start = NOW - dt.timedelta(hours=6)
        # J1: 60 pieces / 10 min = 6 pieces a minute at 10 s each -> 100 %
        t, n1 = _run(db, st, "J1", start, 120, 60)
        # J2: 15 pieces / 10 min at 20 s each -> 50 %
        t, n2 = _run(db, st, "J2", t + dt.timedelta(minutes=10), 120, 15)
        t, n3 = _run(db, st, "J3", t + dt.timedelta(minutes=10), 60, 30)
        db.commit()
        row = oee.compute(db, hours=24, now=NOW)["stations"][0]
    finally:
        db.close()
    assert (n1, n2, n3) == (720, 180, 180)
    # every 10 min gap counts as production; the first reading of a job adds
    # its gap to that job (no pieces: a job change starts a new baseline)
    timed = (120 + 10 + 120) * 60  # J1 + the switch to J2 + J2
    assert row["timed_production_s"] == timed
    assert row["production_s"] == timed + 70 * 60
    assert row["ideal_s"] == 10 * n1 + 20 * n2
    assert row["performance"] == round((10 * n1 + 20 * n2) / timed, 4)
    assert row["jobs_without_cycle_time"] == ["J3"]
    assert row["ok"] == n1 + n2 + n3  # quality still counts every piece


def test_station_without_any_cycle_time_shows_no_performance(client):
    db = SessionLocal()
    try:
        db.query(Reading).delete()
        db.query(Station).delete()
        db.query(Job).delete()
        st = Station(name="Bare", sources={}, idle_timeout_min=30, current_job="Z")
        db.add(st)
        db.flush()
        _run(db, st, "Z", NOW - dt.timedelta(hours=2), 60, 10)
        db.commit()
        r = oee.compute(db, hours=24, now=NOW)
    finally:
        db.close()
    assert r["stations"][0]["performance"] is None and r["stations"][0]["oee"] is None
    assert r["overall"]["stations"] == 0 and r["overall"]["oee"] is None
    assert r["overall"]["quality"] == 1.0 and r["overall"]["jobs_without_cycle_time"] == ["Z"]


def test_jobs_list_fills_itself_and_admins_set_cycle_times(client):
    login(client)
    did, sid = add_station_device(client, "Press", {"jobs": ["A1"], "parts_per_poll": 5, "fail_ratio": 0.0})
    poll(client, did)
    client.post(f"/api/stations/{sid}/entries", json={"ok": 3, "job": "HAND"})
    listed = {j["name"]: j for j in client.get("/api/jobs").json()}
    assert set(listed) == {"A1", "HAND"}
    assert listed["A1"]["ideal_cycle_s"] is None and listed["A1"]["running_on"] == ["Press"]
    assert listed["HAND"]["stations"] == ["Press"]

    jid = listed["A1"]["id"]
    assert client.patch(f"/api/jobs/{jid}", json={"ideal_cycle_s": 0}).status_code == 422
    r = client.patch(f"/api/jobs/{jid}", json={"ideal_cycle_s": 2.5})
    assert r.status_code == 200 and r.json()["ideal_cycle_s"] == 2.5
    summary = next(s for s in client.get("/api/data/summary").json() if s["id"] == sid)
    assert summary["job_cycle_s"] == 2.5
    assert client.get("/api/data/oee").json()["overall"]["stations"] == 1
    # the station settings no longer carry a cycle time
    assert "ideal_cycle_s" not in client.get("/api/stations").json()[0]

    client.post("/api/users", json={"username": "viewer", "password": "pw", "permissions": ["view_dashboard"]})
    client.get("/logout")
    login(client, "viewer", "pw")
    assert len(client.get("/api/jobs").json()) == 2
    assert client.patch(f"/api/jobs/{jid}", json={"ideal_cycle_s": 9}).status_code == 403
    client.get("/logout")
    login(client)
    assert client.patch(f"/api/jobs/{jid}", json={"ideal_cycle_s": None}).json()["ideal_cycle_s"] is None
    # a counted job stays; one whose stations are gone can be removed
    assert client.delete(f"/api/jobs/{jid}").status_code == 409
    client.delete(f"/api/stations/{sid}")
    assert client.delete(f"/api/jobs/{jid}").status_code == 204
    assert [j["name"] for j in client.get("/api/jobs").json()] == ["HAND"]


def _old_cycle_times(db):
    """A 1.6 database: cycle times on the stations, no job rows, upgrade not run."""
    db.query(Job).delete()
    db.query(Meta).filter(Meta.key == jobs.UPGRADE_KEY).delete()
    a = Station(name="A", sources={}, ideal_cycle_s=4)
    b = Station(name="B", sources={}, ideal_cycle_s=6)
    c = Station(name="C", sources={})  # no cycle time
    db.add_all([a, b, c])
    db.flush()
    t = NOW - dt.timedelta(days=1)
    for st, job, when in ((a, "SHARED", t), (b, "SHARED", t + dt.timedelta(hours=1)),
                          (a, "ONLY_A", t), (c, "ONLY_C", t), (b, "PRESET", t)):
        db.add(CounterState(station_id=st.id, job_name=job, updated_at=when))
    db.add(Job(name="PRESET", ideal_cycle_s=99))  # already set: kept
    db.commit()


def test_upgrade_copies_station_cycle_times_to_their_jobs(client, caplog):
    login(client)
    db = SessionLocal()
    try:
        _old_cycle_times(db)
    finally:
        db.close()
    with caplog.at_level(logging.WARNING, logger="cognex.jobs"):
        assert jobs.upgrade(engine) == 2
    assert jobs.upgrade(engine) == 0  # once per database
    db = SessionLocal()
    try:
        got = {j.name: j.ideal_cycle_s for j in db.query(Job)}
    finally:
        db.close()
    # SHARED: B ran it last; ONLY_C: its station had none; PRESET: kept
    assert got == {"SHARED": 6, "ONLY_A": 4, "ONLY_C": None, "PRESET": 99}
    assert "SHARED" in caplog.text and "'B'" in caplog.text and "A 4 s" in caplog.text


def test_restoring_a_1_6_backup_moves_cycle_times(client, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    login(client)
    db = SessionLocal()
    try:
        _old_cycle_times(db)
    finally:
        db.close()
    path = tmp_path / "old.json.gz"
    backup.write_backup(path)
    with engine.begin() as conn:  # the app moves on in the meantime
        conn.execute(text("DELETE FROM jobs"))
    backup.restore(path)
    db = SessionLocal()
    try:
        assert {j.name: j.ideal_cycle_s for j in db.query(Job)}["SHARED"] == 6
        assert db.get(Meta, jobs.UPGRADE_KEY) is not None
    finally:
        db.close()


def test_backup_round_trip_keeps_job_cycle_times(client, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    login(client)
    did, _ = add_station_device(client, "Cam", {"jobs": ["JX"], "parts_per_poll": 1})
    poll(client, did)
    jid = client.get("/api/jobs").json()[0]["id"]
    client.patch(f"/api/jobs/{jid}", json={"ideal_cycle_s": 7})
    path = tmp_path / "b.json.gz"
    backup.write_backup(path)
    client.patch(f"/api/jobs/{jid}", json={"ideal_cycle_s": None})
    backup.restore(path)
    login(client)
    assert client.get("/api/jobs").json()[0]["ideal_cycle_s"] == 7


def test_export_import_carries_job_cycle_times(client):
    login(client)
    did, sid = add_station_device(client, "Rig", {"jobs": ["R1"], "parts_per_poll": 1})
    poll(client, did)
    jid = client.get("/api/jobs").json()[0]["id"]
    client.patch(f"/api/jobs/{jid}", json={"ideal_cycle_s": 3})
    data = client.get("/api/devices/export").json()
    assert data["version"] == 5 and data["jobs"] == [{"name": "R1", "ideal_cycle_s": 3, "shot_s": 3,
                                                      "pieces_per_shot": 1, "piece_rule": None}]
    assert "ideal_cycle_s" not in data["stations"][0]

    client.patch(f"/api/jobs/{jid}", json={"ideal_cycle_s": None})
    data["jobs"][0]["shot_s"] = None  # as in a version 4 file
    data["jobs"].append({"name": "NEW", "ideal_cycle_s": 1.5})
    r = client.post("/api/devices/import", json=data)
    assert r.status_code == 200, r.text
    assert r.json()["job_cycle_times"] == ["R1", "NEW"]
    assert {j["name"]: j["ideal_cycle_s"] for j in client.get("/api/jobs").json()} == {"R1": 3, "NEW": 1.5}

    # a 1.5 / 1.6 file: the station's cycle time goes to the jobs it ran and
    # its default job, where they have none
    old = {"version": 2, "cameras": data["cameras"], "stations": [
        {**data["stations"][0], "name": "Rig 2", "default_job": "LEGACY", "ideal_cycle_s": 8},
        {**data["stations"][0], "ideal_cycle_s": 9}]}
    r = client.post("/api/devices/import", json=old)
    assert r.status_code == 200, r.text
    got = {j["name"]: j["ideal_cycle_s"] for j in client.get("/api/jobs").json()}
    assert got == {"R1": 3, "NEW": 1.5, "LEGACY": 8, data["stations"][0]["default_job"]: 9}


def test_powerbi_jobs_view(client):
    login(client)
    did, sid = add_station_device(client, "V", {"jobs": ["VJ"], "parts_per_poll": 1})
    poll(client, did)  # counted, not yet in the jobs table
    with engine.connect() as conn:
        assert conn.execute(text("SELECT job, ideal_cycle_s FROM powerbi_jobs")).all() == [("VJ", None)]
    jid = client.get("/api/jobs").json()[0]["id"]
    client.patch(f"/api/jobs/{jid}", json={"ideal_cycle_s": 2})
    with engine.connect() as conn:
        assert conn.execute(text("SELECT job, ideal_cycle_s FROM powerbi_jobs")).all() == [("VJ", 2)]
        assert conn.execute(text("SELECT ideal_cycle_s FROM powerbi_stations")).scalar_one() == 2
        assert conn.execute(text("SELECT ideal_cycle_s FROM powerbi_job_totals")).scalar_one() == 2


# ---- 1.15: cycle time per shot, jobs added before production, Jobs tab ---------
def test_cycle_time_per_shot_of_several_pieces(client):
    login(client)
    did, sid = add_station_device(client, "Mould", {"jobs": ["M8"], "parts_per_poll": 4, "fail_ratio": 0.0})
    poll(client, did)
    jid = client.get("/api/jobs").json()[0]["id"]
    # 12 s per shot of a 4-cavity mould: 3 s per piece
    r = client.patch(f"/api/jobs/{jid}", json={"shot_s": 12, "pieces_per_shot": 4})
    assert r.status_code == 200, r.text
    assert (r.json()["shot_s"], r.json()["pieces_per_shot"], r.json()["ideal_cycle_s"]) == (12, 4, 3)
    assert client.get("/api/data/summary").json()[0]["job_cycle_s"] == 3
    assert client.get("/api/data/summary").json()[0]["job_shot_s"] == 12
    # only the pieces change: the seconds per shot stay
    assert client.patch(f"/api/jobs/{jid}", json={"pieces_per_shot": 2}).json()["ideal_cycle_s"] == 6
    assert client.patch(f"/api/jobs/{jid}", json={"pieces_per_shot": 0}).status_code == 422
    assert client.patch(f"/api/jobs/{jid}", json={"pieces_per_shot": 1001}).status_code == 422
    # a 1.14 client sending seconds per piece: 1 piece per shot
    r = client.patch(f"/api/jobs/{jid}", json={"ideal_cycle_s": 5}).json()
    assert (r["shot_s"], r["pieces_per_shot"], r["ideal_cycle_s"]) == (5, 1, 5)
    # clearing keeps the pieces per shot
    client.patch(f"/api/jobs/{jid}", json={"shot_s": 8, "pieces_per_shot": 4})
    r = client.patch(f"/api/jobs/{jid}", json={"shot_s": None}).json()
    assert (r["shot_s"], r["pieces_per_shot"], r["ideal_cycle_s"]) == (None, 4, None)


def test_station_figures_carry_the_current_jobs_cycle_time(client):
    db = SessionLocal()
    try:
        db.query(Reading).delete()
        db.query(Station).delete()
        db.query(Job).delete()
        st = Station(name="Cav", sources={}, idle_timeout_min=30, current_job="C4")
        job = Job(name="C4")
        jobs.apply_cycle(job, 8, 4)  # 2 s per piece
        db.add_all([st, job])
        db.flush()
        # 60 pieces / 10 min = 10 s per piece actual, 40 s per shot of 4
        _run(db, st, "C4", NOW - dt.timedelta(hours=2), 60, 60)
        db.commit()
        row = oee.compute(db, hours=24, now=NOW)["stations"][0]
    finally:
        db.close()
    c = row["cycle"]
    assert (c["job"], c["shot_s"], c["pieces_per_shot"], c["ideal_cycle_s"]) == ("C4", 8, 4, 2)
    assert (c["actual_cycle_s"], c["actual_shot_s"], c["pieces"]) == (10, 40, 360)
    assert row["performance"] == 0.2  # 2 s ideal / 10 s actual


def test_jobs_added_before_production(client):
    login(client)
    r = client.post("/api/jobs", json={"name": "  4711 ", "shot_s": 30, "pieces_per_shot": 6})
    assert r.status_code == 201, r.text
    assert (r.json()["name"], r.json()["ideal_cycle_s"]) == ("4711", 5)
    assert client.post("/api/jobs", json={"name": "4711"}).status_code == 409
    assert client.post("/api/jobs", json={"name": "   "}).status_code == 400
    assert client.post("/api/jobs", json={"name": "BARE"}).status_code == 201
    listed = {j["name"]: j for j in client.get("/api/jobs").json()}
    assert listed["BARE"]["shot_s"] is None and listed["BARE"]["pieces_per_shot"] == 1
    assert listed["4711"]["stations"] == [] and listed["4711"]["running_on"] == []

    # the device then reports the job: its settings apply from the first piece
    did, sid = add_station_device(client, "Inj", {"jobs": ["4711"], "parts_per_poll": 6})
    poll(client, did)
    listed = {j["name"]: j for j in client.get("/api/jobs").json()}
    assert listed["4711"]["running_on"] == ["Inj"] and listed["4711"]["shot_s"] == 30
    assert next(s for s in client.get("/api/data/oee").json()["stations"] if s["id"] == sid)["cycle"]["shot_s"] == 30
    assert client.delete(f"/api/jobs/{listed['4711']['id']}").status_code == 409  # counted now
    assert client.delete(f"/api/jobs/{listed['BARE']['id']}").status_code == 204

    # export / import keep a job without settings too
    data = client.get("/api/devices/export").json()
    assert {"name": "4711", "ideal_cycle_s": 5, "shot_s": 30, "pieces_per_shot": 6, "piece_rule": None} in data["jobs"]
    data["jobs"].append({"name": "LATER", "pieces_per_shot": 2})
    assert client.post("/api/devices/import", json=data).status_code == 200
    listed = {j["name"]: j for j in client.get("/api/jobs").json()}
    assert listed["LATER"]["shot_s"] is None and listed["4711"]["pieces_per_shot"] == 6

    client.post("/api/users", json={"username": "viewer2", "password": "pw", "permissions": ["view_dashboard"]})
    client.get("/logout")
    login(client, "viewer2", "pw")
    assert client.post("/api/jobs", json={"name": "X"}).status_code == 403
    page = client.get("/jobs")
    assert page.status_code == 200 and "Add job" not in page.text and 'href="/jobs"' in page.text


def test_1_14_cycle_times_become_seconds_per_shot_of_one_piece(client):
    login(client)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs"))
        conn.execute(text("INSERT INTO jobs (name, ideal_cycle_s, created_at, updated_at) "
                          "VALUES ('OLD', 4.5, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    jobs.upgrade(engine)
    db = SessionLocal()
    try:
        j = db.query(Job).filter(Job.name == "OLD").one()
        assert (j.shot_s, j.pieces_per_shot, j.ideal_cycle_s) == (4.5, 1, 4.5)
    finally:
        db.close()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT shot_s, pieces_per_shot FROM powerbi_jobs WHERE job = 'OLD'")).one() == (4.5, 1)


def test_backup_round_trip_keeps_shots(client, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    login(client)
    client.post("/api/jobs", json={"name": "B1", "shot_s": 20, "pieces_per_shot": 8})
    path = tmp_path / "b.json.gz"
    backup.write_backup(path)
    jid = client.get("/api/jobs").json()[0]["id"]
    client.patch(f"/api/jobs/{jid}", json={"shot_s": 1, "pieces_per_shot": 1})
    backup.restore(path)
    login(client)
    j = client.get("/api/jobs").json()[0]
    assert (j["shot_s"], j["pieces_per_shot"], j["ideal_cycle_s"]) == (20, 8, 2.5)
