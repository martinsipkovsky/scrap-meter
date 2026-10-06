"""End-to-end API tests against an in-memory sqlite database.

Exercises login, RBAC, device and station CRUD, a simulated poll that feeds
the counters, job-change handling and the notification pipeline (via a dummy
webhook).
"""
def login(client, user="Admin", pw="1234"):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text


def add_station_device(client, name, config=None, protocol="simulator", **device):
    """A device with its own station (as 1.4 devices became). Returns
    (device id, station id)."""
    r = client.post("/api/devices", json={
        "name": name, "host": "sim", "port": 0, "protocol": protocol,
        "protocol_config": config or {}, "create_station": True, **device,
    })
    assert r.status_code == 201, r.text
    sid = next(s["id"] for s in client.get("/api/stations").json() if s["name"] == name)
    return r.json()["id"], sid


def poll(client, did):
    """Poll a device; the reading of its first station."""
    r = client.post(f"/api/devices/{did}/poll")
    assert r.status_code == 200, r.text
    return r.json()["stations"][0]


def test_login_required(client):
    r = client.get("/api/devices")
    assert r.status_code == 401


def test_admin_login_and_create_user_with_permissions(client):
    login(client)
    r = client.post("/api/users", json={
        "username": "line1", "password": "pw", "is_admin": False,
        "permissions": ["view_dashboard", "view_data"],
    })
    assert r.status_code == 201, r.text
    # the limited user cannot manage devices
    c2 = client
    c2.get("/logout")
    login(c2, "line1", "pw")
    assert c2.post("/api/devices", json={
        "name": "x", "host": "h", "protocol": "simulator",
    }).status_code == 403
    # but can read the dashboard summary
    assert c2.get("/api/data/summary").status_code == 200


def test_simulator_poll_accumulates_and_survives_reset(client):
    login(client)
    did, sid = add_station_device(client, "Cam1", {"jobs": ["JOB_A"], "parts_per_poll": 10, "fail_ratio": 0.1,
                                                   "reset_every": 2, "job_change_every": 0}, poll_interval=1)

    totals = []
    for _ in range(6):
        pr = poll(client, did)
        totals.append(pr["total_pass"] + pr["total_fail"])

    # Totals must be monotonically non-decreasing despite the camera's raw
    # counters resetting to a low value on every other poll. The raw counter
    # never exceeds ~2 batches (20), so a final total well above that proves
    # the accumulation survived the resets rather than tracking the raw value.
    assert totals == sorted(totals)
    assert totals[-1] >= 30

    counters = client.get(f"/api/stations/{sid}/counters").json()
    assert counters[0]["total_count"] == totals[-1]


def test_job_change_starts_new_counter(client):
    login(client)
    did, sid = add_station_device(client, "Cam2", {"jobs": ["A", "B"], "parts_per_poll": 5,
                                                   "reset_every": 0, "job_change_every": 2})
    for _ in range(5):
        poll(client, did)
    counters = client.get(f"/api/stations/{sid}/counters").json()
    jobs = {c["job_name"] for c in counters}
    assert {"A", "B"}.issubset(jobs)
    # exactly one active counter at a time
    assert sum(1 for c in counters if c["is_active"]) == 1


def test_notification_rule_fires_to_webhook(client, monkeypatch):
    login(client)
    # capture webhook deliveries instead of hitting the network
    sent = []

    import httpx

    class _Resp:
        def raise_for_status(self):
            return None

    def fake_post(url, **kwargs):
        sent.append((url, kwargs.get("json")))
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)

    client.post("/api/notifications/providers", json={
        "name": "test", "kind": "whatsapp",
        "config": {"transport": "webhook", "url": "http://example/send"},
    })
    # scrap_rate >= 0 will always fire once there is data
    client.post("/api/notifications/rules", json={
        "name": "any scrap", "condition": "scrap_rate", "threshold": 0.0, "cooldown": 0,
    })
    did, sid = add_station_device(client, "Cam3", {"jobs": ["J"], "parts_per_poll": 10, "fail_ratio": 0.5})
    # scrap alerts only go out for stations in production
    client.post(f"/api/stations/{sid}/production/start")
    poll(client, did)

    logs = client.get("/api/notifications/logs").json()
    assert any(l["delivered"] for l in logs), logs
    assert sent, "webhook was not called"


def test_reset_counters_only_resets_dashboard_counters(client):
    login(client)
    did, sid = add_station_device(client, "Cam4", {"jobs": ["J"], "parts_per_poll": 10, "fail_ratio": 0.2,
                                                   "reset_every": 0, "job_change_every": 0})
    assert client.post(f"/api/stations/{sid}/counters/reset").status_code == 400  # no data yet
    for _ in range(3):
        poll(client, did)
    before = client.get(f"/api/stations/{sid}/counters").json()[0]
    assert before["total_count"] > 0

    r = client.post(f"/api/stations/{sid}/counters/reset")
    assert r.status_code == 200, r.text
    job = client.get("/api/data/summary").json()[0]["active_job"]
    assert (job["total_pass"], job["total_fail"], job["total_count"]) == (0, 0, 0)
    assert job["reset_at"]

    poll(client, did)
    job = client.get("/api/data/summary").json()[0]["active_job"]
    assert job["total_count"] == 10  # counts again from zero
    # the job totals behind the history and scrap statistics keep counting
    after = client.get(f"/api/stations/{sid}/counters").json()[0]
    assert after["total_count"] == before["total_count"] + 10
    readings = client.get(f"/api/data/readings?station_id={sid}").json()
    assert readings[0]["total_pass"] + readings[0]["total_fail"] == after["total_count"]
