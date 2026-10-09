"""Ping devices (response times, the slow-response alert) and the Map tab."""
import socket
import threading

import httpx
import pytest

from app import notifications, ping
from app.database import SessionLocal
from app.models import Device, NotificationLog, Station
from app.protocols.tcp_listener import last_peer

from test_api import add_station_device


def login(client, user="Admin", pw="1234"):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text


class _Resp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True}


@pytest.fixture()
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: calls.append(kw.get("json", {}).get("message")) or _Resp())
    return calls


def _viewer(client):
    client.post("/api/users", json={"username": "op", "password": "pw123456", "permissions": ["view_dashboard"]})
    client.get("/logout")
    login(client, "op", "pw123456")


def test_ping_setting(client):
    login(client)
    assert client.get("/api/ui-settings").json()["ping"] == {"enabled": True, "interval_s": 30}
    assert client.put("/api/ui-settings/ping", json={"interval_s": 1}).json() == {"enabled": True, "interval_s": 5}
    assert client.put("/api/ui-settings/ping", json={"enabled": "x"}).status_code == 400
    assert client.put("/api/ui-settings/ping", json={"enabled": False}).json()["enabled"] is False
    ping._settings = None  # read back after a restart
    assert ping.load() == {"enabled": False, "interval_s": 5}
    _viewer(client)
    assert client.put("/api/ui-settings/ping", json={"enabled": True}).status_code == 403


def test_tcp_fallback_and_targets(monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept()[0].close(), daemon=True).start()

    def no_icmp(host):
        raise PermissionError("not allowed")

    monkeypatch.setattr(ping, "_icmp", no_icmp)
    monkeypatch.setattr(ping, "_icmp_ok", None)
    ms, method = ping.measure("127.0.0.1", port)
    assert method == "tcp" and ms is not None and ms < 1000
    srv.close()
    assert ping.target(Device(id=1, name="a", host="10.0.0.5", port=502, protocol="modbus")) == ("10.0.0.5", 502)
    assert ping.target(Device(id=2, name="b", host="sim", port=0, protocol="simulator")) == (None, None)
    assert ping.target(Device(id=3, name="c", host="", port=4840, protocol="opcua",
                              protocol_config={"endpoint": "opc.tcp://10.0.0.9:4841"})) == ("10.0.0.9", 4841)
    listener = Device(id=4, name="d", host="0.0.0.0", port=5100, protocol="tcp_listen")
    assert ping.target(listener) == (None, None)  # nothing sent yet: nothing to ping
    last_peer[4] = "10.0.0.7"
    assert ping.target(listener) == ("10.0.0.7", None)
    last_peer.pop(4)


def test_device_api_shows_the_ping(client, monkeypatch):
    login(client)
    did, sid = add_station_device(client, "Line 1", {"jobs": ["J"]})
    assert client.get("/api/devices").json()[0]["ping"] is None
    monkeypatch.setattr(ping, "target", lambda d: ("10.0.0.5", 502))
    monkeypatch.setattr(ping, "measure", lambda host, port: (12.34, "icmp"))
    db = SessionLocal()
    try:
        ping.ping_device(db.get(Device, did))
    finally:
        db.close()
    p = client.get("/api/devices").json()[0]["ping"]
    assert p["ms"] == 12.3 and p["method"] == "icmp" and p["target"] == "10.0.0.5" and p["ok"] is True
    assert client.get(f"/api/data/stations/{sid}").json()["sources"][0]["ping"]["ms"] == 12.3
    client.put("/api/ui-settings/ping", json={"enabled": False})
    assert client.get("/api/devices").json()[0]["ping"] is None


def _results(did, *values):
    ping._results[did] = {"ms": values[-1], "ok": values[-1] is not None, "method": "icmp", "target": "x",
                          "at": None, "recent": list(values)}


def _check():
    db = SessionLocal()
    try:
        notifications.check_ping(db)
    finally:
        db.close()


def test_slow_response_alert_and_back_to_normal(client, sent):
    login(client)
    client.post("/api/notifications/providers", json={
        "name": "hook", "kind": "whatsapp", "config": {"transport": "webhook", "url": "http://fake/send"}})
    r = client.post("/api/notifications/rules", json={"name": "slow", "condition": "slow_response", "cooldown": 600,
                                                       "severity": "warning"})
    assert r.status_code == 201 and r.json()["threshold"] in (0, 0.0)  # 0 = the default 300 ms
    did, sid = add_station_device(client, "Line 1", {"jobs": ["J"]})
    client.post(f"/api/stations/{sid}/production/start")

    _results(did, 120, 400, None)  # not all three bad: nothing
    _check()
    assert sent == []
    _results(did, 400, None, 350)
    _check()
    assert len(sent) == 1 and "responds slowly" in sent[0] and "400 ms, no reply, 350 ms" in sent[0], sent
    _check()  # still slow, within the cooldown: not again
    assert len(sent) == 1
    _results(did, None, 350, 40)
    _check()
    assert len(sent) == 2 and "responds normally again: 40 ms" in sent[1]
    _check()  # back to normal is sent once
    assert len(sent) == 2
    _results(did, None, None, None)
    _check()
    assert "does not answer pings" in sent[-1]


def test_slow_response_skipped_when_idle_or_muted(client, sent):
    login(client)
    client.post("/api/notifications/providers", json={
        "name": "hook", "kind": "whatsapp", "config": {"transport": "webhook", "url": "http://fake/send"}})
    client.post("/api/notifications/rules", json={"name": "slow", "condition": "slow_response", "cooldown": 0,
                                                  "threshold": 100})
    did, sid = add_station_device(client, "Line 1", {"jobs": ["J"]})
    _results(did, 150, 200, 300)
    _check()  # idle station: skipped, logged
    assert sent == []
    db = SessionLocal()
    try:
        assert db.query(NotificationLog).filter(NotificationLog.skipped.is_(True)).count() == 1
        st = db.get(Station, sid)
        st.alerts_muted = True
        db.commit()
    finally:
        db.close()
    client.post(f"/api/stations/{sid}/production/start")
    _check()  # muted: nothing
    assert sent == []


def test_rule_inactive_while_ping_is_off(client):
    login(client)
    cond = lambda: next(c for c in client.get("/api/notifications/conditions").json()["conditions"]
                        if c["key"] == "slow_response")
    assert cond()["inactive"] is False and cond()["default_threshold"] == 300
    client.put("/api/ui-settings/ping", json={"enabled": False})
    assert cond()["inactive"] is True


def test_map_graph_and_layout(client):
    login(client)
    did, sid = add_station_device(client, "Line 1", {"jobs": ["J"]})
    client.post(f"/api/devices/{did}/poll")
    g = client.get("/api/map").json()
    assert g["server"]["name"] == "Scrap Meter" and g["ping"]["enabled"] is True
    assert [d["name"] for d in g["devices"]] == ["Line 1"] and g["devices"][0]["status"]["state"] == "ok"
    assert g["stations"][0]["name"] == "Line 1" and g["links"] == [{"station_id": sid, "device_id": did}]
    r = client.put("/api/map/layout", json={"positions": {f"d{did}": [10, 20.55], "server": [0, 0], "junk": [1, 1]}})
    assert r.json() == {f"d{did}": [10.0, 20.6], "server": [0.0, 0.0]}
    g = client.get("/api/map").json()
    assert g["devices"][0]["pos"] == [10.0, 20.6] and g["stations"][0]["pos"] is None
    client.put("/api/map/layout", json={"positions": {"server": None}})
    assert client.get("/api/map").json()["server"]["pos"] is None
    assert client.delete("/api/map/layout").status_code == 200
    assert client.get("/api/map").json()["devices"][0]["pos"] is None
    assert client.get("/map").status_code == 200 and 'href="/map"' in client.get("/").text
    _viewer(client)
    assert client.get("/api/map").status_code == 200
    assert client.put("/api/map/layout", json={"positions": {"server": [1, 1]}}).status_code == 403


def test_device_status_codes():
    from app.netmap import device_status

    assert device_status(Device(name="a", enabled=False))["code"] == "W03"
    assert device_status(Device(name="a", enabled=True, connected=False))["code"] == "W01"
    s = device_status(Device(name="a", enabled=True, connected=False, last_error="TCP read failed: timed out"))
    assert s["state"] == "error" and s["code"].startswith("E")
