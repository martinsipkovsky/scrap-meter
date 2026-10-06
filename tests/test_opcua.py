"""OPC UA client protocol against the local simulation server (tests/opcua_sim.py)."""
import socket

import pytest

from app.database import SessionLocal
from app.models import CounterState, Device
from app.protocols import ProtocolError, get_driver, opcua

from opcua_sim import SimServer
from test_api import login

NODES = {"pass_node": "ns=2;s=Line1.Pass", "fail_node": "ns=2;s=Line1.Fail",
         "count_node": "ns=2;s=Line1.Total", "job_node": "ns=2;s=Line1.Job"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def sim():
    server = SimServer(_free_port(), users={"operator": "secret"}).start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def secure_sim():
    server = SimServer(_free_port(), users={"operator": "secret"}, anonymous=False).start()
    yield server
    server.stop()


def _read(server, **cfg):
    return get_driver("opcua", "", 0, {"endpoint": server.endpoint, **NODES, **cfg}).read()


def test_reads_counters_and_job(sim):
    sim.set(Pass=120, Fail=4, Total=124, Job="JOB_A")
    s = _read(sim)
    assert (s.job_name, s.raw_pass, s.raw_fail, s.raw_count) == ("JOB_A", 120, 4, 124)


@pytest.mark.parametrize("mode", ["Sign", "SignAndEncrypt"])
def test_secure_modes_with_login(secure_sim, mode):
    secure_sim.set(Pass=7, Fail=1, Total=8)
    s = _read(secure_sim, security_mode=mode, security_policy="Basic256Sha256",
              username="operator", password="secret")
    assert (s.raw_pass, s.raw_fail) == (7, 1)
    cert, key = opcua.cert_paths()
    assert cert.is_file() and key.is_file()  # generated once, in DATA_DIR


def test_login_errors_are_clear(secure_sim):
    with pytest.raises(ProtocolError, match="refused the login"):
        _read(secure_sim, username="operator", password="wrong")
    with pytest.raises(ProtocolError, match="refused the login"):
        _read(secure_sim)  # anonymous not allowed


def test_unreachable_server():
    with pytest.raises(ProtocolError, match="connect|timeout"):
        get_driver("opcua", "", 0, {"endpoint": f"opc.tcp://127.0.0.1:{_free_port()}", **NODES,
                                    "timeout": 1}).read()


def test_bad_node_id(sim):
    with pytest.raises(ProtocolError, match="pass node"):
        _read(sim, pass_node="ns=2;s=Nope")


def test_browse(sim):
    root = opcua.browse(sim.endpoint, {})
    line = next(c for c in root["children"] if c["name"] == "Line1")
    assert line["has_children"]
    sim.set(Pass=5)
    children = opcua.browse(sim.endpoint, {}, line["node_id"])["children"]
    by_name = {c["name"]: c for c in children}
    assert by_name["Pass"]["node_id"] == "ns=2;s=Line1.Pass"
    assert by_name["Pass"]["value"] == 5 and by_name["Pass"]["data_type"] == "UInt32"
    assert by_name["Job"]["value"] == "JOB_A"


def test_device_poll_survives_counter_reset(client, sim):
    """Same reset-proof accumulation as every other protocol, through the API."""
    login(client)
    sim.set(Pass=100, Fail=10, Total=110, Job="JOB_A")
    r = client.post("/api/devices", json={
        "name": "Line 1", "host": "", "port": 0, "protocol": "opcua",
        "protocol_config": {"endpoint": sim.endpoint, **NODES, "username": "operator", "password": "secret"},
        "create_station": True,
    })
    assert r.status_code == 201, r.text
    dev = r.json()
    assert (dev["host"], dev["port"]) == ("127.0.0.1", sim.port)  # taken from the endpoint
    assert dev["protocol_config"]["password"] == "********"        # never sent back
    # the baseline, +30 OK (starts production), a reset to zero, then +20
    for p, f in ((100, 10), (130, 12), (5, 1), (25, 2)):
        sim.set(Pass=p, Fail=f, Total=p + f)
        assert client.post(f"/api/devices/{dev['id']}/poll").status_code == 200
    db = SessionLocal()
    try:
        sid = client.get("/api/stations").json()[0]["id"]
        st = db.query(CounterState).filter_by(station_id=sid, is_active=True).one()
        assert (st.total_pass, st.total_fail) == (30 + 5 + 20, 2 + 1 + 1)
        # saving the form with the masked password keeps the real one
        cfg = {**dev["protocol_config"], "password": "********"}
        assert client.patch(f"/api/devices/{dev['id']}", json={"protocol_config": cfg}).status_code == 200
        db.expire_all()
        assert db.get(Device, dev["id"]).protocol_config["password"] == "secret"
    finally:
        db.close()


def test_browse_and_certificate_api(client, sim):
    login(client)
    r = client.post("/api/devices/opcua/browse", json={"protocol_config": {"endpoint": sim.endpoint},
                                                       "node_id": "ns=2;s=Line1"})
    assert r.status_code == 200, r.text
    assert {c["name"] for c in r.json()["children"]} >= {"Pass", "Fail", "Total", "Job"}
    r = client.post("/api/devices/opcua/browse", json={"protocol_config": {"endpoint": "http://x"}})
    assert r.status_code == 502
    cert = client.get("/api/devices/opcua/certificate")
    assert cert.status_code == 200 and cert.content[:1] == b"\x30"  # DER sequence
