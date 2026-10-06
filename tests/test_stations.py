"""Stations: OK / NOK from different devices, the upgrade of 1.4 data, OEE."""
import datetime as dt
import socket
import time

import pytest
from sqlalchemy import text

from app import oee, stations
from app.database import SessionLocal, engine
from app.models import CounterState, Device, Job, Meta, NotificationRule, Reading, Station

from opcua_sim import SimServer
from test_api import add_station_device, login

UTC = dt.timezone.utc


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


@pytest.fixture(scope="module")
def sim():
    server = SimServer(_free_port()).start()
    yield server
    server.stop()


def _station(client, name, sources, **fields):
    r = client.post("/api/stations", json={"name": name, "sources": sources, **fields})
    assert r.status_code == 201, r.text
    return r.json()


def test_ok_from_opcua_nok_from_tcp_listener(client, sim):
    """Martin's case: OK counted by an OPC UA server, NOK pushed over TCP."""
    from app.poller import _listen_devices, listener_manager
    from app.protocols.tcp_listener import parse_port_range
    from app.config import settings

    login(client)
    opc = client.post("/api/devices", json={"name": "PLC", "host": "", "port": 0, "protocol": "opcua",
                                            "protocol_config": {"endpoint": sim.endpoint}}).json()
    lo, hi = parse_port_range(settings.listen_ports)
    port = next(p for p in range(lo, hi + 1) if _port_free(p))
    tcp = client.post("/api/devices", json={"name": "Reject counter", "host": "", "port": port,
                                            "protocol": "tcp_listen",
                                            "protocol_config": {"job_field": 0, "pass_field": 1,
                                                                "fail_field": 2}}).json()
    assert client.get("/api/stations").json() == []  # no station unless asked for
    st = _station(client, "M1", {"ok": {"device_id": opc["id"], "key": "ns=2;s=Line1.Pass"},
                                 "nok": {"device_id": tcp["id"], "key": "fail"},
                                 "job": {"device_id": opc["id"], "key": "ns=2;s=Line1.Job"}})
    sid = st["id"]
    assert not st["connected"] and "PLC" in st["problem"]

    def counters():
        c = [x for x in client.get(f"/api/stations/{sid}/counters").json() if x["is_active"]]
        return (c[0]["job_name"], c[0]["total_pass"], c[0]["total_fail"]) if c else None

    listener_manager.sync(_listen_devices())
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as dev:
            dev.sendall(b"X,0,4\r\n")  # NOK 4 before the OK side has a value: nothing recorded yet
            assert _wait(lambda: client.get("/api/devices").json()[1]["connected"])
            sim.set(Pass=100, Job="JOB_A")
            assert client.post(f"/api/devices/{opc['id']}/poll").status_code == 200
            assert counters() == ("JOB_A", 100, 4)  # first sample: the baseline
            sim.set(Pass=130)
            client.post(f"/api/devices/{opc['id']}/poll")
            dev.sendall(b"X,0,6\r\n")
            assert _wait(lambda: counters() == ("JOB_A", 130, 6)), counters()
            # the OK counter is reset on the PLC; the NOK side keeps counting
            sim.set(Pass=7)
            client.post(f"/api/devices/{opc['id']}/poll")
            dev.sendall(b"X,0,9\r\n")
            assert _wait(lambda: counters() == ("JOB_A", 137, 9)), counters()
            # and now the reject counter is reset
            dev.sendall(b"X,0,1\r\n")
            assert _wait(lambda: counters() == ("JOB_A", 137, 10)), counters()
            s = next(x for x in client.get("/api/stations").json() if x["id"] == sid)
            assert s["connected"] and s["problem"] is None
            summary = client.get("/api/data/summary").json()[0]
            assert summary["devices"] == ["PLC", "Reject counter"]
            assert (summary["active_job"]["total_pass"], summary["active_job"]["total_fail"]) == (137, 10)
            # a device used by a station cannot be deleted
            assert client.delete(f"/api/devices/{tcp['id']}").status_code == 409
        assert _wait(lambda: not client.get(f"/api/stations").json()[0]["connected"])
    finally:
        listener_manager.sync([])
    # OPC UA reads exactly the nodes its stations use
    assert stations.keys_for_device(SessionLocal(), opc["id"]) == {"ns=2;s=Line1.Pass", "ns=2;s=Line1.Job"}


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def test_station_sources_are_checked(client):
    login(client)
    assert client.post("/api/stations", json={"name": "x", "sources": {
        "ok": {"device_id": 99, "key": "pass"}}}).status_code == 400
    did, sid = add_station_device(client, "D")
    # a job without OK or NOK counts makes no sense (no devices at all is fine: manual entries)
    assert client.post("/api/stations", json={"name": "x", "sources": {
        "job": {"device_id": did, "key": "job"}}}).status_code == 400
    assert client.post("/api/stations", json={"name": "D", "sources": {
        "ok": {"device_id": did, "key": "pass"}}}).status_code == 409
    values = client.get("/api/devices/values").json()[0]
    assert [v["key"] for v in values["values"]] == ["pass", "fail", "count", "job"]
    # deleting a station takes its readings and rules, then the device can go
    client.post(f"/api/devices/{did}/poll")
    client.post("/api/notifications/rules", json={"name": "r", "condition": "scrap_rate", "station_id": sid})
    assert client.delete(f"/api/stations/{sid}").status_code == 204
    assert client.get("/api/notifications/rules").json() == []
    assert client.delete(f"/api/devices/{did}").status_code == 204


def test_missing_value_waits(client, sim):
    login(client)
    opc = client.post("/api/devices", json={"name": "PLC", "host": "", "port": 0, "protocol": "opcua",
                                            "protocol_config": {"endpoint": sim.endpoint}}).json()
    st = _station(client, "Bad node", {"ok": {"device_id": opc["id"], "key": "ns=2;s=Nope"}})
    r = client.post(f"/api/devices/{opc['id']}/poll").json()
    assert r["stations"] == [] and "ns=2;s=Nope" in r["errors"]["ns=2;s=Nope"]
    s = client.get("/api/stations").json()[0]
    assert s["id"] == st["id"] and not s["connected"] and "Nope" in s["problem"]


def _legacy_db():
    """The data of a 1.4 database: devices that were counted themselves."""
    db = SessionLocal()
    try:
        db.query(Meta).delete()
        cam = Device(name="Cam", host="sim", port=0, protocol="simulator", idle_timeout_min=20,
                     stats_default="exclude", current_job="J", manual_stop=True)
        opc = Device(name="Opc", host="x", port=4840, protocol="opcua",
                     protocol_config={"endpoint": "opc.tcp://x:4840", "pass_node": "ns=2;s=P",
                                      "fail_node": "ns=2;s=F", "default_job": "AUTO"})
        bare = Device(name="Bare", host="x", port=4840, protocol="opcua", protocol_config={})
        db.add_all([cam, opc, bare])
        db.flush()
        db.add(CounterState(device_id=cam.id, job_name="J", total_pass=10, total_fail=1, total_count=11))
        db.add(Reading(device_id=cam.id, job_name="J", total_pass=10, total_fail=1))
        db.add(NotificationRule(name="r", condition="scrap_rate", device_id=cam.id,
                                thresholds={str(cam.id): 0.2}))
        db.commit()
        return cam.id, opc.id, bare.id
    finally:
        db.close()


def test_upgrade_makes_a_station_per_device_with_the_same_id(client):
    login(client)
    cam_id, opc_id, bare_id = _legacy_db()
    assert stations.upgrade(engine) == 2
    assert stations.upgrade(engine) == 0  # once per database
    db = SessionLocal()
    try:
        st = db.get(Station, cam_id)
        assert (st.name, st.idle_timeout_min, st.stats_default, st.manual_stop, st.current_job) == (
            "Cam", 20, "exclude", True, "J")
        assert st.source("ok") == {"device_id": cam_id, "key": "pass"}
        o = db.get(Station, opc_id)
        assert (o.source("ok")["key"], o.source("nok")["key"], o.source("job"), o.default_job) == (
            "ns=2;s=P", "ns=2;s=F", None, "AUTO")
        assert db.get(Station, bare_id) is None  # nothing to count yet
        assert db.query(CounterState).one().station_id == cam_id
        assert db.query(Reading).one().station_id == cam_id
        assert db.query(NotificationRule).one().station_id == cam_id
    finally:
        db.close()
    assert client.get("/api/data/summary").json()[0]["active_job"]["total_pass"] == 10
    # new stations do not collide with the copied ids
    assert client.post("/api/stations", json={"name": "New", "sources": {
        "ok": {"device_id": cam_id, "key": "pass"}}}).json()["id"] > opc_id


def test_restoring_a_1_4_backup_makes_stations(client, tmp_path, monkeypatch):
    from app import backup
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    login(client)
    cam_id, _, _ = _legacy_db()
    with engine.begin() as conn:  # as 1.4 wrote it: no stations, no meta
        conn.execute(text("UPDATE readings SET station_id = NULL"))
    path = tmp_path / "old.json.gz"
    backup.write_backup(path)
    backup.restore(path)
    db = SessionLocal()
    try:
        assert db.get(Station, cam_id).name == "Cam"
        assert db.query(Reading).one().station_id == cam_id
    finally:
        db.close()


def test_oee_last_24_hours(client):
    """Availability = production time / 24 h, performance = the job's ideal
    cycle x parts / production time, quality = OK / (OK + NOK)."""
    login(client)
    now = dt.datetime(2026, 10, 6, 12, tzinfo=UTC)
    db = SessionLocal()
    try:
        a = Station(name="A", sources={}, idle_timeout_min=30)
        b = Station(name="B", sources={}, idle_timeout_min=30)  # runs a job without a cycle time
        x = Station(name="X", sources={}, stats_default="exclude")
        db.add_all([a, b, x, Job(name="J", ideal_cycle_s=30), Job(name="K")])
        db.flush()
        for st in (a, b, x):
            # 6 hours in production, a reading every 10 minutes: 4 parts each,
            # 1 of them NOK every hour
            t = now - dt.timedelta(hours=7)
            p = f = 0
            for i in range(37):
                db.add(Reading(station_id=st.id, job_name="K" if st is b else "J", total_pass=p, total_fail=f, in_production=True,
                               created_at=t + dt.timedelta(minutes=10 * i)))
                p, f = p + (3 if i % 6 == 5 else 4), f + (1 if i % 6 == 5 else 0)
        db.commit()
        r = oee.compute(db, hours=24, now=now)
    finally:
        db.close()
    a_row = next(s for s in r["stations"] if s["name"] == "A")
    assert a_row["production_s"] == 6 * 3600
    assert (a_row["ok"], a_row["nok"]) == (138, 6)
    assert a_row["availability"] == 0.25
    assert a_row["performance"] == round(30 * 144 / (6 * 3600), 4)
    assert a_row["quality"] == round(138 / 144, 4)
    assert a_row["oee"] == round(a_row["availability"] * a_row["performance"] * a_row["quality"], 4)
    b_row = next(s for s in r["stations"] if s["name"] == "B")
    assert b_row["performance"] is None and b_row["oee"] is None and b_row["quality"] == a_row["quality"]
    # overall: stations in the statistics; OEE from those with a cycle time
    assert r["totals"] == {"ok": 276, "nok": 12, "total": 288, "quality": round(276 / 288, 4)}
    assert r["overall"]["oee"] == a_row["oee"] and r["overall"]["without_cycle_time"] == ["B"]
    assert r["overall"]["jobs_without_cycle_time"] == ["K"] and b_row["jobs_without_cycle_time"] == ["K"]


def test_manual_entries(client):
    """Entered by hand: they count like device data, also back-dated, can be
    edited, excluded and deleted, and need their own permission."""
    login(client)
    did, sid = add_station_device(client, "M2", {"jobs": ["J"], "parts_per_poll": 10, "fail_ratio": 0.0,
                                                 "reset_every": 0, "job_change_every": 0})
    client.post(f"/api/stations/{sid}/production/start")
    for _ in range(3):  # baseline, then +10, +10 from the device
        client.post(f"/api/devices/{did}/poll")
    r = client.post(f"/api/stations/{sid}/entries", json={"ok": 5, "nok": 2, "note": "hand check"})
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]
    two_h_ago = (dt.datetime.now(UTC) - dt.timedelta(hours=2)).isoformat()
    assert client.post(f"/api/stations/{sid}/entries", json={"ok": 0, "nok": 3, "at": two_h_ago}).status_code == 201
    client.post(f"/api/devices/{did}/poll")  # the device keeps counting on top: +10

    job = client.get("/api/data/summary").json()[0]["active_job"]
    assert (job["total_pass"], job["total_fail"]) == (40 + 5, 0 + 5)
    today = dt.date.today().isoformat()
    s = client.get("/api/data/scrap", params={"from": today, "to": today, "tz": "UTC"}).json()
    if (dt.datetime.now(UTC) - dt.timedelta(hours=2)).date() == dt.datetime.now(UTC).date():
        assert (s["overall"]["pass"], s["overall"]["fail"]) == (30 + 5, 5)
    o = oee.compute(SessionLocal(), hours=24)
    assert (o["totals"]["ok"], o["totals"]["nok"]) == (35, 5)
    view = client.get(f"/api/data/stations/{sid}?hours=8").json()["history"]
    assert (view["ok"], view["nok"]) == (35, 5)

    rows = client.get("/api/data/readings", params={"station_id": sid}).json()
    entry = next(x for x in rows if x["id"] == entry_id)
    assert entry["manual"] and entry["entered_by"] == "Admin" and entry["note"] == "hand check"
    # edit, exclude, delete
    assert client.put(f"/api/data/readings/{entry_id}/entry", json={"ok": 7, "nok": 0, "note": "x"}).status_code == 200
    assert client.get("/api/data/summary").json()[0]["active_job"]["total_pass"] == 47
    client.patch(f"/api/data/readings/{entry_id}", json={"excluded": True})
    assert oee.compute(SessionLocal(), hours=24)["totals"]["ok"] == 30
    assert client.delete(f"/api/data/readings/{entry_id}").status_code == 204
    assert client.get("/api/data/summary").json()[0]["active_job"]["total_pass"] == 40
    device_reading = next(x for x in rows if not x["manual"])
    assert client.delete(f"/api/data/readings/{device_reading['id']}").status_code == 404

    # checks and permission
    assert client.post(f"/api/stations/{sid}/entries", json={"ok": 0, "nok": 0}).status_code == 400
    future = (dt.datetime.now(UTC) + dt.timedelta(hours=1)).isoformat()
    assert client.post(f"/api/stations/{sid}/entries", json={"ok": 1, "at": future}).status_code == 400
    client.post("/api/users", json={"username": "op", "password": "pw", "permissions": ["view_dashboard"]})
    client.get("/logout")
    login(client, "op", "pw")
    assert client.post(f"/api/stations/{sid}/entries", json={"ok": 1}).status_code == 403


def test_station_fed_only_by_manual_entries(client):
    login(client)
    st = client.post("/api/stations", json={"name": "Manual", "sources": {}, "default_job": "HAND"}).json()
    assert st["connected"] and st["problem"] is None
    client.post(f"/api/stations/{st['id']}/entries", json={"ok": 90, "nok": 10})
    s = client.get("/api/data/summary").json()[0]
    assert s["devices"] == [] and s["current_job"] == "HAND"
    assert (s["active_job"]["total_pass"], s["active_job"]["total_fail"], s["active_job"]["scrap_rate"]) == (90, 10, 0.1)
    assert s["production_state"] == "running"
    today = dt.date.today().isoformat()
    sc = client.get("/api/data/scrap", params={"from": today, "to": today, "tz": "UTC"}).json()
    assert sc["per_station"][0]["total"] == 100
    # it exports and imports like any station
    data = client.get("/api/devices/export").json()
    assert data["stations"][0]["sources"] == {"ok": None, "nok": None, "count": None, "job": None}
    assert client.post("/api/devices/import", json=data).status_code == 200
