"""Piece rules: several camera pictures make one piece (app.pieces), counted
by the stations (app.stations) and set per job (API, export / import)."""
import datetime as dt

import pytest

from app import pieces, production, stations
from app.database import SessionLocal
from app.models import CounterState, Device, Job, Reading, SourceState, Station

from test_api import login

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 7, 8, 0, tzinfo=UTC)


def rule(**kw):
    return pieces.normalize({"pictures": 2, **kw})


def run(r, pictures, piece=None, t=0.0, ids=None):
    """Feed pictures one at a time ("OK" / "NOK"); returns (OK, NOK, open piece)."""
    ok = nok = 0
    for i, p in enumerate(pictures):
        a, b, piece = pieces.apply(r, piece, p == "OK", p == "NOK", t, ids[i] if ids else None)
        ok, nok = ok + a, nok + b
    return ok, nok, piece


# ---- the rule --------------------------------------------------------------------
def test_normalize():
    assert pieces.normalize(None) is None and pieces.normalize({"pictures": 1}) is None
    r = rule()
    assert r == {"pictures": 2, "verdict": "all_ok", "min_ok": 2, "nok_closes": False, "timeout_s": None,
                 "missing": "nok", "group_key": None}
    assert pieces.normalize({"pictures": 1, "group_key": "id"})["group_key"] == "id"
    for bad in ({"pictures": 0}, {"pictures": 99}, {"pictures": 3, "verdict": "min_ok", "min_ok": 4},
                {"pictures": 2, "verdict": "x"}, {"pictures": 2, "missing": "x"}, {"pictures": 2, "timeout_s": -1}):
        with pytest.raises(ValueError):
            pieces.normalize(bad)
    assert pieces.describe(None) == "1 picture = 1 piece"
    assert pieces.describe(rule(timeout_s=5)) == ("2 pictures = 1 piece, OK only if all pictures are OK, "
                                                   "judged after 5 s, missing pictures make it NOK")


def test_two_pictures_one_piece():
    r = rule()
    # Martin's example: one good and one bad picture make a bad piece
    assert run(r, ["OK", "NOK"])[:2] == (0, 1)
    assert run(r, ["NOK", "OK"])[:2] == (0, 1)
    assert run(r, ["OK", "OK"])[:2] == (1, 0)
    ok, nok, piece = run(r, ["OK", "OK", "OK"])
    assert (ok, nok, piece["ok"], piece["nok"]) == (1, 0, 1, 0)  # the third waits for its partner
    assert run(r, ["OK"], piece)[:2] == (1, 0)


def test_at_least_k_of_n():
    r = pieces.normalize({"pictures": 3, "verdict": "min_ok", "min_ok": 2})
    assert run(r, ["OK", "NOK", "OK"])[:2] == (1, 0)
    assert run(r, ["NOK", "NOK", "OK"])[:2] == (0, 1)


def test_nok_ends_the_piece():
    r = pieces.normalize({"pictures": 3, "nok_closes": True})
    # OK, NOK ends the first piece; the next three pictures are the second
    assert run(r, ["OK", "NOK", "OK", "OK", "OK"])[:2] == (1, 1)
    r2 = pieces.normalize({"pictures": 3, "verdict": "min_ok", "min_ok": 2, "nok_closes": True})
    assert run(r2, ["NOK", "OK", "NOK", "OK", "OK", "OK"])[:2] == (1, 1)


def test_timer_and_missing_pictures():
    for missing, want in (("nok", (0, 1)), ("judge", (1, 0)), ("discard", (0, 0))):
        r = rule(timeout_s=10, missing=missing)
        _, _, piece = run(r, ["OK"], t=0)
        assert pieces.apply(r, piece, 0, 0, 9.9)[:2] == (0, 0)  # still waiting
        ok, nok, piece = pieces.apply(r, piece, 0, 0, 10)
        assert (ok, nok, piece) == (*want, None), missing
    # judging the pictures taken: a NOK among them makes the piece NOK
    r = rule(timeout_s=10, missing="judge")
    _, _, piece = run(r, ["NOK"], t=0)
    assert pieces.apply(r, piece, 0, 0, 11)[:2] == (0, 1)
    # a new picture after the timer starts a new piece
    _, _, piece = run(r, ["OK"], t=0)
    assert pieces.apply(r, piece, 1, 0, 20)[:2] == (1, 0) and pieces.apply(r, piece, 1, 0, 20)[2]["ok"] == 1
    # job change / stop: close_open
    assert pieces.close_open(rule(), {"ok": 1, "nok": 0, "since": 0, "id": None}) == (0, 1)
    assert pieces.close_open(rule(missing="discard"), {"ok": 1, "nok": 0, "since": 0, "id": None}) == (0, 0)


def test_piece_id_groups_pictures():
    r = pieces.normalize({"pictures": 3, "group_key": "piece", "missing": "judge"})
    # piece 7 gets two pictures (judged when piece 8 starts), piece 8 three
    ok, nok, piece = run(r, ["OK", "OK", "OK", "NOK", "OK"], ids=["7", "7", "8", "8", "8"])
    assert (ok, nok, piece["id"]) == (1, 1, "8")
    r = pieces.normalize({"pictures": 2, "group_key": "piece"})  # missing = NOK
    assert run(r, ["OK", "OK", "OK"], ids=["1", "2", "2"])[:2] == (1, 1)


def test_polled_jumps_assume_the_worst():
    r = rule()
    # 2 OK and 2 NOK pictures in one read: each NOK spoils its own piece
    assert pieces.apply(r, None, 2, 2, 0)[:2] == (0, 2)
    assert pieces.apply(r, None, 3, 1, 0)[:2] == (1, 1)
    ok, nok, piece = pieces.apply(r, None, 1001, 0, 0)
    assert (ok, nok, piece["ok"]) == (500, 0, 1)
    assert pieces.apply(r, None, 0, 6, 0)[:2] == (0, 3)
    assert pieces.apply(pieces.normalize({"pictures": 2, "nok_closes": True}), None, 0, 6, 0)[:2] == (0, 6)
    # no rule: pictures are pieces
    assert pieces.apply(None, None, 5, 2, 0) == (5, 2, None)


# ---- counted by a station ------------------------------------------------------------
class Clock:
    def __init__(self, monkeypatch):
        self.now = T0
        monkeypatch.setattr(stations, "utcnow", lambda: self.now)

    def at(self, seconds):
        self.now = T0 + dt.timedelta(seconds=seconds)
        return self


@pytest.fixture()
def db(client):
    login(client)
    s = SessionLocal()
    yield s
    s.close()


def _setup(db, piece_rule, **values):
    cam = Device(name="Cam", host="x", port=0, protocol="simulator", connected=True)
    db.add(cam)
    db.commit()
    st = Station(name="Press", default_job="J1", sources=stations.check_sources(db, [
        {"device_id": cam.id, "ok": "pass", "nok": "fail", "start_count": 1}]))
    db.add_all([st, Job(name="J1", piece_rule=pieces.normalize(piece_rule))])
    db.commit()
    production.start(st)
    db.commit()
    return cam, st


def _read(db, cam, ok, nok, **extra):
    return stations.device_read(db, cam, {"pass": ok, "fail": nok, **extra})


def _totals(db, st):
    db.expire_all()
    cs = db.query(CounterState).filter_by(station_id=st.id, job_name="J1").one()
    return cs.total_pass, cs.total_fail


def test_station_counts_pieces_and_logs_pictures(db, monkeypatch):
    clock = Clock(monkeypatch)
    cam, st = _setup(db, {"pictures": 2})
    _read(db, cam, 0, 0)  # baseline
    clock.at(5); _read(db, cam, 1, 0)   # OK picture: piece open
    clock.at(10); _read(db, cam, 1, 1)  # NOK picture: the piece is NOK
    clock.at(15); _read(db, cam, 2, 1)
    clock.at(20); _read(db, cam, 3, 1)  # OK + OK: an OK piece
    assert _totals(db, st) == (1, 1)
    last = db.query(Reading).order_by(Reading.id.desc()).first()
    assert (last.raw_pass, last.raw_fail, last.ok_added, last.nok_added) == (3, 1, 1, 0)
    clock.at(25); _read(db, cam, 4, 1)
    state = db.query(SourceState).filter_by(station_id=st.id).one()
    assert state.open_piece["ok"] == 1
    # the open piece shows on the station and in the jobs list
    view = stations.sources_view(db, db.get(Station, st.id), stations.devices_of(db, [st]))
    assert view["sources"][0]["open_piece"]["ok"] == 1


def test_job_change_and_stop_close_the_open_piece(db, monkeypatch):
    clock = Clock(monkeypatch)
    cam, st = _setup(db, {"pictures": 2, "missing": "nok"})
    st.sources = stations.check_sources(db, [{"device_id": cam.id, "ok": "pass", "nok": "fail", "job": "job",
                                              "start_count": 1}])
    db.commit()
    _read(db, cam, 0, 0, job="J1")
    clock.at(5); _read(db, cam, 1, 0, job="J1")  # half a J1 piece
    clock.at(10); _read(db, cam, 1, 0, job="J2")  # job change: the J1 piece is NOK
    assert _totals(db, st) == (0, 1)
    # J2 has no rule: its pictures are pieces
    clock.at(15); _read(db, cam, 2, 0, job="J2")
    db.expire_all()
    assert db.query(CounterState).filter_by(station_id=st.id, job_name="J2").one().total_pass == 1
    # stopped: an open piece is dropped
    clock.at(20); _read(db, cam, 3, 0, job="J1")
    stations_st = db.get(Station, st.id)
    production.stop(stations_st)
    db.commit()
    clock.at(25); _read(db, cam, 4, 0, job="J1")
    assert db.query(SourceState).filter_by(station_id=st.id).one().open_piece is None


def test_timer_on_a_polled_station(db, monkeypatch):
    clock = Clock(monkeypatch)
    cam, st = _setup(db, {"pictures": 2, "timeout_s": 30, "missing": "judge"})
    _read(db, cam, 0, 0)
    clock.at(5); _read(db, cam, 1, 0)
    clock.at(20); _read(db, cam, 1, 0)  # no new picture, timer not over
    assert _totals(db, st) == (0, 0)
    clock.at(40); _read(db, cam, 1, 0)  # timer over: judged on its one OK picture
    assert _totals(db, st) == (1, 0)


def test_api_and_export(client):
    login(client)
    db = SessionLocal()
    try:
        db.add(Job(name="J1"))
        db.commit()
        jid = db.query(Job).one().id
    finally:
        db.close()
    r = client.patch(f"/api/jobs/{jid}", json={"piece_rule": {"pictures": 2, "nok_closes": True, "timeout_s": 5}})
    assert r.status_code == 200, r.text
    assert r.json()["piece_rule"]["nok_closes"] is True and "a NOK ends the piece" in r.json()["piece_rule_text"]
    assert client.patch(f"/api/jobs/{jid}", json={"piece_rule": {"pictures": 0}}).status_code == 400
    # the cycle time stays when only the rule changes, and the other way round
    client.patch(f"/api/jobs/{jid}", json={"ideal_cycle_s": 3})
    listed = {j["name"]: j for j in client.get("/api/jobs").json()}["J1"]
    assert listed["ideal_cycle_s"] == 3 and listed["piece_rule"]["pictures"] == 2 and listed["open_pieces"] == []
    exported = client.get("/api/devices/export").json()
    assert exported["jobs"] == [{"name": "J1", "ideal_cycle_s": 3, "shot_s": 3, "pieces_per_shot": 1,
                                 "piece_rule": listed["piece_rule"]}]
    client.patch(f"/api/jobs/{jid}", json={"piece_rule": None})
    assert client.get("/api/jobs").json()[0]["piece_rule"] is None
    r = client.post("/api/devices/import", json={"version": 4, "cameras": [], "stations": [], "jobs": exported["jobs"]})
    assert r.status_code == 200, r.text
    assert client.get("/api/jobs").json()[0]["piece_rule"]["pictures"] == 2
