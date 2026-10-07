"""WhatsApp group commands: parsing, allowed groups, help, replies, the
ready-made status command and the API."""
import re

from app import commands, settings_store
from app.database import SessionLocal
from app.models import ChatCommand, CommandLog

from test_api import add_station_device

GROUP = "120363000000000001@g.us"
OTHER = "120363000000000002@g.us"


def login(client, user="Admin", pw="1234"):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text


def _camera(client, name):
    did, sid = add_station_device(client, name, {"jobs": ["J1"], "parts_per_poll": 10, "fail_ratio": 0.2,
                                                 "reset_every": 0, "job_change_every": 0})
    client.post(f"/api/stations/{sid}/production/start")
    for _ in range(3):
        client.post(f"/api/devices/{did}/poll")
    return did


def _status(db, **kw):
    cmd = ChatCommand(**{**commands.DEFAULT_STATUS, "group_ids": [], **kw})
    db.add(cmd)
    db.commit()
    return cmd


def test_parse():
    assert commands.parse("!status", "!") == ("status", "")
    assert commands.parse("  !Status  Line 1 ", "!") == ("status", "Line 1")
    assert commands.parse("status", "!") is None
    assert commands.parse("!", "!") is None
    assert commands.parse("/scrap", "/") == ("scrap", "")
    assert commands.valid_prefix("!") and commands.valid_prefix("#cm")
    assert not commands.valid_prefix("a!") and not commands.valid_prefix("") and not commands.valid_prefix("! x")


def test_status_reply_and_camera_filter(client):
    login(client)
    _camera(client, "Line 1")
    _camera(client, "Line 2")
    db = SessionLocal()
    try:
        _status(db)
        outcome, kw, reply = commands.answer(db, {"chat": GROUP, "text": "!status"}, "!")
        assert (outcome, kw) == ("answered", "status")
        assert reply.startswith("📊 Status ")
        assert "Line 1: ▶ in production, job J1" in reply and "Line 2:" in reply
        # dashboard counters: the baseline, then 2 polls of 10 parts per camera
        # (the simulator picks the fails)
        per_camera = [int(a) + int(b) for a, b in re.findall(r"OK (\d+) · NOK (\d+) · scrap", reply)]
        assert per_camera == [20, 20, 40]  # two cameras, then the total line

        _, _, one = commands.answer(db, {"chat": GROUP, "text": "!status line 2"}, "!")
        assert "Line 2:" in one and "Line 1:" not in one
        _, _, none = commands.answer(db, {"chat": GROUP, "text": "!status nope"}, "!")
        assert none == "No station matches 'nope'."
    finally:
        db.close()


def test_periods_and_unknown_placeholders(client):
    login(client)
    _camera(client, "Cam")
    db = SessionLocal()
    try:
        for period in ("today", "hours"):
            cmd = ChatCommand(keyword="x", period=period, hours=2, timezone="Europe/Bratislava",
                              header="{period}", line="{camera} {pass}/{fail} {nope}", footer="")
            reply = commands.render(db, cmd)
            assert "{nope}" in reply
            ok, nok = re.search(r"Cam (\d+)/(\d+)", reply).groups()
            assert int(ok) + int(nok) == 20, reply  # parts made after the first reading
    finally:
        db.close()


def test_groups_help_and_unknown(client):
    login(client)
    db = SessionLocal()
    try:
        _status(db, group_ids=[GROUP])
        assert commands.answer(db, {"chat": OTHER, "text": "!status"}, "!")[0] == "not_allowed"
        # unrelated groups are not answered at all, not even with a hint
        assert commands.answer(db, {"chat": OTHER, "text": "!foo"}, "!") == ("not_allowed", "foo", None)
        outcome, _, reply = commands.answer(db, {"chat": GROUP, "text": "!foo"}, "!")
        assert outcome == "unknown" and "!help" in reply
        outcome, _, reply = commands.answer(db, {"chat": GROUP, "text": "!help"}, "!")
        assert outcome == "answered" and "!status – Production state" in reply
        assert commands.answer(db, {"chat": GROUP, "text": "hello"}, "!")[0] == "ignored"
    finally:
        db.close()


def test_handle_message_sends_and_logs(client, monkeypatch):
    login(client)
    from app.notifiers.whatsapp_linked import link

    sent = []
    monkeypatch.setattr(link, "send", lambda to, text: sent.append((to, text)))
    monkeypatch.setattr(commands, "MIN_INTERVAL", 0)
    db = SessionLocal()
    _status(db)
    db.close()
    commands.handle_message({"chat": GROUP, "chat_name": "Shift A", "sender_name": "Martin", "text": "!status"})
    commands.handle_message({"chat": GROUP, "text": "just chatting"})
    assert len(sent) == 1 and sent[0][0] == GROUP
    logs = client.get("/api/notifications/commands/log").json()
    assert len(logs) == 1
    assert logs[0]["outcome"] == "answered" and logs[0]["chat_name"] == "Shift A" and logs[0]["sender"] == "Martin"


def test_api_prefix_crud_preview_and_seed(client):
    login(client)
    assert client.put("/api/notifications/commands/prefix", json={"prefix": "x"}).status_code == 400
    assert client.put("/api/notifications/commands/prefix", json={"prefix": "#"}).json()["prefix"] == "#"
    assert client.get("/api/notifications/commands").json()["prefix"] == "#"
    client.put("/api/notifications/commands/prefix", json={"prefix": "!"})

    body = {"keyword": "scrap", "period": "today", "line": "{camera} {scrap}", "group_ids": [GROUP]}
    r = client.post("/api/notifications/commands", json=body)
    assert r.status_code == 201, r.text
    assert client.post("/api/notifications/commands", json=body).status_code == 409
    assert client.post("/api/notifications/commands", json={"keyword": "Bad Word"}).status_code == 422
    cid = r.json()["id"]
    assert client.patch(f"/api/notifications/commands/{cid}", json={"enabled": False}).json()["enabled"] is False
    _camera(client, "P1")
    reply = client.post("/api/notifications/commands/preview", json={**body, "camera": "p1"}).json()["reply"]
    assert re.fullmatch(r"P1 \d+\.\d%", reply), reply
    assert client.delete(f"/api/notifications/commands/{cid}").status_code == 204

    # the ready-made status command is added once, and stays deleted
    settings_store.save(commands.SEEDED_KEY, None)
    db = SessionLocal()
    try:
        commands.seed_defaults(db)
        assert [c.keyword for c in db.query(ChatCommand)] == ["status"]
        db.query(ChatCommand).delete()
        db.commit()
        commands.seed_defaults(db)
        assert db.query(ChatCommand).count() == 0
        assert db.query(CommandLog).count() == 0
    finally:
        db.close()


def test_old_camera_placeholders_still_work(client):
    login(client)
    _camera(client, "Line 1")
    db = SessionLocal()
    try:
        cmd = _status(db, header="{cameras} cams", line="{camera}|{device}")
        assert commands.render(db, cmd).splitlines()[:2] == ["1 cams", "Line 1|Line 1"]
    finally:
        db.close()


def test_active_days_lists_only_recently_active_stations(client):
    import datetime as dt

    from app.models import Station, utcnow

    login(client)
    _camera(client, "Line 1")
    _camera(client, "Line 2")
    _camera(client, "Line 3")
    db = SessionLocal()
    try:
        now = utcnow()
        st = {s.name: s for s in db.query(Station)}
        # Line 1 runs now; Line 2 last counted 3 days ago; Line 3 never counted
        st["Line 2"].last_pass_change_at = now - dt.timedelta(days=3)
        st["Line 3"].last_pass_change_at = None
        db.commit()

        everyone = commands.render(db, _status(db, keyword="all"), now=now)
        assert all(f"Line {n}:" in everyone for n in (1, 2, 3))

        week = _status(db, keyword="week", active_days=7, header="{stations} of {active_days} d")
        reply = commands.render(db, week, now=now)
        assert reply.startswith("2 of 7 d")
        assert "Line 1:" in reply and "Line 2:" in reply and "Line 3:" not in reply
        # the totals only count the listed stations
        per_station = [int(a) + int(b) for a, b in re.findall(r"OK (\d+) · NOK (\d+) · scrap", reply)]
        assert per_station[-1] == sum(per_station[:-1])

        day = _status(db, keyword="day", active_days=1)
        reply = commands.render(db, day, now=now)
        assert "Line 1:" in reply and "Line 2:" not in reply
        assert commands.render(db, day, "line 2", now=now) == "No station in production in the last day matches 'line 2'."

        # nothing active: says so instead of an empty list
        st["Line 1"].last_pass_change_at = st["Line 2"].last_pass_change_at = now - dt.timedelta(days=10)
        db.commit()
        assert commands.render(db, day, now=now) == "No station was in production in the last day."
        assert commands.answer(db, {"chat": GROUP, "text": "!week"}, "!")[2] == "No station was in production in the last 7 days."
    finally:
        db.close()


def test_api_active_days(client):
    login(client)
    body = {"keyword": "recent", "line": "{station}", "active_days": 5}
    r = client.post("/api/notifications/commands", json=body)
    assert r.status_code == 201 and r.json()["active_days"] == 5
    cid = r.json()["id"]
    assert client.post("/api/notifications/commands", json={**body, "keyword": "bad", "active_days": 0}).status_code == 422
    assert client.patch(f"/api/notifications/commands/{cid}", json={"active_days": None}).json()["active_days"] is None
    assert client.patch(f"/api/notifications/commands/{cid}", json={"active_days": 2}).json()["active_days"] == 2
    # without the field, a command lists every station as before
    r = client.post("/api/notifications/commands", json={"keyword": "all", "line": "{station}"})
    assert r.json()["active_days"] is None
