"""Discord and Signal: alerts, chat commands and the Chat room, against the
fake servers in fake_messengers.py (nothing reaches real accounts)."""
import pytest

import fake_messengers as fake
from app import chatroom, commands, messengers
from app.config import settings
from app.database import SessionLocal
from app.models import ChatCommand, ChatMessage, CommandLog, Station
from app.notifiers import discord, signal
from app.notifiers.base import NotifierError

from test_api import add_station_device

ALERTS, FLOOR = "111111111111111111", "222222222222222222"
GROUP = fake.GROUPS[0]


@pytest.fixture(scope="module")
def server():
    srv, url = fake.start()
    yield url
    srv.shutdown()


@pytest.fixture(autouse=True)
def _fresh(server, monkeypatch):
    fake.state.reset()
    monkeypatch.setattr(settings, "signal_api_url", server + "/signal")
    signal._account.update(number=None, checked=0.0)
    signal._groups.update(rows=[], at=0.0)
    monkeypatch.setattr(commands, "MIN_INTERVAL", 0)
    yield
    chatroom.set_room(None)


def login(client):
    r = client.post("/login", data={"username": "Admin", "password": "1234"}, follow_redirects=False)
    assert r.status_code == 303


def bot_config(server, **extra):
    return {"mode": "bot", "bot_token": fake.TOKEN, "channel_ids": [ALERTS, FLOOR], "base_url": server + "/discord",
            **extra}


def add_provider(client, name, kind, config):
    r = client.post("/api/notifications/providers", json={"name": name, "kind": kind, "enabled": True, "config": config})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def seed_status_command():
    db = SessionLocal()
    try:
        if not db.query(ChatCommand).filter_by(keyword="status").count():
            db.add(ChatCommand(**commands.DEFAULT_STATUS, group_ids=[]))
            db.commit()
    finally:
        db.close()


def test_discord_send_bot_and_webhook(client, server):
    login(client)
    pid = add_provider(client, "Discord", "discord", bot_config(server))
    assert client.post(f"/api/notifications/providers/{pid}/test").status_code == 200
    assert {s["to"] for s in fake.state.sent} == {ALERTS, FLOOR}

    # a long message is split at line ends (Discord takes 2000 characters)
    fake.state.reset()
    discord.DiscordNotifier(bot_config(server, channel_ids=[ALERTS])).send("\n".join(["x" * 90] * 50))
    assert len(fake.state.sent) == 3 and all(len(s["text"]) <= 2000 for s in fake.state.sent)

    fake.state.reset()
    discord.DiscordNotifier({"mode": "webhook", "webhook_url": server + "/discord/webhooks/1/abc"}).send("hello")
    assert fake.state.sent == [{"to": "webhook", "text": "hello", "via": "discord"}]

    with pytest.raises(NotifierError, match="Check the bot token"):
        discord.DiscordNotifier(bot_config(server, bot_token="wrong")).send("x")
    with pytest.raises(NotifierError, match="channel 333"):
        discord.DiscordNotifier(bot_config(server, channel_ids=["333"])).send("x")


def test_discord_commands_and_chat_room(client, server):
    login(client)
    add_station_device(client, "Line 1", {"jobs": ["J1"]})
    seed_status_command()
    pid = add_provider(client, "Discord", "discord", bot_config(server))
    fake.state.discord_message(ALERTS, {"id": "1", "username": "old"}, "!status")  # before the app looked
    reader = messengers.Reader()
    assert reader.poll_once() == 0  # the first look only starts after the newest message
    assert not fake.state.sent

    # the room is the shop-floor channel
    opts = client.get("/api/chat/options").json()
    floor = next(o for o in opts["options"] if o["chat"] == FLOOR)
    assert floor["name"] == "#shop-floor" and floor["provider_id"] == pid
    assert client.put("/api/chat/room", json=floor).status_code == 200

    fake.state.discord_message(ALERTS, {"id": "7", "username": "eva", "global_name": "Eva"}, "!status")
    fake.state.discord_message(FLOOR, {"id": "7", "username": "eva", "global_name": "Eva"}, "good morning")
    assert reader.poll_once() == 2
    reply = next(s for s in fake.state.sent if s["to"] == ALERTS)
    assert "Line 1" in reply["text"]
    assert reader.poll_once() == 0  # the bot's own reply is not read as a message

    msgs = client.get("/api/chat/messages").json()
    assert [(m["direction"], m["author"], m["text"]) for m in msgs] == [("in", "Eva", "good morning")]
    r = client.post("/api/chat/messages", json={"text": "hello floor"})
    assert r.status_code == 201 and r.json()["status"] == "sent"
    assert fake.state.sent[-1] == {"to": FLOOR, "text": "**Admin:** hello floor", "via": "discord"}

    db = SessionLocal()
    try:
        log = db.query(CommandLog).order_by(CommandLog.id.desc()).first()
        assert log.chat == ALERTS and log.outcome == "answered" and log.sender == "Eva"
    finally:
        db.close()


def test_signal_link_send_commands_and_room(client, server):
    login(client)
    _, sid = add_station_device(client, "Line 1", {"jobs": ["J1"]})
    seed_status_command()  # mute / unmute work where any command is allowed
    s = client.get("/api/notifications/signal").json()
    assert s == {"state": "linked", "number": fake.NUMBER, "error": None}
    assert client.get("/api/notifications/signal/qr").headers["content-type"] == "image/png"
    groups = client.get("/api/notifications/signal/groups").json()
    assert [g["name"] for g in groups] == ["Shift leaders", "Maintenance"]

    pid = add_provider(client, "Signal", "signal", {"to": [GROUP["id"]]})
    assert client.post(f"/api/notifications/providers/{pid}/test").status_code == 200
    assert fake.state.sent[-1]["to"] == GROUP["id"] and fake.state.sent[-1]["via"] == "signal"

    # !mute from the group mutes the station and answers there
    room = next(o for o in client.get("/api/chat/options").json()["options"] if o["kind"] == "signal"
                and o["chat"] == GROUP["id"])
    client.put("/api/chat/room", json=room)
    fake.state.inbox.append({"envelope": {"source": "+10000000099", "sourceName": "Jan", "timestamp": 1,
                                          "dataMessage": {"timestamp": int(__import__("time").time() * 1000),
                                                          "message": "!mute Line 1",
                                                          "groupInfo": {"groupId": GROUP["internal_id"]}}}})
    assert messengers.Reader().poll_once() == 1
    db = SessionLocal()
    try:
        st = db.get(Station, sid)
        assert st.alerts_muted and st.muted_by == "Jan"
        kept = db.query(ChatMessage).filter_by(kind="signal").all()
        assert [(m.author, m.text) for m in kept if m.direction == "in"] == [("Jan", "!mute Line 1")]
    finally:
        db.close()
    assert "muted" in fake.state.sent[-1]["text"] and fake.state.sent[-1]["to"] == GROUP["id"]

    r = client.post("/api/chat/messages", json={"text": "on my way"})
    assert r.json()["status"] == "sent"
    assert fake.state.sent[-1] == {"to": GROUP["id"], "text": "Admin: on my way", "via": "signal"}


def test_signal_not_set_up_or_not_linked(client, server, monkeypatch):
    login(client)
    fake.state.linked = False
    assert client.get("/api/notifications/signal").json()["state"] == "not_linked"
    with pytest.raises(NotifierError, match="no phone is linked"):
        signal.SignalNotifier({"to": ["+1"]}).send("x")
    monkeypatch.setattr(settings, "signal_api_url", "")
    assert client.get("/api/notifications/signal").json()["state"] == "not_set_up"
    assert messengers.Reader().poll_once() == 0


def test_command_chats_lists_all_messengers(client, server):
    login(client)
    add_provider(client, "Discord", "discord", bot_config(server))
    add_provider(client, "Hook", "discord", {"mode": "webhook", "webhook_url": server + "/discord/webhooks/1/x"})
    r = client.get("/api/notifications/commands/chats").json()
    names = {(c["name"], c["messenger"]) for c in r["chats"]}
    assert ("#alerts", "Discord (Discord)") in names and ("Shift leaders", "Signal") in names
    assert not any(c["messenger"].startswith("Discord (Hook") for c in r["chats"])
