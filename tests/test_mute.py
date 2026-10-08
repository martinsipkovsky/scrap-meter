"""Muted alerts (!mute / !unmute and the Scrap warnings switch), and the
version / Changelog page."""
from app import __version__, changelog, commands, mute, notifications
from app.database import SessionLocal
from app.models import ChatCommand, NotificationLog, NotificationProvider, NotificationRule, Station

from test_api import add_station_device

GROUP = "120363000000000001@g.us"


def login(client, user="Admin", pw="1234"):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text


def _setup(client):
    login(client)
    _, sid = add_station_device(client, "Line 1", {"jobs": ["J1"], "parts_per_poll": 10, "fail_ratio": 0.5,
                                                    "reset_every": 0, "job_change_every": 0})
    add_station_device(client, "Line 2", {"jobs": ["J1"]})
    db = SessionLocal()
    try:
        if not db.query(ChatCommand).count():  # seeded once per test run
            db.add(ChatCommand(**commands.DEFAULT_STATUS, group_ids=[]))
        db.add(NotificationProvider(name="hook", kind="whatsapp", config={"transport": "webhook", "url": "http://x"}))
        db.add(NotificationRule(name="jobs", condition="job_change", cooldown=0))
        db.commit()
    finally:
        db.close()
    return sid


def _say(text, sender="Martin"):
    db = SessionLocal()
    try:
        return commands.answer(db, {"chat": GROUP, "text": text, "sender_name": sender}, "!")[2]
    finally:
        db.close()


def _station(sid):
    db = SessionLocal()
    try:
        return db.get(Station, sid)
    finally:
        db.close()


def _logs():
    db = SessionLocal()
    try:
        return db.query(NotificationLog).count()
    finally:
        db.close()


def test_chat_mute_until_job_change(client, monkeypatch):
    sid = _setup(client)
    assert _say("!mute").startswith("Which station? Send !mute <station>. Stations: Line 1, Line 2")
    assert _say("!mute line").startswith("No single station matches 'line'.")
    assert "muted until its job changes" in _say("!mute LINE 1")
    st = _station(sid)
    assert st.alerts_muted and st.muted_by == "Martin" and st.muted_until_job_change
    assert "already muted (by Martin)" in _say("!mute line 1")
    assert "!mute <station>" in _say("!help") and "!unmute <station>" in _say("!help")

    summary = {d["id"]: d for d in client.get("/api/data/summary").json()}
    assert summary[sid]["alerts_muted"] is True and summary[sid]["muted_by"] == "Martin"

    # a muted station sends nothing
    sent = []
    monkeypatch.setattr(notifications.notifiers, "get_notifier",
                        lambda kind, cfg: type("N", (), {"send": lambda self, m: sent.append(m)})())
    db = SessionLocal()
    try:
        notifications.emit(db, "job_change", "changed", db.get(Station, sid))
        assert sent == []
        # the job changes: alerts are on again, and the job change is sent
        st = db.get(Station, sid)
        mute.job_changed(db, st, "J1", "J2")
        assert not st.alerts_muted
        notifications.emit(db, "job_change", "changed", st)
        assert len(sent) == 1 and sent[0].endswith("changed")
    finally:
        db.close()

    events = client.get("/api/data/events", params={"station_id": sid}).json()
    assert [(e["kind"], e["by"], e["source"]) for e in events] == [
        ("unmute", None, "job change"), ("mute", "Martin", "chat")]
    assert events[0]["detail"] == "job changed from 'J1' to 'J2'"


def test_switch_and_unmute(client):
    sid = _setup(client)
    r = client.put(f"/api/stations/{sid}/alerts", json={"on": False})
    assert r.json()["alerts_muted"] is True and r.json()["muted_until_job_change"] is False
    db = SessionLocal()
    try:
        mute.job_changed(db, db.get(Station, sid), "J1", "J2")  # the switch outlasts a job change
        assert db.get(Station, sid).alerts_muted
    finally:
        db.close()
    assert "on again" in _say("!unmute Line 1", "Jana")
    assert _say("!unmute Line 1") == "🔔 Alerts of 'Line 1' are on."
    _say("!mute Line 1")
    # the switch turned off over a "!mute" makes it last until turned on
    client.put(f"/api/stations/{sid}/alerts", json={"on": False})
    assert _station(sid).muted_until_job_change is False and _station(sid).muted_by == "Admin"
    client.put(f"/api/stations/{sid}/alerts", json={"on": True})
    assert not _station(sid).alerts_muted
    kinds = [(e["kind"], e["source"]) for e in client.get("/api/data/events").json()]
    assert kinds == [("unmute", "web"), ("mute", "web"), ("mute", "chat"), ("unmute", "chat"), ("mute", "web")]
    assert "Alerts muted and turned on" in client.get("/data").text

    client.post("/api/users", json={"username": "op", "password": "pw", "permissions": ["view_dashboard"]})
    client.get("/logout")
    login(client, "op", "pw")
    assert client.put(f"/api/stations/{sid}/alerts", json={"on": False}).status_code == 403
    # deleting the station takes its events along
    client.get("/logout")
    login(client)
    assert client.delete(f"/api/stations/{sid}").status_code == 204
    assert client.get("/api/data/events").json() == []


def test_version_and_changelog(client):
    login(client)
    page = client.get("/").text
    assert f"Version {__version__}" in page and 'href="/changelog"' in page
    r = client.get("/changelog")
    assert r.status_code == 200 and "running now" in r.text
    releases = changelog.parse(changelog.PATH.read_text(encoding="utf-8"))
    assert releases[0]["version"] == __version__  # every release gets its entry
    assert [r["version"] for r in releases][-2:] == ["1.1", "1.0"]
    assert all(r["date"] and r["items"] for r in releases)
    one = changelog.parse("# x\n\n## 2.0.0 — 2027-01-01\n- Use `!mute` <now>\n")
    assert one == [{"version": "2.0.0", "date": "2027-01-01", "items": ["Use <code>!mute</code> &lt;now&gt;"]}]
