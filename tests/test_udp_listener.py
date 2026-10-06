"""UDP listener protocol: datagram parsing, and a camera sending real datagrams."""
import socket
import time

import pytest

from app.protocols.udp_listener import UdpListenerDriver


def login(client, user="Admin", pw="1234"):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _free_udp_port() -> int:
    for port in range(5100, 5120):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    pytest.skip("no free UDP port in 5100-5119")


def test_datagram_without_terminator_is_one_record():
    d = UdpListenerDriver("", 5100, {})
    assert d.records(b"JOB_A,10,1") == ["JOB_A,10,1"]
    assert d.records(b"A,1,0\r\nA,2,0\r\n") == ["A,1,0", "A,2,0"]
    assert d.records(b"A,1,0\nA,2,0") == ["A,1,0", "A,2,0"]


def test_udp_shares_port_range_and_uniqueness_with_tcp(client):
    login(client)
    r = client.post("/api/devices", json={"name": "U0", "host": "", "port": 23, "protocol": "udp_listen"})
    assert r.status_code == 400
    assert client.post("/api/devices", json={"name": "T1", "host": "", "port": 5110,
                                             "protocol": "tcp_listen"}).status_code == 201
    r = client.post("/api/devices", json={"name": "U1", "host": "", "port": 5110, "protocol": "udp_listen"})
    assert r.status_code == 409


def test_camera_datagrams_feed_counters(client):
    from app.poller import _listen_devices, udp_listener_manager

    login(client)
    port = _free_udp_port()
    r = client.post("/api/devices", json={
        "name": "UdpCam", "host": "", "port": port, "protocol": "udp_listen",
        "protocol_config": {"mode": "event", "job_field": 0, "status_field": 1}, "create_station": True,
    })
    assert r.status_code == 201, r.text
    sid = client.get("/api/stations").json()[0]["id"]
    udp_listener_manager.sync(_listen_devices("udp_listen"))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as cam:
            for rec in (b"JOB_U,Pass", b"JOB_U,Pass", b"JOB_U,Fail", b"JOB_U,Pass\r\n"):
                cam.sendto(rec, ("127.0.0.1", port))
                time.sleep(0.05)

            def totals():
                c = client.get(f"/api/stations/{sid}/counters").json()
                return (c[0]["total_pass"], c[0]["total_fail"]) if c else None

            # every part is counted, repeat passes included
            assert _wait(lambda: totals() == (3, 1)), totals()
        dev = client.get("/api/devices").json()[0]
        assert dev["connected"] and dev["current_job"] == "JOB_U"
    finally:
        udp_listener_manager.sync([])


def test_offline_after_silence(client):
    from app.poller import _listen_devices, udp_listener_manager

    login(client)
    port = _free_udp_port()
    client.post("/api/devices", json={
        "name": "Quiet", "host": "", "port": port, "protocol": "udp_listen",
        "protocol_config": {"offline_after": 1},
    })
    udp_listener_manager.sync(_listen_devices("udp_listen"))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as cam:
            cam.sendto(b"J,1,0", ("127.0.0.1", port))
        assert _wait(lambda: client.get("/api/devices").json()[0]["connected"])
        assert _wait(lambda: not client.get("/api/devices").json()[0]["connected"], timeout=5)
        assert "No data" in client.get("/api/devices").json()[0]["last_error"]
    finally:
        udp_listener_manager.sync([])
