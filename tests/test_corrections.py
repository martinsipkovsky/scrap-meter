"""Corrections: manual entries with negative parts take back falsely counted
parts everywhere entries count, ask before taking a day below zero, and never
fire a scrap alert."""
import datetime as dt

from sqlalchemy import text

from app import daily, oee
from app.database import SessionLocal

from test_api import login
from test_notifications import _provider, sent  # noqa: F401 - fixture

UTC = dt.timezone.utc


def _manual_station(client, name="Hand", job="HAND"):
    return client.post("/api/stations", json={"name": name, "sources": {}, "default_job": job}).json()["id"]


def test_negative_entries_subtract_everywhere(client):
    login(client)
    sid = _manual_station(client)
    assert client.post(f"/api/stations/{sid}/entries", json={"ok": 90, "nok": 20}).status_code == 201
    r = client.post(f"/api/stations/{sid}/entries", json={"ok": -10, "nok": -5, "note": "double count"})
    assert r.status_code == 201, r.text
    assert (r.json()["ok"], r.json()["nok"]) == (-10, -5)

    job = client.get("/api/data/summary").json()[0]["active_job"]
    assert (job["total_pass"], job["total_fail"], job["total_count"]) == (80, 15, 95)
    counters = client.get(f"/api/stations/{sid}/counters").json()[0]
    assert (counters["manual_pass"], counters["manual_fail"]) == (80, 15)

    today = dt.datetime.now(UTC).date().isoformat()
    s = client.get("/api/data/scrap", params={"from": today, "to": today, "tz": "UTC"}).json()
    assert (s["overall"]["pass"], s["overall"]["fail"], s["overall"]["scrap_rate"]) == (80, 15, round(15 / 95, 4))
    assert client.get("/api/data/scrap.xlsx", params={"from": today, "to": today, "tz": "UTC"}).status_code == 200

    o = oee.compute(SessionLocal(), hours=24)
    assert (o["totals"]["ok"], o["totals"]["nok"]) == (80, 15)

    view = client.get(f"/api/data/stations/{sid}?hours=8").json()["history"]
    assert (view["ok"], view["nok"], view["corr_ok"], view["corr_nok"]) == (80, 15, -10, -5)
    bar = next(b for b in view["bars"] if b["corr_ok"])
    assert (bar["ok"], bar["nok"], bar["corr_nok"]) == (80, 15, -5)

    rows = client.get("/api/data/readings", params={"station_id": sid}).json()
    corr = next(x for x in rows if x["raw_pass"] < 0)
    assert corr["manual"] and (corr["raw_pass"], corr["raw_fail"]) == (-10, -5) and corr["note"] == "double count"

    db = SessionLocal()
    try:
        daily.compute_days(db, [dt.datetime.now(UTC).date()], "UTC")
        from app.models import DailyStation
        row = db.query(DailyStation).filter(DailyStation.station_id == sid).one()
        assert (row.ok, row.nok, row.manual_ok, row.manual_nok) == (80, 15, 80, 15)
        db.expunge_all()  # compute_days writes the rows anew
        # a day taken below zero: Power BI's scrap and quality stay within 0 - 100 %
        client.post(f"/api/stations/{sid}/entries", json={"nok": -20, "confirm": True})
        daily.compute_days(db, [dt.datetime.now(UTC).date()], "UTC")
        v = db.execute(text("SELECT ok_count, nok_count, scrap_pct, quality_pct FROM powerbi_daily_stations "
                            "WHERE station_id = :s"), {"s": sid}).one()
        assert (v[0], v[1], float(v[2]), float(v[3])) == (80, -5, 0.0, 100.0)
        last = client.get("/api/data/readings", params={"station_id": sid}).json()[0]
        client.delete(f"/api/data/readings/{last['id']}")
    finally:
        db.close()

    # editing and deleting a correction gives the parts back
    assert client.put(f"/api/data/readings/{corr['id']}/entry", json={"ok": -1, "nok": 0}).status_code == 200
    assert client.get("/api/data/summary").json()[0]["active_job"]["total_pass"] == 89
    assert client.delete(f"/api/data/readings/{corr['id']}").status_code == 204
    job = client.get("/api/data/summary").json()[0]["active_job"]
    assert (job["total_pass"], job["total_fail"]) == (90, 20)
    # one side may be zero, not both
    assert client.post(f"/api/stations/{sid}/entries", json={"ok": 0, "nok": 0}).status_code == 400
    assert client.post(f"/api/stations/{sid}/entries", json={"ok": 5, "nok": -3}).status_code == 201


def test_below_zero_asks_to_confirm(client):
    login(client)
    sid = _manual_station(client)
    client.post(f"/api/stations/{sid}/entries", json={"ok": 10, "nok": 2, "job": "A"})
    client.post(f"/api/stations/{sid}/entries", json={"ok": 5, "nok": 0, "job": "B"})

    # within the station and the job: fine
    assert client.post(f"/api/stations/{sid}/entries", json={"nok": -2, "job": "A", "tz": "UTC"}).status_code == 201
    # job B has no NOK today: asks, nothing is saved
    r = client.post(f"/api/stations/{sid}/entries", json={"nok": -1, "job": "B", "tz": "UTC"})
    assert r.status_code == 409
    msg = r.json()["detail"]["confirm"]
    assert "Station 'Hand'" in msg and "NOK -1" in msg and "Job 'B'" in msg
    assert client.get(f"/api/data/readings?station_id={sid}").json()[0]["raw_fail"] != -1
    # confirmed: saved, the dashboard shows zero rather than a negative count
    r = client.post(f"/api/stations/{sid}/entries", json={"nok": -1, "job": "B", "tz": "UTC", "confirm": True})
    assert r.status_code == 201
    # OK of job B (5) would go below zero, the station's (15) would not
    r = client.post(f"/api/stations/{sid}/entries", json={"ok": -6, "job": "B", "tz": "UTC"})
    assert r.status_code == 409 and r.json()["detail"]["confirm"].startswith("Job 'B'")

    # a correction on another day is checked against that day
    yesterday = (dt.datetime.now(UTC) - dt.timedelta(days=1)).replace(hour=12).isoformat()
    r = client.post(f"/api/stations/{sid}/entries", json={"ok": -1, "job": "A", "at": yesterday, "tz": "UTC"})
    assert r.status_code == 409

    # editing: the entry's own old parts don't count against it (job C
    # lifts the station's NOK back above zero)
    client.post(f"/api/stations/{sid}/entries", json={"nok": 5, "job": "C"})
    rows = client.get(f"/api/data/readings?station_id={sid}").json()
    a_corr = next(x for x in rows if x["job_name"] == "A" and x["raw_fail"] == -2)
    assert client.put(f"/api/data/readings/{a_corr['id']}/entry",
                      json={"nok": -2, "note": "same", "tz": "UTC"}).status_code == 200
    r = client.put(f"/api/data/readings/{a_corr['id']}/entry", json={"nok": -3, "tz": "UTC"})
    assert r.status_code == 409
    assert client.put(f"/api/data/readings/{a_corr['id']}/entry",
                      json={"nok": -3, "tz": "UTC", "confirm": True}).status_code == 200


def test_a_correction_never_fires_a_scrap_alert(client, sent):  # noqa: F811
    login(client)
    _provider(client, "all", "http://all/send")
    sid = _manual_station(client)
    client.post("/api/notifications/rules", json={
        "name": "scrap", "condition": "scrap_rate", "threshold": 0.2, "cooldown": 0})
    client.post("/api/notifications/rules", json={
        "name": "fails", "condition": "fail_count", "threshold": 3, "cooldown": 0})
    client.post(f"/api/stations/{sid}/entries", json={"ok": 10, "nok": 2})
    assert sent == []
    # taking back OK raises the scrap rate (2 / 4) and NOK stays at 2: no alert
    client.post(f"/api/stations/{sid}/entries", json={"ok": -8, "tz": "UTC"})
    client.post(f"/api/stations/{sid}/entries", json={"ok": 1, "nok": -1, "tz": "UTC"})
    assert sent == []
    # a normal entry is checked as before
    client.post(f"/api/stations/{sid}/entries", json={"nok": 3})
    assert len(sent) == 2
