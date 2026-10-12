"""A counter that was not reset is not counted again (1.26), and the
Database tab's recalculation puts right the readings 1.25 and older inflated."""
import datetime as dt

import pytest

from app import backup, counters, scrap_stats, stations
from app.database import SessionLocal
from app.models import CounterState, Device, Reading, Station
from app.poller import record_sample
from app.protocols.tcp_listener import TcpListenerDriver

from test_api import login

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 12, 8, 0, tzinfo=UTC)


class Clock:
    def __init__(self, monkeypatch):
        self.now = T0
        monkeypatch.setattr(stations, "utcnow", lambda: self.now)

    def at(self, seconds: float) -> "Clock":
        self.now = T0 + dt.timedelta(seconds=seconds)
        return self


@pytest.fixture()
def db(client):
    login(client)
    s = SessionLocal()
    yield s
    s.close()


def _station(db, name, **source):
    device = Device(name=name, host="x", port=0, protocol="simulator", connected=True)
    db.add(device)
    db.commit()
    st = Station(name=name, default_job="MAIN", sources=stations.check_sources(db, [
        {"device_id": device.id, "ok": "pass", "nok": "fail", "count": "count", "job": "job", **source}]))
    db.add(st)
    db.commit()
    return device, st


def _read(db, device, ok, nok, job="A"):
    # as a camera reports: pass, fail and their sum as the total
    return stations.device_read(db, device, {"pass": ok, "fail": nok, "count": ok + nok, "job": job})


def _totals(db, st, job="A"):
    db.expire_all()
    cs = db.query(CounterState).filter_by(station_id=st.id, job_name=job).one()
    return cs.total_pass, cs.total_fail, cs.total_count


# what a device whose OK counter is reset per box reports, NOK running on:
# (seconds, OK, NOK)
BOXES = [(0, 100, 150), (5, 101, 150), (10, 103, 151), (15, 0, 151), (20, 2, 152), (25, 4, 152),
         (30, 1, 153), (35, 3, 153)]


def test_only_the_ok_counter_resets(db, monkeypatch):
    clock = Clock(monkeypatch)
    device, st = _station(db, "Press", start_count=1)
    for s, ok, nok in BOXES:
        clock.at(s)
        _read(db, device, ok, nok)
    # 1 + 2 before the first reset, 0 + 2 + 2, then 1 + 2; NOK rose 150 -> 153
    assert _totals(db, st) == (10, 3, 13)
    rows = db.query(Reading).filter_by(station_id=st.id).order_by(Reading.id).all()
    assert [(r.ok_added, r.nok_added) for r in rows] == [(0, 0), (1, 0), (2, 1), (0, 0), (2, 1), (2, 0), (1, 1), (2, 0)]


def test_a_listener_that_restarts_its_tally_counts_from_zero(db, monkeypatch):
    Clock(monkeypatch)
    device, st = _station(db, "Pusher", start_count=1)
    driver = TcpListenerDriver("", 5100, {"mode": "event"})
    for record in ["A,Pass", "A,Fail", "A,Pass"]:
        record_sample(db, device, driver.parse(record))
    assert _totals(db, st) == (2, 1, 3)
    # the new job's first part is a NOK: the tally is (0, 1), where NOK did not drop
    record_sample(db, device, driver.parse("B,Fail"))
    record_sample(db, device, driver.parse("B,Pass"))
    assert _totals(db, st, "B") == (1, 1, 2) and _totals(db, st) == (2, 1, 3)
    # and back: A starts again from zero with an OK, where OK did not drop
    record_sample(db, device, driver.parse("A,Pass"))
    assert _totals(db, st) == (3, 1, 4)


def _history(db, clock, device):
    """A day as 1.25 counted it: production, OK resets while running, a
    dashboard reset, and an OK reset during a pause that the next start
    counted (the pieces held for the start window)."""
    for s, ok, nok in BOXES:
        clock.at(s)
        _read(db, device, ok, nok)
    st = db.query(Station).filter(Station.name == device.name).one()
    for cs in db.query(CounterState).filter_by(station_id=st.id):
        cs.reset_shown(clock.at(36).now)        # Reset counters on the station view
    db.commit()
    clock.at(40)
    _read(db, device, 6, 154)
    pause = 40 + 31 * 60                         # the idle timeout is 30 minutes
    clock.at(pause)
    _read(db, device, 0, 154)                    # the OK counter reset during the pause
    clock.at(pause + 10)
    _read(db, device, 1, 154)
    clock.at(pause + 20)
    _read(db, device, 2, 155)                    # 2 OK within 60 s: production starts
    clock.at(pause + 30)
    _read(db, device, 5, 156)
    return st


def _state(db, st):
    db.expire_all()
    rows = db.query(Reading).filter_by(station_id=st.id).order_by(Reading.id).all()
    shown = stations.shown(db, db.get(Station, st.id))
    return {"added": [(r.ok_added, r.nok_added) for r in rows],
            "totals": [(r.total_pass, r.total_fail) for r in rows],
            "counters": _totals(db, st),
            "shown": (shown.shown_pass, shown.shown_fail, shown.shown_count)}


def test_recalculation_matches_what_the_current_rule_counts(client, db, monkeypatch):
    clock = Clock(monkeypatch)
    right_device, _ = _station(db, "Right", start_count=2)
    right = _state(db, _history(db, clock, right_device))
    assert right["counters"] == (18, 6, 24) and right["shown"] == (8, 3, 11)

    monkeypatch.setattr(stations, "deltas", counters.deltas_1_25)
    old_device, _ = _station(db, "Old", start_count=2)
    st = _history(db, clock, old_device)
    monkeypatch.setattr(stations, "deltas", counters.deltas)
    inflated = _state(db, st)
    # NOK 151, 152 and 154 were counted again at the three OK resets
    assert inflated["counters"] == (18, 6 + 151 + 152 + 154, 24 + 151 + 152 + 154)
    assert inflated["shown"] == (8, 3 + 154, 11 + 154)

    p = client.get("/api/database/recount", params={"tz": "UTC"}).json()
    assert p["total"] == {"readings": 3, "ok": 0, "nok": 457, "unclear": 0, "scanned": 26}
    assert [s["station"] for s in p["stations"]] == ["Old"]
    assert p["stations"][0]["days"] == [{"day": "2026-10-12", "readings": 3, "ok": 18, "nok": 463,
                                         "ok_after": 18, "nok_after": 6}]
    assert _state(db, st) == inflated                # the preview changed nothing

    assert client.post("/api/database/recount", json={}).status_code == 400
    r = client.post("/api/database/recount", json={"confirm": True})
    assert r.status_code == 200, r.text
    done = r.json()
    assert (done["readings"], done["ok"], done["nok"]) == (3, 0, 457)
    # the data from before it was saved on the server first
    assert done["safety_backup"].endswith("-before-recalculation.json.gz")
    saved = backup.backups_dir() / done["safety_backup"]
    assert backup.inspect(saved)["tables"]["readings"] == 26
    assert done["safety_backup"] in [b["name"] for b in client.get("/api/database/backup").json()["safety_backups"]]

    assert _state(db, st) == right
    stats = scrap_stats.compute(db, T0.date(), T0.date(), UTC)
    assert {(r["station"], r["pass"], r["fail"]) for r in stats["per_station"]} == {("Right", 18, 6), ("Old", 18, 6)}
    # nothing more to do, and reads carry on from the corrected totals
    again = client.get("/api/database/recount").json()
    assert again["total"]["readings"] == 0 and again["last"]["nok"] == 457
    assert client.post("/api/database/recount", json={"confirm": True}).status_code == 400
    clock.at(40 + 32 * 60)
    _read(db, old_device, 6, 157)
    assert _totals(db, st) == (19, 7, 26)


def test_recalculation_of_readings_from_before_1_8(client, db):
    # up to 1.7 a reading held no counted pieces, only the job's running totals
    st = Station(name="Legacy", default_job="A", sources=[])
    db.add(st)
    db.commit()
    rows = [(100, 150, 0, 0), (103, 151, 3, 1), (1, 152, 4, 153), (4, 152, 7, 153)]
    for i, (ok, nok, tp, tf) in enumerate(rows):
        db.add(Reading(station_id=st.id, job_name="A", raw_pass=ok, raw_fail=nok, raw_count=ok + nok,
                       total_pass=tp, total_fail=tf, created_at=T0 + dt.timedelta(seconds=5 * i)))
    db.add(CounterState(station_id=st.id, job_name="A", total_pass=7, total_fail=153, total_count=160))
    db.commit()

    p = client.get("/api/database/recount").json()
    assert (p["total"]["readings"], p["total"]["nok"]) == (1, 151)
    assert client.post("/api/database/recount", json={"confirm": True}).status_code == 200
    db.expire_all()
    got = db.query(Reading).filter_by(station_id=st.id).order_by(Reading.id).all()
    assert [(r.total_pass, r.total_fail) for r in got] == [(0, 0), (3, 1), (4, 2), (7, 2)]
    assert _totals(db, st) == (7, 2, 9)


def test_a_reading_that_counted_something_else_is_left_alone(client, db, monkeypatch):
    clock = Clock(monkeypatch)
    device, st = _station(db, "Odd", start_count=1)
    for s, ok, nok in BOXES[:4]:
        clock.at(s)
        _read(db, device, ok, nok)
    # the reading of the OK reset says it counted 7 NOK: neither rule gives that
    row = db.query(Reading).filter_by(station_id=st.id).order_by(Reading.id.desc()).first()
    row.nok_added = 7
    db.commit()
    p = client.get("/api/database/recount").json()
    assert p["total"]["readings"] == 0 and p["total"]["unclear"] == 1
    assert (p["unclear"][0]["station"], p["unclear"][0]["counted_nok"], p["unclear"][0]["old_nok"]) == ("Odd", 7, 151)
    assert client.post("/api/database/recount", json={"confirm": True}).status_code == 400


def test_recalculation_is_for_administrators(client):
    login(client)
    client.post("/api/users", json={"username": "op", "password": "pw", "is_admin": False,
                                    "permissions": ["view_dashboard", "view_data", "manage_devices"]})
    client.get("/logout")
    login(client, "op", "pw")
    assert client.get("/api/database/recount").status_code == 403
    assert client.post("/api/database/recount", json={"confirm": True}).status_code == 403
