"""Station comments and the read-only views for reports (Power BI)."""
from sqlalchemy import text

from app import reporting
from app.database import engine

from test_api import login


def _station(client, name="M1"):
    r = client.post("/api/stations", json={"name": name, "sources": {}, "default_job": "JOB-A"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_comment_keeps_a_snapshot_of_the_station(client):
    login(client)
    sid = _station(client)
    assert client.post(f"/api/stations/{sid}/entries", json={"ok": 90, "nok": 10}).status_code == 201

    r = client.post(f"/api/stations/{sid}/comments", json={"text": "  Tool changed on line 2  "})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["text"] == "Tool changed on line 2"
    assert (c["station_name"], c["job"], c["ok"], c["nok"], c["scrap_rate"], c["author"]) == \
        ("M1", "JOB-A", 90, 10, 0.1, "Admin")

    # later counts and a rename don't change the comment
    client.post(f"/api/stations/{sid}/entries", json={"ok": 0, "nok": 100})
    client.patch(f"/api/stations/{sid}", json={"name": "M1 new"})
    client.post(f"/api/stations/{sid}/comments", json={"text": "second"})
    rows = client.get(f"/api/stations/{sid}/comments").json()
    assert [x["text"] for x in rows] == ["second", "Tool changed on line 2"]  # newest first
    assert rows[1]["nok"] == 10 and rows[1]["station_name"] == "M1"
    assert rows[0]["nok"] == 110 and rows[0]["station_name"] == "M1 new"

    # since 1.15 comments stay on the station view, not the dashboard blocks
    summary = {s["id"]: s for s in client.get("/api/data/summary").json()}
    assert "latest_comment" not in summary[sid]


def test_comment_validation_and_permissions(client):
    login(client)
    sid = _station(client)
    other = _station(client, "M2")
    assert client.post(f"/api/stations/{sid}/comments", json={"text": "   "}).status_code == 400
    assert client.post(f"/api/stations/{sid}/comments", json={"text": ""}).status_code == 422
    assert client.post(f"/api/stations/{sid}/comments", json={"text": "x" * 2001}).status_code == 422
    assert client.post("/api/stations/999/comments", json={"text": "hi"}).status_code == 404

    client.post("/api/users", json={"username": "op", "password": "pw123456", "permissions": ["view_dashboard"]})
    client.post("/api/users", json={"username": "nobody", "password": "pw123456", "permissions": []})
    login(client, "nobody", "pw123456")
    assert client.post(f"/api/stations/{sid}/comments", json={"text": "hi"}).status_code == 403
    assert client.get(f"/api/stations/{sid}/comments").status_code == 403

    # anyone who sees the dashboard may comment, only an admin may delete
    login(client, "op", "pw123456")
    r = client.post(f"/api/stations/{other}/comments", json={"text": "operator note"})
    assert r.status_code == 201 and r.json()["author"] == "op"
    assert client.delete(f"/api/comments/{r.json()['id']}").status_code == 403
    login(client)
    assert client.delete(f"/api/comments/{r.json()['id']}").status_code == 204
    assert client.delete(f"/api/comments/{r.json()['id']}").status_code == 404
    assert client.get(f"/api/stations/{other}/comments").json() == []


def test_comments_outlive_their_station(client):
    login(client)
    sid = _station(client)
    client.post(f"/api/stations/{sid}/comments", json={"text": "kept for reports"})
    assert client.delete(f"/api/stations/{sid}").status_code == 204
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT station_name, comment FROM powerbi_station_comments")).all()
    assert rows == [("M1", "kept for reports")]


def test_powerbi_views(client):
    login(client)
    sid = _station(client)
    client.post(f"/api/stations/{sid}/entries", json={"ok": 3, "nok": 1, "note": "hand check"})
    client.post(f"/api/stations/{sid}/comments", json={"text": "hello"})
    reporting.ensure_views(engine)  # again: must be repeatable
    with engine.connect() as conn:
        c = conn.execute(text("SELECT station_name, job, ok_count, nok_count, scrap_pct, comment, author "
                              "FROM powerbi_station_comments")).one()
        assert tuple(c) == ("M1", "JOB-A", 3, 1, 25, "hello", "Admin")
        r = conn.execute(text("SELECT station_name, job, manual_entry, raw_ok, raw_nok, note "
                              "FROM powerbi_readings")).one()
        assert tuple(r) == ("M1", "JOB-A", 1, 3, 1, "hand check")
        t = conn.execute(text("SELECT station_name, job, ok_count, nok_count FROM powerbi_job_totals")).one()
        assert tuple(t) == ("M1", "JOB-A", 3, 1)
        assert conn.execute(text("SELECT station_name FROM powerbi_stations")).scalar_one() == "M1"
        conn.execute(text("SELECT * FROM powerbi_devices")).all()


def test_database_page_reading_info(client):
    login(client)
    r = client.get("/api/database/reading")
    assert r.status_code == 200
    info = r.json()
    assert not info["postgres"] and info["reader"] is None  # tests run on SQLite
    assert {v["name"] for v in info["views"]} == set(reporting.VIEWS)
    assert client.post("/api/database/reading/password").status_code == 404

    client.post("/api/users", json={"username": "op", "password": "pw123456", "permissions": ["view_dashboard"]})
    login(client, "op", "pw123456")
    assert client.get("/api/database/reading").status_code == 403
    assert client.post("/api/database/reading/password").status_code == 403
