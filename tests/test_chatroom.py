"""The Chat room tab: choosing the room, sending from the web, keeping what
arrives in the chat and what the app sends there, the permission."""
import threading
import time

import httpx
import pytest

from app import chatroom, commands, notifications
from app.auth import hash_password
from app.database import SessionLocal, engine
from app.models import ChatCommand, ChatMessage, Meta, NotificationProvider, User
from app.notifiers.base import NotifierError
from app.notifiers.whatsapp_linked import link

GROUP = "120363000000000001@g.us"
OTHER = "120363000000000002@g.us"


def login(client, user="Admin", pw="1234"):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text


@pytest.fixture(autouse=True)
def _no_room():
    chatroom.set_room(None)
    yield
    chatroom.set_room(None)


@pytest.fixture()
def wa(monkeypatch):
    """A connected linked phone whose requests are recorded, not sent."""
    sent = []

    def request(cmd, timeout=30, **args):
        if link.state != "connected":
            raise NotifierError("WhatsApp is not connected (error).")
        if cmd == "send":
            sent.append((args["to"], args["text"]))
            return True
        if cmd == "groups":
            return [{"id": GROUP, "name": "Shift A"}, {"id": OTHER, "name": "Other"}]
        raise AssertionError(cmd)

    monkeypatch.setattr(link, "request", request)
    monkeypatch.setattr(link, "state", "connected")
    return sent


def _user(name, perms):
    db = SessionLocal()
    try:
        db.add(User(username=name, password_hash=hash_password("pw"), permissions=perms))
        db.commit()
    finally:
        db.close()


def _rows():
    db = SessionLocal()
    try:
        return [(m.direction, m.author, m.text, m.status) for m in db.query(ChatMessage).order_by(ChatMessage.id)]
    finally:
        db.close()


def test_choose_room_and_send(client, wa):
    login(client)
    assert client.get("/api/chat/room").json()["room"] is None
    opts = client.get("/api/chat/options").json()
    assert [o["name"] for o in opts["options"]] == ["Shift A", "Other"]
    r = client.put("/api/chat/room", json=opts["options"][0])
    assert r.status_code == 200 and r.json()["room"]["chat"] == GROUP and r.json()["connected"]
    assert client.get("/chat").status_code == 200

    m = client.post("/api/chat/messages", json={"text": "Line 2 is down"}).json()
    assert m["status"] == "sent" and m["author"] == "Admin"
    assert wa == [(GROUP, "*Admin:* Line 2 is down")]
    assert _rows() == [("out", "Admin", "Line 2 is down", "sent")]
    assert client.post("/api/chat/messages", json={"text": "  "}).status_code == 400

    # a failed send is kept with its error
    link.state = "error"
    m = client.post("/api/chat/messages", json={"text": "again"}).json()
    assert m["status"] == "failed" and "not connected" in m["error"]
    assert [x["text"] for x in client.get("/api/chat/messages").json()] == ["Line 2 is down", "again"]

    # clearing the room keeps the history
    client.put("/api/chat/room", json=None)
    assert client.get("/api/chat/room").json()["room"] is None
    assert client.get("/api/chat/messages").json() == [] and len(_rows()) == 2


def test_incoming_messages_and_commands(client, wa, monkeypatch):
    login(client)
    client.put("/api/chat/room", json={"kind": "whatsapp", "chat": GROUP, "name": "Shift A"})
    handled = []
    monkeypatch.setattr(commands, "handle_message", lambda msg: handled.append(msg["text"]))
    now = time.time()
    chatroom.on_whatsapp_message({"id": "A1", "ts": now, "chat": GROUP, "sender_name": "Jana", "text": "hello"})
    chatroom.on_whatsapp_message({"id": "A1", "ts": now, "chat": GROUP, "sender_name": "Jana", "text": "hello"})
    chatroom.on_whatsapp_message({"id": "B1", "ts": now, "chat": OTHER, "sender_name": "Ivo", "text": "!status"})
    assert _rows() == [("in", "Jana", "hello", "received")]  # once, and only the room's chat
    assert handled == ["hello", "hello", "!status"]  # every message still reaches the commands

    first = client.get("/api/chat/messages").json()
    chatroom.on_whatsapp_message({"id": "A2", "ts": now, "chat": GROUP, "sender_name": "Jana", "text": "[photo]",
                                  "media": True})
    after = client.get(f"/api/chat/messages?after={first[-1]['id']}").json()
    assert [m["text"] for m in after] == ["[photo]"]


def test_alerts_and_web_commands_show_in_room(client, wa, monkeypatch):
    login(client)
    client.put("/api/chat/room", json={"kind": "whatsapp", "chat": GROUP, "name": "Shift A"})
    db = SessionLocal()
    try:
        db.add(NotificationProvider(name="WA", kind="whatsapp", config={"transport": "linked", "to": [GROUP, OTHER]}))
        db.commit()
        db.add(ChatCommand(**commands.DEFAULT_STATUS, group_ids=[]))
        db.commit()
        notifications.dispatch(db, "High scrap", severity="alert")
    finally:
        db.close()
    assert ("out", None, "🚨 ALERT · High scrap", "sent") in _rows()
    assert len(_rows()) == 1  # the copy sent to the other group is not in the room

    # "!status" written on the web is answered in the group like one typed there
    monkeypatch.setattr(commands, "MIN_INTERVAL", 0)
    client.post("/api/chat/messages", json={"text": "!help"})
    # the answer runs on its own thread: wait until it is done (logged)
    for t in threading.enumerate():
        if t.name == "chatroom-command":
            t.join(10)
    rows = _rows()
    assert rows[1] == ("out", "Admin", "!help", "sent")
    assert rows[2][0] == "out" and rows[2][1] is None and rows[2][2].startswith("Commands")


def test_telegram_room(client, monkeypatch):
    login(client)
    calls = []

    def fake_post(url, json=None, timeout=None, **kw):
        calls.append((url.rsplit("/", 1)[-1], json))
        result = {"title": "Maintenance"} if url.endswith("getChat") else {"message_id": 5}
        return httpx.Response(200, json={"ok": True, "result": result})

    monkeypatch.setattr(httpx, "post", fake_post)
    db = SessionLocal()
    try:
        p = NotificationProvider(name="TG", kind="telegram", config={"bot_token": "1:x", "chat_ids": ["-100"]})
        db.add(p)
        db.commit()
        pid = p.id
    finally:
        db.close()
    monkeypatch.setattr(chatroom.telegram_reader, "sync", lambda: None)  # no polling thread in tests
    opts = client.get("/api/chat/options").json()["options"]
    assert opts == [{"kind": "telegram", "chat": "-100", "name": "Maintenance", "provider_id": pid,
                     "messenger": "Telegram (TG)"}]
    assert client.put("/api/chat/room", json={"kind": "telegram", "chat": "-100"}).status_code == 400
    client.put("/api/chat/room", json=opts[0])
    client.post("/api/chat/messages", json={"text": "Hi"})
    assert calls[-1] == ("sendMessage", {"chat_id": "-100", "text": "Admin: Hi", "disable_notification": False})

    room = chatroom.room()
    chatroom.TelegramReader.handle(room, {"update_id": 1, "message": {
        "message_id": 9, "date": 1700000000, "chat": {"id": -100}, "from": {"first_name": "Eva"}, "text": "ok"}})
    chatroom.TelegramReader.handle(room, {"update_id": 2, "message": {
        "message_id": 9, "date": 1700000000, "chat": {"id": -999}, "text": "elsewhere"}})
    assert _rows() == [("out", "Admin", "Hi", "sent"), ("in", "Eva", "ok", "received")]


def test_permission_and_upgrade(client, wa):
    _user("worker", ["view_dashboard"])
    _user("chatter", ["chat_room"])
    login(client, "worker", "pw")
    assert client.get("/api/chat/room").status_code == 403
    assert client.get("/chat").status_code == 403
    login(client, "chatter", "pw")
    assert client.get("/api/chat/room").json()["can_choose"] is False
    assert client.get("/api/chat/options").status_code == 403
    assert client.put("/api/chat/room", json={"kind": "whatsapp", "chat": GROUP}).status_code == 403

    # users of a database from before the Chat room get the permission once
    db = SessionLocal()
    try:
        db.query(Meta).filter(Meta.key == chatroom.UPGRADE_KEY).delete()
        db.commit()
    finally:
        db.close()
    assert chatroom.upgrade(engine) == 1
    assert chatroom.upgrade(engine) == 0
    login(client, "worker", "pw")
    assert client.get("/api/chat/room").status_code == 200
