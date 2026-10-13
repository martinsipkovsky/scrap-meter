"""The linked WhatsApp client without WhatsApp: a group message does not wait
for its group's name for long, and the Notifications page gets what arrived
and the client's warnings."""
import logging
import threading
import time

from app.notifiers import whatsapp_linked as w

GROUP = "120363000000000001@g.us"


def _collect():
    got, done = [], threading.Event()

    def emit(msg):
        got.append(msg)
        done.set()

    return got, done, emit


def test_message_goes_on_with_the_group_name():
    got, done, emit = _collect()
    w.emit_named(emit, {"event": "message", "text": "!status"}, lambda: "Shift A", timeout=2)
    assert done.wait(2)
    time.sleep(0.1)
    assert got == [{"event": "message", "text": "!status", "chat_name": "Shift A"}]


def test_message_does_not_wait_for_a_name_that_never_comes():
    got, done, emit = _collect()
    release = threading.Event()
    w.emit_named(emit, {"event": "message", "text": "!status"}, lambda: release.wait(5) and "late", timeout=0.2)
    assert done.wait(2)
    assert got == [{"event": "message", "text": "!status", "chat_name": None}]
    release.set()  # the name arriving later does not pass the message on twice
    time.sleep(0.3)
    assert len(got) == 1


def test_message_goes_on_when_the_lookup_fails():
    got, done, emit = _collect()

    def boom():
        raise RuntimeError("rate-overlimit")

    w.emit_named(emit, {"event": "message"}, boom, timeout=2)
    assert done.wait(1)
    assert got == [{"event": "message", "chat_name": None}]


def test_client_warnings_are_forwarded_and_limited():
    got = []
    handler = w._Forward(got.append)
    logger = logging.getLogger("whatsmeow.test")
    logger.addHandler(handler)
    try:
        logger.info("not kept")
        for i in range(w.LOG_PER_MINUTE + 20):
            logger.warning("failed to decrypt group message %d", i)
    finally:
        logger.removeHandler(handler)
    assert 1 <= len(got) <= w.LOG_PER_MINUTE
    assert got[0]["event"] == "log" and got[0]["level"] == "WARNING" and "decrypt" in got[0]["text"]


def test_status_says_what_arrived():
    link = w.WhatsAppLink()
    passed = []
    link.on_message = passed.append
    link._handle({"event": "seen", "group": False, "text": True})
    link._handle({"event": "seen", "group": True, "text": True})
    link._handle({"event": "message", "id": "A1", "chat": GROUP, "chat_name": "Shift A", "text": "!status"})
    link._handle({"event": "unreadable", "group": True, "chat": GROUP})
    link._handle({"event": "log", "ts": 1.0, "level": "ERROR", "name": "whatsmeow.Client", "text": "boom"})
    for _ in range(40):
        if passed:
            break
        time.sleep(0.05)
    assert passed and passed[0]["text"] == "!status"
    s = link.status()
    r = s["received"]
    assert (r["seen"], r["groups"], r["passed"], r["unreadable"]) == (2, 1, 1, 1)
    assert r["last_chat"] == "Shift A" and r["last_at"] and r["unreadable_at"]
    texts = [line["text"] for line in s["log"]]
    assert any("could not be decrypted" in t for t in texts) and "whatsmeow.Client: boom" in texts
