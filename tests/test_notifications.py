"""Notification rules: severity, routing to chosen providers, per-camera
thresholds, event conditions, and the Telegram and linked WhatsApp notifiers."""
import httpx
import pytest

from app.notifiers import NotifierError, get_notifier

from test_api import add_station_device


def login(client, user="Admin", pw="1234"):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text


class _Resp:
    status_code = 200

    def __init__(self, data=None):
        self._data = data or {"ok": True}

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


@pytest.fixture()
def sent(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs.get("json")))
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def _provider(client, name, url):
    r = client.post("/api/notifications/providers", json={
        "name": name, "kind": "whatsapp", "config": {"transport": "webhook", "url": url}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _camera(client, name="CamN", fail_ratio=0.5):
    """A simulated device with its station, in production: (device id, station id)."""
    did, sid = add_station_device(client, name, {"jobs": ["J"], "parts_per_poll": 10, "fail_ratio": fail_ratio,
                                                 "reset_every": 0, "job_change_every": 0})
    client.post(f"/api/stations/{sid}/production/start")
    return did, sid


def test_rule_goes_only_to_its_providers_with_severity(client, sent):
    login(client)
    a = _provider(client, "group A", "http://a/send")
    _provider(client, "group B", "http://b/send")
    r = client.post("/api/notifications/rules", json={
        "name": "scrap", "condition": "scrap_rate", "threshold": 0.1, "cooldown": 0,
        "severity": "warning", "provider_ids": [a]})
    assert r.status_code == 201, r.text
    did, _ = _camera(client)
    client.post(f"/api/devices/{did}/poll")
    urls = {u for u, _ in sent}
    assert urls == {"http://a/send"}
    assert sent[0][1]["message"].startswith("⚠️ WARNING · High scrap")


def test_per_camera_threshold_overrides_the_global_one(client, sent):
    login(client)
    _provider(client, "all", "http://all/send")
    did, sid = _camera(client, fail_ratio=0.5)
    client.post("/api/notifications/rules", json={
        "name": "scrap", "condition": "scrap_rate", "threshold": 0.1, "cooldown": 0,
        "thresholds": {str(sid): 0.9}})  # this station only alerts at 90 %
    client.post(f"/api/devices/{did}/poll")
    assert sent == []
    rule = client.get("/api/notifications/rules").json()[0]
    client.patch(f"/api/notifications/rules/{rule['id']}", json={"thresholds": {}})
    client.post(f"/api/devices/{did}/poll")
    assert len(sent) == 1


def test_rules_from_before_routing_still_send_to_every_provider(client, sent):
    login(client)
    _provider(client, "a", "http://a/send")
    _provider(client, "b", "http://b/send")
    from app.database import SessionLocal
    from app.models import NotificationRule

    db = SessionLocal()
    db.add(NotificationRule(name="old", condition="scrap_rate", threshold=0.0, cooldown=0,
                            provider_ids=None, thresholds=None))
    db.commit()
    db.close()
    did, _ = _camera(client)
    client.post(f"/api/devices/{did}/poll")
    assert {u for u, _ in sent} == {"http://a/send", "http://b/send"}


def test_production_change_and_job_change_events(client, sent):
    login(client)
    _provider(client, "p", "http://p/send")
    for cond in ("production_change", "job_change"):
        assert client.post("/api/notifications/rules", json={
            "name": cond, "condition": cond, "cooldown": 0, "severity": "info"}).status_code == 201
    did, sid = add_station_device(client, "CamJ", {"jobs": ["A", "B"], "parts_per_poll": 5, "reset_every": 0,
                                                   "job_change_every": 2})
    for _ in range(3):
        client.post(f"/api/devices/{did}/poll")
    client.post(f"/api/stations/{sid}/production/stop")
    client.post(f"/api/devices/{did}/poll")
    messages = [body["message"] for _, body in sent]
    assert any("changed job from 'A' to 'B'" in m for m in messages), messages
    assert any("was stopped by an operator" in m for m in messages), messages
    assert all(m.startswith("ℹ️ INFO · ") for m in messages)


def test_unknown_condition_and_severity_are_refused(client):
    login(client)
    assert client.post("/api/notifications/rules", json={"name": "x", "condition": "nope"}).status_code == 400
    assert client.post("/api/notifications/rules", json={
        "name": "x", "condition": "app_started", "severity": "loud"}).status_code == 400
    meta = client.get("/api/notifications/conditions").json()
    assert {"scrap_rate", "app_started", "backup_failed"} <= {c["key"] for c in meta["conditions"]}


def test_telegram_sends_to_every_chat(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        if kwargs["json"]["chat_id"] == "-2":
            r = _Resp({"ok": False, "description": "Bad Request: chat not found"})
            r.status_code = 400
            return r
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    n = get_notifier("telegram", {"bot_token": "123:ABC", "chat_ids": "-1, -2"})
    with pytest.raises(NotifierError) as exc:
        n.send("hello")
    assert [c[1]["chat_id"] for c in calls] == ["-1", "-2"]
    assert calls[0][0] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert "chat -2: Bad Request: chat not found" in str(exc.value)
    assert "123:ABC" not in str(exc.value)  # the token never lands in the alert log


def test_linked_whatsapp_without_a_phone_says_so(client):
    login(client)
    with pytest.raises(NotifierError, match="not linked"):
        get_notifier("whatsapp", {"transport": "linked", "to": "123@g.us"}).send("x")
    assert client.get("/api/notifications/whatsapp").json()["state"] in ("unknown", "not_linked")
