"""HMI windows: device web pages on the station view (app.hmi)."""
import gzip
import http.server
import threading

import httpx
import pytest

from app import hmi
from test_api import add_station_device, login

APP = "http://scrapmeter.local:8000"


def _station(client, **fields):
    r = client.post("/api/stations", json={"name": "M1", "sources": [], **fields})
    assert r.status_code == 201, r.text
    return r.json()


def test_windows_are_saved_in_order(client):
    login(client)
    st = _station(client, hmi_windows=[
        {"name": " Camera 1 ", "url": "192.168.0.10/hmi", "height": 700},
        {"name": "PLC", "url": "https://10.0.0.5:8443/"},
    ])
    assert st["hmi_windows"] == [
        {"name": "Camera 1", "url": "http://192.168.0.10/hmi", "height": 700},
        {"name": "PLC", "url": "https://10.0.0.5:8443/", "height": None},
    ]
    sid = st["id"]
    # reorder; a change of other settings keeps the list
    r = client.patch(f"/api/stations/{sid}", json={"hmi_windows": list(reversed(st["hmi_windows"]))})
    assert [w["name"] for w in r.json()["hmi_windows"]] == ["PLC", "Camera 1"]
    r = client.patch(f"/api/stations/{sid}", json={"idle_timeout_min": 20})
    assert [w["name"] for w in r.json()["hmi_windows"]] == ["PLC", "Camera 1"]
    # the station view has them
    view = client.get(f"/api/data/stations/{sid}").json()
    assert [w["name"] for w in view["hmi_windows"]] == ["PLC", "Camera 1"]
    # an empty list removes them
    assert client.patch(f"/api/stations/{sid}", json={"hmi_windows": []}).json()["hmi_windows"] == []


@pytest.mark.parametrize("window", [
    {"name": "x", "url": "javascript:alert(1)"},
    {"name": "x", "url": "ftp://10.0.0.5/"},
    {"name": "x", "url": "http://"},
    {"name": "x", "url": "http//10.0.0.5"},
    {"name": "x", "url": "10.0.0.5:99999"},
    {"name": "", "url": "http://10.0.0.5/"},
    {"name": "x", "url": "  "},
    {"name": "x", "url": "http://10.0.0.5/", "height": 50},
])
def test_bad_windows_are_refused(client, window):
    login(client)
    r = client.post("/api/stations", json={"name": "M1", "sources": [], "hmi_windows": [window]})
    assert r.status_code == 422, r.text


def test_viewers_see_the_windows_but_cannot_edit(client):
    login(client)
    sid = _station(client, hmi_windows=[{"name": "Cam", "url": "http://10.0.0.5/"}])["id"]
    client.post("/api/users", json={"username": "viewer", "password": "pw", "is_admin": False,
                                    "permissions": ["view_dashboard"]})
    client.get("/logout")
    login(client, "viewer", "pw")
    assert client.get(f"/api/data/stations/{sid}").json()["hmi_windows"][0]["name"] == "Cam"
    assert client.get("/api/stations").json()[0]["hmi_windows"][0]["name"] == "Cam"
    assert client.patch(f"/api/stations/{sid}", json={"hmi_windows": []}).status_code == 403


def test_export_import_and_backup(client):
    login(client)
    _, sid = add_station_device(client, "A")
    wins = [{"name": "Cam A", "url": "http://10.0.0.7/", "height": 500}]
    client.patch(f"/api/stations/{sid}", json={"hmi_windows": wins})
    data = client.get("/api/devices/export").json()
    assert data["stations"][0]["hmi_windows"] == wins

    # a file without the list (1.12 or older) keeps the station's windows
    old = {**data, "stations": [{k: v for k, v in data["stations"][0].items() if k != "hmi_windows"}]}
    assert client.post("/api/devices/import", json=old).status_code == 200
    assert client.get("/api/stations").json()[0]["hmi_windows"] == wins
    # a file with the list sets it
    data["stations"][0]["hmi_windows"] = [{"name": "New", "url": "http://10.0.0.8/"}]
    assert client.post("/api/devices/import", json=data).status_code == 200
    assert client.get("/api/stations").json()[0]["hmi_windows"][0]["name"] == "New"
    # a bad address fails the whole import
    data["stations"][0]["hmi_windows"] = [{"name": "Bad", "url": "file:///etc/passwd"}]
    assert client.post("/api/devices/import", json=data).status_code == 422
    assert client.get("/api/stations").json()[0]["hmi_windows"][0]["name"] == "New"

    # backups carry them
    saved = client.get("/api/database/backup/download").content
    assert b'"New"' in gzip.decompress(saved)
    client.patch(f"/api/stations/{sid}", json={"hmi_windows": []})
    info = client.post("/api/database/backup/inspect", files={"file": ("b.json.gz", saved, "application/gzip")}).json()
    assert client.post("/api/database/backup/import", json={"token": info["token"], "confirm": True}).status_code == 200
    login(client)
    assert client.get("/api/stations").json()[0]["hmi_windows"][0]["name"] == "New"


@pytest.mark.parametrize("headers,blocked", [
    ({}, False),
    ({"X-Frame-Options": "DENY"}, True),
    ({"X-Frame-Options": "sameorigin"}, True),
    ({"X-Frame-Options": "ALLOW-FROM http://x"}, False),  # browsers ignore ALLOW-FROM
    ({"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'"}, True),
    ({"Content-Security-Policy": "frame-ancestors 'self'"}, True),
    ({"Content-Security-Policy": "frame-ancestors *"}, False),
    ({"Content-Security-Policy": "frame-ancestors http://scrapmeter.local:8000"}, False),
    ({"Content-Security-Policy": "frame-ancestors scrapmeter.local:*"}, False),
    ({"Content-Security-Policy": "frame-ancestors *.local:*"}, False),
    ({"Content-Security-Policy": "frame-ancestors *.local"}, True),  # port 80 only
    ({"Content-Security-Policy": "frame-ancestors https://scrapmeter.local"}, True),
    ({"Content-Security-Policy": "frame-ancestors http://other:8000"}, True),
    # frame-ancestors wins over X-Frame-Options
    ({"Content-Security-Policy": "frame-ancestors *", "X-Frame-Options": "DENY"}, False),
])
def test_framing(headers, blocked):
    reason = hmi.framing(httpx.Headers(headers), APP, "http://10.0.0.5/")
    assert (reason is not None) == blocked, reason


def test_sameorigin_on_the_same_address_is_allowed():
    assert hmi.framing({"X-Frame-Options": "SAMEORIGIN"}, "http://10.0.0.5", "http://10.0.0.5:80/hmi") is None


class _Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        if self.path.startswith("/deny"):
            self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h1>HMI</h1>")

    def log_message(self, *args):
        pass


@pytest.fixture()
def device():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_check_endpoint(client, device):
    login(client)
    sid = _station(client, hmi_windows=[
        {"name": "ok", "url": device + "/"},
        {"name": "deny", "url": device + "/deny"},
        {"name": "gone", "url": "http://127.0.0.1:1/"},
    ])["id"]
    check = lambda i: client.get(f"/api/stations/{sid}/hmi/{i}/check", params={"origin": APP})
    assert check(0).json() == {"reachable": True, "status": 200, "blocked": None, "error": None}
    r = check(1).json()
    assert r["reachable"] and "DENY" in r["blocked"]
    r = check(2).json()
    assert not r["reachable"] and r["error"]
    assert check(3).status_code == 404
    assert client.get(f"/api/stations/{sid}/hmi/0/check", params={"origin": "javascript:x"}).status_code == 400
