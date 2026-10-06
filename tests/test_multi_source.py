"""Stations with several sources: each device with its own counters and job,
the start rule (N OK pieces within M seconds), counting only while in
production, the active devices, and the upgrade of 1.5 - 1.7 stations."""
import datetime as dt

import pytest

from app import production, scrap_stats, stations
from app.database import SessionLocal, engine
from app.models import CounterState, Device, Meta, Reading, SourceState, Station

from test_api import login

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 6, 8, 0, tzinfo=UTC)


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


def _devices(db, *names):
    out = [Device(name=n, host="x", port=0, protocol="simulator", connected=True) for n in names]
    db.add_all(out)
    db.commit()
    return out


def _read(db, device, ok=None, nok=None, job=None):
    values = {k: v for k, v in (("pass", ok), ("fail", nok), ("job", job)) if v is not None}
    return stations.device_read(db, device, values)


def _shown(db, st):
    db.expire_all()
    s = stations.shown(db, db.get(Station, st.id))
    return (s.job_name, s.shown_pass, s.shown_fail) if s else None


def test_two_cameras_each_with_its_own_job_and_the_start_rule(db, monkeypatch):
    clock = Clock(monkeypatch)
    cam1, cam2 = _devices(db, "Cam 1", "Cam 2")
    st = Station(name="Station 1", default_job="MAIN", sources=stations.check_sources(db, [
        {"device_id": cam1.id, "ok": "pass", "nok": "fail", "job": "job"},
        {"device_id": cam2.id, "ok": "pass", "nok": "fail", "job": "job", "start_count": 5, "start_window_s": 30},
    ]))
    db.add(st)
    db.commit()

    _read(db, cam1, 100, 5, "A")                      # the baseline
    assert _shown(db, st) == ("A", 0, 0) and production.state(st, clock.now) == production.IDLE
    clock.at(10)
    _read(db, cam1, 101, 5, "A")                      # 1 OK in the window: not yet
    clock.at(80)
    _read(db, cam1, 102, 6, "A")                      # the piece at 10 s left the 60 s window
    assert _shown(db, st) == ("A", 0, 0)
    clock.at(90)
    _read(db, cam1, 103, 6, "A")                      # 2 OK within 60 s: production starts
    db.refresh(st)
    assert production.state(st, clock.now) == production.RUNNING
    assert _shown(db, st) == ("A", 2, 1)              # the pieces that started it count
    clock.at(100)
    _read(db, cam1, 110, 6, "A")
    assert _shown(db, st) == ("A", 9, 1)

    # camera 2 runs job B; the station is in production, so it counts at once
    _read(db, cam2, 50, 0, "B")                       # its baseline
    clock.at(110)
    _read(db, cam2, 53, 1, "B")
    assert _shown(db, st) == ("A + B", 12, 2)
    db.expire_all()
    counters = {c.job_name: (c.total_pass, c.total_fail, c.is_active)
                for c in db.query(CounterState).filter_by(station_id=st.id)}
    assert counters == {"A": (9, 1, True), "B": (3, 1, True)}
    view = stations.sources_view(db, db.get(Station, st.id), stations.devices_of(db, [st]), clock.now)
    assert view["active_devices"] == ["Cam 1", "Cam 2"]
    assert [s["current_job"] for s in view["sources"]] == ["A", "B"]

    # 31 minutes without OK: not in production; NOK changes are not counted
    clock.at(110 + 31 * 60)
    _read(db, cam1, 110, 9, "A")
    _read(db, cam2, 53, 4, "B")
    # (the station's job is now the one of camera 2, the source active last)
    assert _shown(db, st) == ("B + A", 12, 2)
    view = stations.sources_view(db, db.get(Station, st.id), stations.devices_of(db, [st]), clock.now)
    assert view["active_devices"] == [] and view["last_active"]["device"] == "Cam 2"
    # camera 2 needs 5 OK within 30 s to start; 4 is not enough
    clock.at(110 + 32 * 60)
    _read(db, cam2, 57, 4, "B")
    assert _shown(db, st) == ("B + A", 12, 2)
    clock.at(110 + 32 * 60 + 20)
    _read(db, cam2, 58, 5, "B")                        # 5 within 30 s: counts 5 OK, 1 NOK
    assert _shown(db, st) == ("B + A", 17, 3)
    view = stations.sources_view(db, db.get(Station, st.id), stations.devices_of(db, [st]), clock.now)
    assert view["active_devices"] == ["Cam 2"]         # camera 1 made nothing since

    # a manual Stop wins: nothing counts until Start
    db.refresh(st)
    production.stop(st)
    db.commit()
    clock.at(110 + 33 * 60)
    _read(db, cam1, 150, 9, "A")
    _read(db, cam2, 70, 9, "B")
    assert _shown(db, st) == ("B + A", 17, 3)

    # the raw readings are all logged; only the counted pieces are in them as added
    rows = db.query(Reading).filter_by(station_id=st.id).order_by(Reading.id).all()
    assert len(rows) == 13 and sum(r.ok_added for r in rows) == 17 and sum(r.nok_added for r in rows) == 3
    assert {r.device_id for r in rows} == {cam1.id, cam2.id} and {r.source_id for r in rows} == {"s1", "s2"}
    stats = scrap_stats.compute(db, T0.date(), T0.date(), UTC)
    assert (stats["overall"]["pass"], stats["overall"]["fail"]) == (17, 3)
    jobs = {(r["job"], r["pass"], r["fail"]) for r in stats["per_job"]}
    assert jobs == {("A", 9, 1), ("B", 8, 2)}


def test_a_source_without_job_follows_the_station_job(db, monkeypatch):
    clock = Clock(monkeypatch)
    plc, rejects = _devices(db, "PLC", "Rejects")
    st = Station(name="M1", default_job="MAIN", sources=stations.check_sources(db, [
        {"device_id": plc.id, "ok": "pass", "job": "job", "start_count": 1},
        {"device_id": rejects.id, "nok": "fail"},
    ]))
    db.add(st)
    db.commit()
    assert _read(db, rejects, nok=3) == []             # waits for the PLC's job
    _read(db, plc, 10, job="J1")
    _read(db, rejects, nok=3)
    clock.at(5)
    _read(db, plc, 11, job="J1")                       # 1 OK starts production (start_count 1)
    _read(db, rejects, nok=5)
    assert _shown(db, st) == ("J1", 1, 2)
    clock.at(10)
    _read(db, plc, 0, job="J2")                        # job change with a counter reset
    _read(db, rejects, nok=6)
    assert _shown(db, st) == ("J2", 0, 1)
    db.expire_all()
    assert {(c.job_name, c.total_pass, c.total_fail) for c in db.query(CounterState).filter_by(station_id=st.id)} == {
        ("J1", 1, 2), ("J2", 0, 1)}


def test_editing_a_source_starts_a_new_baseline(db, client, monkeypatch):
    Clock(monkeypatch)
    (cam,) = _devices(db, "Cam")
    st = Station(name="E", sources=stations.check_sources(db, [{"device_id": cam.id, "ok": "pass", "start_count": 1}]))
    db.add(st)
    db.commit()
    _read(db, cam, 10, 0)
    r = client.patch(f"/api/stations/{st.id}", json={"sources": [{"id": "s1", "device_id": cam.id, "ok": "fail"}]})
    assert r.status_code == 200, r.text
    db.expire_all()
    _read(db, cam, 10, 500)                            # 500 on the new value: a baseline, not 500 pieces
    assert _shown(db, st) == ("MAIN", 0, 0)


def test_upgrade_of_1_7_stations_keeps_counting(client, monkeypatch):
    """A 1.7 station with OK from one device and NOK from another becomes two
    sources that carry on from the raw values read last."""
    login(client)
    clock = Clock(monkeypatch)
    db = SessionLocal()
    try:
        a, b = _devices(db, "A", "B")
        st = Station(name="Old", sources={"ok": {"device_id": a.id, "key": "pass"},
                                          "nok": {"device_id": b.id, "key": "fail"},
                                          "job": {"device_id": a.id, "key": "job"}},
                     last_pass_change_at=T0 - dt.timedelta(minutes=1))
        db.add(st)
        db.flush()
        db.add(CounterState(station_id=st.id, job_name="J", total_pass=500, total_fail=20, total_count=520,
                            last_raw_pass=1000, last_raw_fail=40, last_raw_count=1040, is_active=True))
        db.query(Meta).filter_by(key=stations.SOURCES_KEY).delete()
        db.commit()
        assert stations.upgrade_sources(engine) == 1
        assert stations.upgrade_sources(engine) == 0  # once per database
        db.expire_all()
        st = db.get(Station, st.id)
        assert [(s["device_id"], s["ok"], s["nok"], s["job"], s["start_count"]) for s in st.source_list()] == [
            (a.id, "pass", None, "job", 1), (b.id, None, "fail", None, 1)]
        assert {(x.source_id, x.last_pass, x.last_fail, x.job) for x in db.query(SourceState)} == {
            ("s1", 1000, 0, "J"), ("s2", 0, 40, "J")}
        _read(db, a, 1010, job="J")    # still in production: +10 from the last raw value
        _read(db, b, nok=43)
        assert _shown(db, st) == ("J", 510, 23)
    finally:
        db.close()
