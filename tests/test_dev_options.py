"""Developer options: the WhatsApp virtual client and Signal are off unless
turned on; a server that used them before 1.21 gets them on once."""
import pytest

import fake_messengers as fake
from app import dev_options, settings_store
from app.config import settings
from app.database import SessionLocal
from app.models import NotificationProvider
from app.notifiers import signal
from app.notifiers.base import NotifierError
from app.notifiers.whatsapp import WhatsAppNotifier


def login(client):
    assert client.post("/login", data={"username": "Admin", "password": "1234"},
                       follow_redirects=False).status_code == 303


def _forget():
    settings_store.save(dev_options.KEY, None)
    dev_options.reset_cache()


def test_off_hides_and_stops(client, monkeypatch):
    login(client)
    srv, url = fake.start()
    try:
        monkeypatch.setattr(settings, "signal_api_url", url + "/signal")
        signal._account.update(number=None, checked=0.0)
        r = client.put("/api/ui-settings/dev-options", json={"whatsapp_linked": False, "signal": False})
        assert r.json() == {"whatsapp_linked": False, "signal": False}
        page = client.get("/notifications").text
        assert "WhatsApp phone</h2>" not in page and "Signal phone</h2>" not in page
        assert "signal" not in [k["key"] for k in client.get("/api/notifications/notifier-kinds").json()]
        assert client.get("/api/notifications/signal").json()["state"] == "off"
        assert client.post("/api/notifications/whatsapp/link").status_code == 409
        with pytest.raises(NotifierError, match="Developer options"):
            WhatsAppNotifier({"transport": "linked", "to": "1@g.us"}).send("x")
        with pytest.raises(NotifierError, match="Developer options"):
            signal.SignalNotifier({"to": ["group.x"]}).send("x")
        assert not [o for o in client.get("/api/chat/options").json()["options"] if o["kind"] == "signal"]

        client.put("/api/ui-settings/dev-options", json={"signal": True})
        page = client.get("/notifications").text
        assert "Signal phone</h2>" in page and "WhatsApp phone</h2>" not in page
        assert client.get("/api/notifications/signal").json()["state"] == "linked"
    finally:
        srv.shutdown()


def test_only_admins_change_them(client):
    login(client)
    client.post("/api/users", json={"username": "op", "password": "pw-op-1", "is_admin": False,
                                    "permissions": ["view_dashboard"]})
    client.get("/logout")
    assert client.post("/login", data={"username": "op", "password": "pw-op-1"},
                       follow_redirects=False).status_code == 303
    assert client.get("/api/ui-settings").json()["dev_options"] is None
    assert client.put("/api/ui-settings/dev-options", json={"signal": True}).status_code == 403


def test_upgrade_turns_on_what_was_used(client, monkeypatch):
    login(client)
    monkeypatch.setattr(settings, "signal_api_url", "")
    db = SessionLocal()
    try:
        _forget()
        assert dev_options.upgrade(db) == {"whatsapp_linked": False, "signal": False}  # a new install
        assert dev_options.upgrade(db) is None  # once
        _forget()
        db.add(NotificationProvider(name="wa", kind="whatsapp", config={"transport": "linked", "to": "1@g.us"}))
        db.commit()
        assert dev_options.upgrade(db) == {"whatsapp_linked": True, "signal": False}
        _forget()
        monkeypatch.setattr(settings, "signal_api_url", "http://signal:8080")
        assert dev_options.upgrade(db)["signal"] is True
    finally:
        db.close()
