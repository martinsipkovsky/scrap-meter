"""Alerts only during production (Notifications > Alert settings): a station
that is idle or stopped gets no alerts, the alert log lists them as skipped;
production_change and chat commands are not affected."""
import httpx
import pytest

from app import commands, notifications, stations
from app.database import SessionLocal
from app.models import ChatCommand, Device, NotificationLog, Station

from test_api import add_station_device

GROUP = "120363000000000001@g.us"


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


def _setup(client, rules=("disconnected",)):
    """A camera and its station (idle: never produced), a webhook provider and
    the given rules for all stations."""
    login(client)
    assert client.post("/api/notifications/providers", json={
        "name": "hook", "kind": "whatsapp", "config": {"transport": "webhook", "url": "http://fake/send"}}).status_code == 201
    for cond in rules:
        assert client.post("/api/notifications/rules", json={
            "name": cond, "condition": cond, "cooldown": 0, "severity": "alert"}).status_code == 201
    did, sid = add_station_device(client, "Line 1", {"jobs": ["J"], "parts_per_poll": 10,
                                                      "reset_every": 0, "job_change_every": 0})
    return did, sid


def _drop(did, error="Camera 10.0.0.5 disconnected"):
    db = SessionLocal()
    try:
        stations.device_failed(db, db.get(Device, did), error)
    finally:
        db.close()


def _logs():
    db = SessionLocal()
    try:
        return [(r.message, r.delivered, r.skipped, r.detail) for r in db.query(NotificationLog).order_by(NotificationLog.id)]
    finally:
        db.close()


def test_policy_is_on_by_default_and_saved(client):
    login(client)
    assert client.get("/api/notifications/policy").json() == {"production_only": True}
    assert client.put("/api/notifications/policy", json={"production_only": "no"}).status_code == 400
    assert client.put("/api/notifications/policy", json={"production_only": False}).json() == {"production_only": False}
    notifications._policy = None  # as after a restart: read back from the settings table
    assert notifications.policy() == {"production_only": False}


def test_device_drop_on_idle_station_is_skipped_and_logged_once(client, sent):
    did, sid = _setup(client)
    _drop(did)
    _drop(did)  # still offline on the next read: not logged again within a minute
    assert sent == []
    logs = _logs()
    assert len(logs) == 1, logs
    message, delivered, skipped, detail = logs[0]
    assert "Line 1" in message and "disconnected" in message
    assert skipped and not delivered and "not in production (idle)" in detail
    row = client.get("/api/notifications/logs").json()[0]
    assert row["skipped"] is True


def test_device_drop_during_production_is_sent(client, sent):
    did, sid = _setup(client)
    client.post(f"/api/stations/{sid}/production/start")
    _drop(did)
    assert len(sent) == 1 and "disconnected" in sent[0]
    assert not any(skipped for *_, skipped, _ in _logs())


def test_switch_off_sends_as_before(client, sent):
    did, sid = _setup(client)
    client.put("/api/notifications/policy", json={"production_only": False})
    _drop(did)
    assert len(sent) == 1 and "Line 1" in sent[0]


def test_stopped_station_events_skipped_but_production_change_sent(client, sent):
    did, sid = _setup(client, rules=("job_change", "production_change"))
    client.post(f"/api/devices/{did}/poll")
    client.post(f"/api/stations/{sid}/production/start")
    client.post(f"/api/devices/{did}/poll")  # records the running state
    client.post(f"/api/stations/{sid}/production/stop")
    db = SessionLocal()
    try:
        st = db.get(Station, sid)
        notifications.check_production(db, st)
        notifications.emit(db, "job_change", "Station 'Line 1' changed job from 'J' to 'K'", st)
    finally:
        db.close()
    assert any("stopped by an operator" in m for m in sent), sent
    assert not any("changed job" in m for m in sent), sent
    skipped = [(m, d) for m, _, s, d in _logs() if s]
    assert len(skipped) == 1 and "changed job" in skipped[0][0] and "(stopped)" in skipped[0][1]


def test_chat_commands_answer_for_idle_station(client):
    _setup(client)
    db = SessionLocal()
    try:
        if not db.query(ChatCommand).count():
            db.add(ChatCommand(**commands.DEFAULT_STATUS, group_ids=[]))
            db.commit()
        outcome, _, reply = commands.answer(db, {"chat": GROUP, "text": "!status"}, "!")
    finally:
        db.close()
    assert outcome == "answered" and "Line 1" in reply
