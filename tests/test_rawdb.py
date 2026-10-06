"""The Raw data tab: admins only, paged rows, type-checked edits with an audit
log and undo, hidden secrets, and the read-only SQL box."""
import json

from test_api import add_station_device, login


def _rows(client, table, **params):
    if "filters" in params:
        params["filters"] = json.dumps(params["filters"])
    r = client.get(f"/api/rawdb/tables/{table}/rows", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_admins_only(client):
    login(client)
    client.post("/api/users", json={"username": "op", "password": "pw", "permissions": list(
        ["view_dashboard", "view_data", "manage_devices", "manage_users"])})
    assert "/rawdb" in client.get("/").text
    client.get("/logout")
    login(client, "op", "pw")
    assert "/rawdb" not in client.get("/").text
    assert client.get("/rawdb", follow_redirects=False).status_code == 403
    for method, url in (("get", "/api/rawdb/tables"), ("get", "/api/rawdb/tables/stations/rows"),
                        ("get", "/api/rawdb/audit"), ("post", "/api/rawdb/sql")):
        assert getattr(client, method)(url, **({"json": {"query": "SELECT 1"}} if method == "post" else {})).status_code == 403


def test_browse_edit_and_undo(client):
    login(client)
    for n in ("A", "B", "C"):
        add_station_device(client, n)
    tables = {t["name"]: t for t in client.get("/api/rawdb/tables").json()}
    assert tables["stations"]["rows"] == 3 and tables["db_audit_log"]["read_only"]
    assert not tables["users"]["can_insert"]

    d = _rows(client, "stations", size=25, sort="name")
    assert [r["name"] for r in d["rows"]] == ["A", "B", "C"] and d["total"] == 3
    cols = {c["name"]: c for c in d["columns"]}
    assert cols["idle_timeout_min"]["kind"] == "integer" and cols["manual_stop"]["kind"] == "boolean"
    assert cols["id"]["primary_key"] and not cols["id"]["editable"]
    assert _rows(client, "stations", sort="name", desc=True)["rows"][0]["name"] == "C"
    assert [r["name"] for r in _rows(client, "stations", q="b")["rows"]] == ["B"]
    assert [r["name"] for r in _rows(client, "stations", filters=[{"column": "name", "op": "!=", "value": "A"}],
                                     sort="name")["rows"]] == ["B", "C"]
    assert _rows(client, "stations", page=2, size=25)["rows"] == []

    sid = d["rows"][0]["id"]
    url = f"/api/rawdb/tables/stations/rows/{sid}"
    r = client.patch(url, json={"values": {"idle_timeout_min": "45", "manual_stop": True}})
    assert r.status_code == 200, r.text
    assert (r.json()["idle_timeout_min"], r.json()["manual_stop"]) == (45, True)
    assert client.get("/api/stations").json()[0]["idle_timeout_min"] == 45
    # wrong types and the key are refused, nothing changes
    assert client.patch(url, json={"values": {"idle_timeout_min": "lots"}}).status_code == 400
    assert client.patch(url, json={"values": {"id": 99}}).status_code == 400
    assert client.patch(url, json={"values": {"name": None}}).status_code == 400  # not nullable
    assert client.patch(url, json={"values": {"last_reading_at": "2026-10-06T08:30"}}).status_code == 200

    log = client.get("/api/rawdb/audit").json()
    upd = next(e for e in log if e["action"] == "update" and "idle_timeout_min" in e["new"])
    assert upd["old"] == {"idle_timeout_min": 30, "manual_stop": False} and upd["user"] == "Admin"
    assert upd["new"] == {"idle_timeout_min": 45, "manual_stop": True} and upd["table"] == "stations"
    # the later change of the same row has to be undone first? only when it touches the same values
    assert client.post(f"/api/rawdb/audit/{upd['id']}/undo").status_code == 200
    assert client.get("/api/stations").json()[0]["idle_timeout_min"] == 30
    assert client.post(f"/api/rawdb/audit/{upd['id']}/undo").status_code == 400  # once
    # changed again since: refused
    client.patch(url, json={"values": {"default_job": "X"}})
    client.patch(url, json={"values": {"default_job": "Y"}})
    first = next(e for e in client.get("/api/rawdb/audit").json() if e["new"] == {"default_job": "X"})
    assert client.post(f"/api/rawdb/audit/{first['id']}/undo").status_code == 400

    # add, undo the add; delete, undo the delete
    r = client.post("/api/rawdb/tables/jobs/rows", json={"values": {"name": "NEWJOB", "ideal_cycle_s": "2.5"}})
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["ideal_cycle_s"] == 2.5 and job["id"]
    add = client.get("/api/rawdb/audit").json()[0]
    assert add["action"] == "insert" and add["row"] == str(job["id"])
    client.post(f"/api/rawdb/audit/{add['id']}/undo")
    assert all(j["name"] != "NEWJOB" for j in _rows(client, "jobs")["rows"])
    assert client.delete(f"/api/rawdb/tables/stations/rows/{sid}").status_code == 204
    assert len(client.get("/api/stations").json()) == 2
    dele = client.get("/api/rawdb/audit").json()[0]
    assert dele["action"] == "delete" and dele["old"]["name"] == "A"
    assert client.post(f"/api/rawdb/audit/{dele['id']}/undo").status_code == 200
    assert sorted(s["name"] for s in client.get("/api/stations").json()) == ["A", "B", "C"]
    # the audit log can't be edited
    assert client.delete(f"/api/rawdb/tables/db_audit_log/rows/{dele['id']}").status_code == 400
    # foreign key dropdown
    opts = client.get("/api/rawdb/tables/counter_states/options/station_id").json()
    assert {o["label"].split(" · ")[1] for o in opts} == {"A", "B", "C"}


def test_secrets_stay_hidden(client):
    login(client)
    d = _rows(client, "users")
    admin = d["rows"][0]
    assert admin["password_hash"] == "•••• hidden"
    assert next(c for c in d["columns"] if c["name"] == "password_hash")["hidden"]
    assert client.patch(f"/api/rawdb/tables/users/rows/{admin['id']}",
                        json={"values": {"password_hash": "x"}}).status_code == 400
    assert client.delete(f"/api/rawdb/tables/users/rows/{admin['id']}").status_code == 400  # yourself
    assert client.post("/api/rawdb/tables/users/rows", json={"values": {"username": "x"}}).status_code == 400
    assert client.get("/api/rawdb/tables/users/rows", params={"q": "pbkdf2"}).json()["rows"] == []

    client.post("/api/notifications/providers", json={"name": "tg", "kind": "telegram",
                                                      "config": {"bot_token": "123:SECRET", "chat_ids": ["1"]}})
    assert _rows(client, "notification_providers")["rows"][0]["config"] == "•••• hidden"
    dev = client.post("/api/devices", json={"name": "PLC", "host": "", "port": 0, "protocol": "opcua",
                                            "protocol_config": {"endpoint": "opc.tcp://x:4840", "username": "u",
                                                                "password": "s3cret"}}).json()
    row = next(r for r in _rows(client, "devices")["rows"] if r["id"] == dev["id"])
    assert row["protocol_config"]["password"] == "********"
    cfg = {**row["protocol_config"], "username": "v"}
    r = client.patch(f"/api/rawdb/tables/devices/rows/{dev['id']}", json={"values": {"protocol_config": json.dumps(cfg)}})
    assert r.status_code == 200, r.text
    from app.database import SessionLocal
    from app.models import DbAuditLog, Device

    db = SessionLocal()
    try:
        saved = db.get(Device, dev["id"]).protocol_config
        assert (saved["username"], saved["password"]) == ("v", "s3cret")  # the masked password was kept
    finally:
        db.close()
    log = client.get("/api/rawdb/audit").json()[0]
    assert log["new"]["protocol_config"]["password"] == "********"
    assert "s3cret" not in json.dumps(client.get("/api/rawdb/audit").json())
    assert "s3cret" not in json.dumps(_rows(client, "db_audit_log")["rows"])


def test_read_only_sql(client):
    login(client)
    add_station_device(client, "A")

    def sql(q):
        return client.post("/api/rawdb/sql", json={"query": q})

    r = sql("SELECT name, idle_timeout_min FROM stations ORDER BY name;")
    assert r.status_code == 200, r.text
    assert r.json()["columns"] == ["name", "idle_timeout_min"] and r.json()["rows"] == [["A", 30]]
    assert sql("WITH s AS (SELECT name FROM stations) SELECT count(*) AS n FROM s").json()["rows"] == [[1]]
    for bad in ("DELETE FROM stations", "UPDATE stations SET name = 'x'", "SELECT 1; DELETE FROM stations",
                "DROP TABLE stations", "SELECT password_hash FROM users", "SELECT * FROM notification_providers",
                'SELECT "password_hash" FROM users', "SELECT * INTO x FROM stations", "PRAGMA table_info(users)",
                "WITH x AS (DELETE FROM stations RETURNING 1) SELECT * FROM x", ""):
        r = sql(bad)
        assert r.status_code == 400, (bad, r.text)
    assert client.get("/api/stations").json()[0]["name"] == "A"
    # hashes are masked however they are selected
    r = sql("SELECT * FROM users").json()
    i = r["columns"].index("password_hash")
    assert r["rows"][0][i] == "•••• hidden"
    r = sql("WITH t(a, b, c) AS (SELECT id, username, password_hash FROM users) SELECT c FROM t")
    assert r.status_code == 400  # names the column
    r = sql("WITH t AS (SELECT * FROM users) SELECT * FROM t").json()
    assert "pbkdf2" not in json.dumps(r)
    # an error from the database is shown, not a crash
    r = sql("SELECT nope FROM stations")
    assert r.status_code == 400 and "database says" in r.json()["detail"]
    # strings in the query can hold any words
    assert sql("SELECT 'delete; drop' AS x").json()["rows"] == [["delete; drop"]]
