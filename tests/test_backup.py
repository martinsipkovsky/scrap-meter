"""Backup download/import on the Database page and the FTP auto-backup."""
import datetime as dt
import gzip
import io
import json
import threading
import time

import pytest

from app import backup, backup_ftp
from app.config import settings

UTC = dt.timezone.utc


def login(client, user="Admin", pw="1234"):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))


def _camera(client, name):
    r = client.post("/api/devices", json={
        "name": name, "host": "sim", "port": 0, "protocol": "simulator",
        "protocol_config": {"jobs": ["JOB_A"], "parts_per_poll": 10, "fail_ratio": 0.1},
        "create_station": True,
    })
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    assert client.post(f"/api/devices/{did}/poll").status_code == 200
    return did


def _upload(client, content, name="b.json.gz"):
    return client.post("/api/database/backup/inspect", files={"file": (name, io.BytesIO(content), "application/gzip")})


def test_download_and_import_round_trip(client, tmp_path):
    login(client)
    _camera(client, "Cam1")
    assert client.get("/api/database/backup").json()["last_backup"] is None

    r = client.get("/api/database/backup/download")
    assert r.status_code == 200
    assert "cognex-backup-" in r.headers["content-disposition"]
    saved = r.content
    lines = [json.loads(x) for x in gzip.decompress(saved).splitlines()]
    assert lines[0]["format"] == "cognex-monitor-backup"
    assert lines[-1] == {"end": True}
    assert client.get("/api/database/backup").json()["last_backup"]["kind"] == "download"

    # change the data after the backup
    _camera(client, "Cam2")
    assert len(client.get("/api/devices").json()) == 2

    r = _upload(client, saved)
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["tables"]["devices"] == 1 and info["has_users"]

    # import needs an explicit confirmation
    assert client.post("/api/database/backup/import", json={"token": info["token"]}).status_code == 400
    r = client.post("/api/database/backup/import", json={"token": info["token"], "confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"]["devices"] == 1 and body["signed_out"]

    login(client)
    devices = client.get("/api/devices").json()
    assert [d["name"] for d in devices] == ["Cam1"]
    # the data from before the import was kept and can be downloaded
    status = client.get("/api/database/backup").json()
    assert len(status["safety_backups"]) == 1
    name = status["safety_backups"][0]["name"]
    assert name.endswith("-before-import.json.gz")
    before = client.get(f"/api/database/backup/saved/{name}")
    assert before.status_code == 200
    copy = tmp_path / name
    copy.write_bytes(before.content)
    assert backup.inspect(copy)["tables"]["devices"] == 2

    # new rows still get fresh ids after the restore
    _camera(client, "Cam3")


def test_import_rejects_bad_files(client):
    login(client)
    assert _upload(client, b"not a backup").status_code == 400
    assert _upload(client, gzip.compress(b'{"format": "something-else"}\n')).status_code == 400
    good = client.get("/api/database/backup/download").content
    data = gzip.decompress(good)
    lines = data.splitlines(keepends=True)
    cut = gzip.compress(b"".join(lines[:-1]))  # without the end marker
    r = _upload(client, cut)
    assert r.status_code == 400 and "incomplete" in r.json()["detail"]


def test_backup_admin_only(client):
    login(client)
    client.post("/api/users", json={"username": "u1", "password": "pw", "is_admin": False, "permissions": []})
    client.get("/logout")
    login(client, "u1", "pw")
    assert client.get("/api/database/backup").status_code == 403
    assert client.get("/api/database/backup/download").status_code == 403


def test_ftp_settings_keep_password(client):
    login(client)
    cfg = {"enabled": False, "host": "ftp.local", "port": 21, "user": "u", "password": "secret",
           "folder": "/b", "schedule": "interval", "interval_hours": 6, "keep": 3}
    r = client.put("/api/database/backup/ftp", json=cfg)
    assert r.status_code == 200, r.text
    assert "password" not in r.json()["ftp"] and r.json()["ftp"]["has_password"]
    cfg["password"] = None
    client.put("/api/database/backup/ftp", json=cfg)
    assert backup_ftp.load()["password"] == "secret"


def test_next_run():
    now = dt.datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
    base = {"enabled": True, "host": "h", "saved_at": now.isoformat(), "timezone": "Europe/Prague"}
    assert backup_ftp.next_run({**base, "enabled": False}, None, now) is None
    # daily 02:00 Prague (UTC+2 in October) after 10:00 UTC -> tomorrow 00:00 UTC
    daily = {**base, "schedule": "daily", "daily_time": "02:00"}
    assert backup_ftp.next_run(daily, None, now) == dt.datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
    last = dt.datetime(2026, 10, 3, 0, 0, 5, tzinfo=UTC)
    assert backup_ftp.next_run(daily, last, now) == dt.datetime(2026, 10, 4, 0, 0, tzinfo=UTC)
    # interval: right away when it never ran, then every N hours
    interval = {**base, "schedule": "interval", "interval_hours": 6}
    assert backup_ftp.next_run(interval, None, now) == now
    assert backup_ftp.next_run(interval, now, now) == now + dt.timedelta(hours=6)


# --------------------------------------------------------------------------- #
# Against a real FTP server (pyftpdlib, only installed for the test run)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def ftp_server(tmp_path):
    pytest.importorskip("pyftpdlib")
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    root = tmp_path / "ftp"
    root.mkdir()
    auth = DummyAuthorizer()
    auth.add_user("cognex", "pw", str(root), perm="elradfmw")
    handler = type("H", (FTPHandler,), {"authorizer": auth})
    server = FTPServer(("127.0.0.1", 0), handler)
    port = server.socket.getsockname()[1]
    t = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.2}, daemon=True)
    t.start()
    yield {"host": "127.0.0.1", "port": port, "user": "cognex", "password": "pw", "root": root}
    server.close_all()


def test_ftp_test_run_and_keep(client, ftp_server):
    login(client)
    cfg = {"enabled": False, "host": ftp_server["host"], "port": ftp_server["port"],
           "user": "cognex", "password": "pw", "folder": "/backups/cognex", "keep": 2}
    r = client.post("/api/database/backup/ftp/test", json=cfg)
    assert r.json()["ok"], r.json()
    bad = client.post("/api/database/backup/ftp/test", json={**cfg, "password": "wrong"}).json()
    assert not bad["ok"] and "refused" in bad["message"]

    client.put("/api/database/backup/ftp", json=cfg)
    folder = ftp_server["root"] / "backups" / "cognex"
    (folder / "other-file.txt").write_text("x")
    for i in range(3):
        (folder / f"cognex-backup-2020010{i + 1}-000000.json.gz").write_bytes(b"old")
    r = client.post("/api/database/backup/ftp/run").json()
    assert r["ok"], r
    names = sorted(p.name for p in folder.iterdir())
    assert len([n for n in names if n.startswith("cognex-backup-")]) == 2
    assert "other-file.txt" in names
    assert r["file"] in names
    assert backup.inspect((folder / r["file"]))["has_users"]
    status = client.get("/api/database/backup").json()
    assert status["last_backup"]["kind"] == "ftp" and status["ftp_last_run"]["ok"]


def test_scheduler_runs_when_due(client, ftp_server):
    login(client)
    cfg = {"enabled": True, "host": ftp_server["host"], "port": ftp_server["port"],
           "user": "cognex", "password": "pw", "folder": "auto", "schedule": "interval", "interval_hours": 1}
    assert client.put("/api/database/backup/ftp", json=cfg).status_code == 200
    result = backup_ftp.scheduler.tick()
    assert result and result["ok"], result
    # not due again until an hour later
    assert backup_ftp.scheduler.tick() is None
    time.sleep(1.1)  # file names have one-second resolution
    later = backup.utcnow() + dt.timedelta(hours=1, minutes=1)
    assert backup_ftp.scheduler.tick(later)["ok"]
    assert len(list((ftp_server["root"] / "auto").iterdir())) == 2
