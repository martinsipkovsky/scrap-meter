"""System tab: live values, the samples behind the graphs, admins only."""
import datetime as dt

from app import system_info
from app.database import SessionLocal
from app.models import SystemSample, utcnow

from test_api import add_station_device


def login(client, user="Admin", pw="1234"):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text


def test_live_values(client):
    login(client)
    add_station_device(client, "Line 1", {"jobs": ["J"]})
    s = client.get("/api/system").json()
    assert 0 <= s["cpu"]["percent"] <= 100 and s["cpu"]["cores"] >= 1
    assert s["memory"]["total"] > 0 and s["memory"]["app_rss"] > 0
    assert s["disks"][0]["path"] == "/" and s["disks"][0]["total"] > 0
    assert s["database"]["connected"] and s["database"]["server"].startswith("SQLite")
    assert s["database"]["size_bytes"] > 0
    assert any(n["name"] == "lo" for n in s["network"]["interfaces"])
    assert s["host"]["app_version"] and s["host"]["python"]
    assert s["poller"]["devices"] == 1 and s["poller"]["running"] is False  # POLL_ENABLED=false in tests
    # the second call has a network rate (bytes per second since the first)
    assert client.get("/api/system").json()["network"]["rx_rate"] is not None


def test_samples_history_and_cleanup(client):
    login(client)
    db = SessionLocal()
    try:
        db.add(SystemSample(created_at=utcnow() - dt.timedelta(days=8), cpu_percent=1))
        db.add(SystemSample(created_at=utcnow() - dt.timedelta(hours=2), cpu_percent=50))
        db.commit()
        row = system_info.take_sample(db)  # also deletes rows older than 7 days
        assert row.mem_percent > 0 and row.db_bytes > 0
        assert db.query(SystemSample).count() == 2
    finally:
        db.close()
    h = client.get("/api/system/history", params={"hours": 1}).json()
    assert len(h["points"]) == 1 and h["sample_s"] == 60
    h = client.get("/api/system/history", params={"hours": 24}).json()
    assert [round(p["cpu_percent"]) for p in h["points"]][0] == 50 and len(h["points"]) == 2


def test_admins_only(client):
    login(client)
    assert client.get("/system").status_code == 200
    assert 'href="/system"' in client.get("/").text
    client.post("/api/users", json={"username": "op", "password": "pw123456", "permissions": ["view_dashboard"]})
    client.get("/logout")
    login(client, "op", "pw123456")
    assert client.get("/api/system").status_code == 403
    assert client.get("/api/system/history").status_code == 403
    assert client.get("/system").status_code == 403
    assert 'href="/system"' not in client.get("/").text
