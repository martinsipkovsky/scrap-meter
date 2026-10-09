"""Test configuration.

Point the app at a temporary sqlite database and disable the background
poller *before* the app package is imported, so every test module shares one
consistent set of ORM mappers (no importlib.reload, which would duplicate the
mapper registry).
"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_tmp.name}"
os.environ["POLL_ENABLED"] = "false"
os.environ["WHATSAPP_ENABLED"] = "false"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cognex-data-")
os.environ["DEFAULT_ADMIN_USER"] = "Admin"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "1234"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app
from app.seed import seed_admin
from app.database import SessionLocal


@pytest.fixture(autouse=True)
def _reset_simulator_state():
    # the simulator keeps per-device state in a module global keyed by
    # device_id; test DBs recycle id=1, so clear it between tests.
    import app.protocols.simulator as sim
    sim._STATE.clear()
    yield
    sim._STATE.clear()


@pytest.fixture(autouse=True)
def _dev_options_on():
    # most tests use the WhatsApp virtual client and Signal: on, as on a
    # server that used them before 1.21 (test_dev_options checks them off)
    from app import dev_options

    dev_options.save({"whatsapp_linked": True, "signal": True})
    yield


@pytest.fixture(autouse=True)
def _alert_policy_default():
    # alerts only during production is on by default (never saved); clear what
    # a test saved and the skipped-alert memory
    from app import notifications, settings_store

    settings_store.save(notifications.POLICY_KEY, None)
    notifications._policy = None
    notifications._skip_logged.clear()
    notifications._ping_alerted.clear()
    yield


@pytest.fixture(autouse=True)
def _ping_default():
    # Ping devices on every 30 s (never saved), no results, no saved map layout
    from app import netmap, ping, settings_store

    settings_store.save(ping.KEY, None)
    settings_store.save(netmap.KEY, None)
    ping._settings = None
    ping._results.clear()
    yield


@pytest.fixture()
def client():
    # fresh schema per test
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_admin(db)
    finally:
        db.close()
    with TestClient(app) as c:
        yield c
