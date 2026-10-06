"""Production state, station view history, export/import and the Database page."""
import datetime as dt

from app import production
from app.database import SessionLocal, engine, migrate_schema
from app.models import Reading, Station, utcnow
from app.routers.data import ok_nok_buckets


def login(client, user="Admin", pw="1234"):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text


def make_sim(client, name="Cam", **cfg):
    """A simulated device and its station: the station's JSON plus "device_id"."""
    r = client.post("/api/devices", json={
        "name": name, "host": "sim", "port": 0, "protocol": "simulator",
        "protocol_config": {"jobs": ["J"], "parts_per_poll": 10, "fail_ratio": 0.2,
                            "reset_every": 0, "job_change_every": 0, **cfg},
        "create_station": True,
    })
    assert r.status_code == 201, r.text
    st = next(x for x in client.get("/api/stations").json() if x["name"] == name)
    return {**st, "device_id": r.json()["id"]}


def _set(sid, **fields):
    db = SessionLocal()
    try:
        d = db.get(Station, sid)
        for k, v in fields.items():
            setattr(d, k, v)
        db.commit()
    finally:
        db.close()


def test_state_rules():
    now = utcnow()
    d = Station(name="x", idle_timeout_min=30, manual_stop=False)
    assert production.state(d, now) == "idle"  # never counted a pass
    d.last_pass_change_at = now - dt.timedelta(minutes=29)
    assert production.state(d, now) == "running"
    d.last_pass_change_at = now - dt.timedelta(minutes=31)
    assert production.state(d, now) == "idle"
    d.idle_timeout_min = 60
    assert production.state(d, now) == "running"
    d.manual_stop = True
    assert production.state(d, now) == "stopped"


def test_pass_increase_puts_camera_in_production_and_idle_after_timeout(client):
    login(client)
    d = make_sim(client)
    assert d["production_state"] == "idle" and d["idle_timeout_min"] == 30
    client.post(f"/api/devices/{d['device_id']}/poll")  # baseline
    client.post(f"/api/devices/{d['device_id']}/poll")  # +passes
    s = {x["id"]: x for x in client.get("/api/data/summary").json()}[d["id"]]
    assert s["production_state"] == "running" and s["in_production"]

    _set(d["id"], last_pass_change_at=utcnow() - dt.timedelta(minutes=31))
    s = {x["id"]: x for x in client.get("/api/data/summary").json()}[d["id"]]
    assert s["production_state"] == "idle"
    # a new pass brings it back automatically
    client.post(f"/api/devices/{d['device_id']}/poll")
    s = {x["id"]: x for x in client.get("/api/data/summary").json()}[d["id"]]
    assert s["production_state"] == "running"


def test_manual_stop_is_sticky_until_start(client):
    login(client)
    d = make_sim(client)
    sid, did = d["id"], d["device_id"]
    assert client.post(f"/api/stations/{sid}/production/stop").json()["production_state"] == "stopped"
    client.post(f"/api/devices/{did}/poll")
    client.post(f"/api/devices/{did}/poll")
    assert client.get("/api/stations").json()[0]["production_state"] == "stopped"
    assert client.post(f"/api/stations/{sid}/production/start").json()["production_state"] == "running"


def test_idle_timeout_editable(client):
    login(client)
    d = make_sim(client)
    r = client.patch(f"/api/stations/{d['id']}", json={"idle_timeout_min": 45})
    assert r.json()["idle_timeout_min"] == 45
    assert client.patch(f"/api/stations/{d['id']}", json={"idle_timeout_min": 0}).status_code == 422


def test_no_scrap_alert_when_not_in_production(client, monkeypatch):
    login(client)
    import httpx

    sent = []

    class _Resp:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.append(kw.get("json")) or _Resp())
    client.post("/api/notifications/providers", json={
        "name": "t", "kind": "whatsapp", "config": {"transport": "webhook", "url": "http://x/send"}})
    client.post("/api/notifications/rules", json={
        "name": "scrap", "condition": "scrap_rate", "threshold": 0.0, "cooldown": 0})
    d = make_sim(client, fail_ratio=0.5)
    client.post(f"/api/stations/{d['id']}/production/stop")
    client.post(f"/api/devices/{d['device_id']}/poll")
    client.post(f"/api/devices/{d['device_id']}/poll")
    assert not sent
    client.post(f"/api/stations/{d['id']}/production/start")
    client.post(f"/api/devices/{d['device_id']}/poll")
    assert sent


def test_ok_nok_buckets_use_total_differences():
    t0 = dt.datetime(2026, 1, 1, 8, 0, tzinfo=dt.timezone.utc)

    def r(minute, job, p, f):
        return Reading(job_name=job, total_pass=p, total_fail=f, created_at=t0 + dt.timedelta(minutes=minute))

    rows = [r(0, "A", 100, 5), r(1, "A", 110, 6), r(2, "A", 125, 6), r(6, "B", 0, 0), r(7, "B", 4, 1)]
    bars = ok_nok_buckets(rows, t0, t0 + dt.timedelta(minutes=9), 300)
    assert [(b["ok"], b["nok"]) for b in bars] == [(25, 1), (4, 1)]


def test_station_view_endpoint(client):
    login(client)
    d = make_sim(client)
    for _ in range(3):
        client.post(f"/api/devices/{d['device_id']}/poll")
    v = client.get(f"/api/data/stations/{d['id']}?hours=1").json()
    assert client.get(f"/api/data/devices/{d['id']}?hours=1").status_code == 200  # 1.4 path
    assert v["name"] == "Cam" and v["history"]["bucket_seconds"] == 60
    assert v["history"]["ok"] + v["history"]["nok"] == 20  # two polls after the baseline
    for url in ("/station/", "/device/", "/camera/"):  # the last two are old links
        assert client.get(f"{url}{d['id']}").status_code == 200


def test_export_import_roundtrip(client):
    login(client)
    make_sim(client, "A")
    make_sim(client, "B")
    exp = client.get("/api/devices/export")
    assert "attachment" in exp.headers["content-disposition"]
    data = exp.json()
    assert data["version"] == 2
    assert [c["name"] for c in data["cameras"]] == ["A", "B"]
    assert "id" not in data["cameras"][0]
    assert data["stations"][0]["sources"]["ok"] == {"device": "A", "key": "pass"}
    assert data["stations"][0]["idle_timeout_min"] == 30

    data["stations"][0]["idle_timeout_min"] = 15
    data["cameras"].append({**data["cameras"][1], "name": "C"})
    data["stations"].append({**data["stations"][1], "name": "C",
                             "sources": {"ok": {"device": "C", "key": "pass"}, "nok": {"device": "A", "key": "fail"}}})
    r = client.post("/api/devices/import", json=data)
    assert r.status_code == 200, r.text
    assert r.json() == {"created": ["C"], "updated": ["A", "B"], "stations_created": ["C"],
                        "stations_updated": ["A", "B"]}
    devs = {d["name"]: d for d in client.get("/api/devices").json()}
    sts = {s["name"]: s for s in client.get("/api/stations").json()}
    assert set(devs) == {"A", "B", "C"} and sts["A"]["idle_timeout_min"] == 15
    assert sts["C"]["sources"]["nok"] == {"device_id": devs["A"]["id"], "key": "fail"}


def test_import_of_a_1_4_file_makes_stations(client):
    login(client)
    r = client.post("/api/devices/import", json={"version": 1, "cameras": [
        {"name": "Old", "host": "sim", "port": 0, "protocol": "simulator", "idle_timeout_min": 20,
         "stats_default": "exclude"},
        {"name": "Opc", "protocol": "opcua", "protocol_config": {"endpoint": "opc.tcp://x:4840"}},
    ]})
    assert r.status_code == 200, r.text
    assert r.json()["stations_created"] == ["Old"]  # an OPC UA device without nodes gets none
    st = client.get("/api/stations").json()[0]
    assert (st["name"], st["idle_timeout_min"], st["stats_default"]) == ("Old", 20, "exclude")


def test_import_is_all_or_nothing(client):
    login(client)
    r = client.post("/api/devices/import", json={"cameras": [
        {"name": "ok", "protocol": "simulator"},
        {"name": "bad", "protocol": "nope"},
    ]})
    assert r.status_code == 400 and "bad" in r.json()["detail"]
    assert client.get("/api/devices").json() == []
    r = client.post("/api/devices/import", json={"cameras": [
        {"name": "L1", "protocol": "tcp_listen", "port": 5100},
        {"name": "L2", "protocol": "tcp_listen", "port": 5100},
    ]})
    assert r.status_code == 400 and "5100" in r.json()["detail"]


def test_migration_adds_missing_columns():
    from app.reporting import VIEWS

    with engine.begin() as conn:
        # an old database: no report views yet (SQLite won't drop a column a view uses)
        for name in VIEWS:
            conn.exec_driver_sql(f"DROP VIEW IF EXISTS {name}")
        conn.exec_driver_sql("ALTER TABLE stations DROP COLUMN ideal_cycle_s")
    assert "stations.ideal_cycle_s" in migrate_schema(engine)
    assert migrate_schema(engine) == []


def test_database_page_is_admin_only(client):
    login(client)
    s = client.get("/api/database").json()
    assert s["source"] == "environment" and s["active"]["driver"].startswith("sqlite")
    assert client.get("/database").status_code == 200
    client.post("/api/users", json={"username": "op", "password": "pw", "permissions": ["view_dashboard"]})
    client.get("/logout")
    login(client, "op", "pw")
    assert client.get("/api/database").status_code == 403
    assert client.get("/database").status_code == 403


def test_database_save_requires_working_connection(client):
    login(client)
    r = client.put("/api/database", json={"host": "127.0.0.1", "port": 1, "database": "x", "user": "u", "password": "p"})
    assert r.status_code == 400
    from app import dbconfig
    assert dbconfig.load() is None


def test_copy_all_data_into_new_database(client, tmp_path, monkeypatch):
    login(client)
    d = make_sim(client)
    client.post(f"/api/devices/{d['device_id']}/poll")
    from app.routers import database_admin
    from sqlalchemy import create_engine, text

    target = f"sqlite+pysqlite:///{tmp_path / 'new.db'}"
    copied = database_admin.copy_all_data(target)
    assert copied["devices"] == 1 and copied["stations"] == 1 and copied["users"] == 1 and copied["readings"] == 1
    eng = create_engine(target)
    with eng.connect() as c:
        assert c.execute(text("select name from devices")).scalar_one() == "Cam"
    eng.dispose()


def test_static_assets_are_cache_busted(client):
    import re

    login(client)
    html = client.get("/devices").text
    m = re.search(r'src="(/static/app\.js\?v=[0-9a-f]{10})"', html)
    assert m, "app.js must carry a content hash"
    assert "downloadJSON" in client.get(m.group(1)).text
    assert re.search(r'href="/static/style\.css\?v=[0-9a-f]{10}"', client.get("/login").text)
