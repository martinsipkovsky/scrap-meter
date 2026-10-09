"""The Settings tab (per-user look and behaviour, admin defaults) and the
problem codes on the dashboard."""
from app import ui_settings
from app.auth import hash_password
from app.database import SessionLocal
from app.models import Device, Station, User
from app.stations import PROBLEM_CODES, problem_code

from test_api import add_station_device


def login(client, user="Admin", pw="1234"):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def _user(name, **fields):
    db = SessionLocal()
    try:
        db.add(User(username=name, password_hash=hash_password("pw-" + name), is_active=True, **fields))
        db.commit()
    finally:
        db.close()


def _reset_defaults():
    ui_settings.save_defaults({})


def test_settings_defaults_and_own_choices(client):
    _reset_defaults()
    login(client)
    s = client.get("/api/ui-settings").json()
    assert s["effective"] == ui_settings.builtin()
    assert s["effective"]["mode"] == "dark" and s["effective"]["date_format"] == "dmy"
    assert s["can_set_defaults"] is True

    # an administrator sets defaults for everyone; unknown keys and values are dropped
    r = client.put("/api/ui-settings/defaults", json={"mode": "light", "refresh_s": "10", "accent": "pink", "x": 1})
    assert r.status_code == 200
    assert r.json()["admin_defaults"] == {"mode": "light", "refresh_s": 10}

    _user("viewer", is_admin=False, permissions=["view_dashboard"])
    client.get("/logout")
    login(client, "viewer", "pw-viewer")
    s = client.get("/api/ui-settings").json()
    assert s["effective"]["mode"] == "light" and s["effective"]["refresh_s"] == 10
    assert s["can_set_defaults"] is False
    assert client.put("/api/ui-settings/defaults", json={"mode": "dark"}).status_code == 403

    # the user's own choice wins over the default; leaving it out follows the default again
    r = client.put("/api/ui-settings/mine", json={"mode": "system", "accent": "teal", "date_format": "ymd"})
    assert r.json()["effective"]["mode"] == "system" and r.json()["effective"]["accent"] == "teal"
    page = client.get("/settings").text
    assert 'data-mode="system"' in page and 'data-accent="teal"' in page
    assert '"date_format": "ymd"' in page  # window.SM_UI for the page scripts
    r = client.put("/api/ui-settings/mine", json={"accent": "teal"})
    assert r.json()["effective"]["mode"] == "light"
    _reset_defaults()


def test_start_page_after_login(client):
    _reset_defaults()
    _user("statsman", is_admin=False, permissions=["view_dashboard", "view_data"])
    login(client, "statsman", "pw-statsman")
    client.put("/api/ui-settings/mine", json={"start_page": "/scrap"})
    client.get("/logout")
    assert login(client, "statsman", "pw-statsman").headers["location"] == "/scrap"
    # a start page the user may not open falls back to the dashboard
    client.put("/api/ui-settings/mine", json={"start_page": "/chat"})
    client.get("/logout")
    assert login(client, "statsman", "pw-statsman").headers["location"] == "/"


def test_problem_codes():
    assert problem_code("Listening on TCP port 5102, waiting for the device to connect") == "W01"
    assert problem_code("Could not connect to SLMP device 10.0.0.5:5000: [Errno 111] Connection refused") == "E02"
    assert problem_code("TCP read failed: timed out") == "E03"
    assert problem_code("PLC closed the SLMP connection") == "E04"
    assert problem_code("Set the server's endpoint URL, e.g. opc.tcp://10.0.0.5:4840") == "E05"
    assert problem_code("Node ns=2;s=X not found: BadNodeIdUnknown") == "E06"
    assert problem_code("[Errno 98] Address already in use") == "E07"
    assert problem_code("something odd") == "E09"
    assert all(c[0] in "EW" and len(c) == 3 for c in PROBLEM_CODES)


def test_dashboard_problems(client):
    login(client)
    did, sid = add_station_device(client, "Line 1", {"jobs": ["J1"]})
    db = SessionLocal()
    try:
        d = db.get(Device, did)
        d.connected, d.last_error, d.last_poll_at = False, None, None  # added, never read
        db.commit()
    finally:
        db.close()
    row = next(r for r in client.get("/api/data/summary").json() if r["id"] == sid)
    assert [(p["code"], p["level"]) for p in row["problems"]] == [("W01", "waiting")]
    db = SessionLocal()
    try:
        d = db.get(Device, did)
        d.connected, d.last_error = False, "Listening on TCP port 5102, waiting for the device to connect"
        db.commit()
    finally:
        db.close()
    row = next(r for r in client.get("/api/data/summary").json() if r["id"] == sid)
    assert row["problems"] == [{"code": "W01", "level": "waiting",
                                "message": "Device 'Line 1': Listening on TCP port 5102, waiting for the device to connect"}]
    db = SessionLocal()
    try:
        d = db.get(Device, did)
        d.last_error = "TCP read failed: timed out"
        db.commit()
    finally:
        db.close()
    row = next(r for r in client.get("/api/data/summary").json() if r["id"] == sid)
    assert [(p["code"], p["level"]) for p in row["problems"]] == [("E03", "error")]
    assert row["last_error"] == "Device 'Line 1': TCP read failed: timed out"
    db = SessionLocal()
    try:
        assert db.get(Station, sid) is not None
    finally:
        db.close()
