"""TCP listener protocol: record parsing, and a real camera pushing over a socket.

The end-to-end test opens the listener on a real port, connects like a camera
would, pushes records, and checks they land in the same reset-proof counters,
readings and online status as a polled camera.
"""
import socket
import time

import pytest

from app.protocols.tcp_listener import TcpListenerDriver, parse_port_range


def login(client, user="Admin", pw="1234"):
    r = client.post("/login", data={"username": user, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text


def test_counter_mode_parsing():
    d = TcpListenerDriver("", 5100, {"delimiter": ";", "job_field": 0, "pass_field": 2,
                                      "fail_field": 1, "count_field": 3})
    s = d.parse("JOB_A;7;100;107")
    assert (s.job_name, s.raw_pass, s.raw_fail, s.raw_count) == ("JOB_A", 100, 7, 107)


def test_default_job_when_record_has_none():
    d = TcpListenerDriver("", 5100, {"job_field": None, "pass_field": 0, "fail_field": 1,
                                      "default_job": "LINE1"})
    s = d.parse("5,1")
    assert (s.job_name, s.raw_pass, s.raw_fail) == ("LINE1", 5, 1)


def test_event_mode_keeps_a_running_tally_per_job():
    d = TcpListenerDriver("", 5100, {"mode": "event"})
    samples = [d.parse(r) for r in ["A,Pass", "A,Pass", "A,Fail", "A,ok"]]
    assert [(s.raw_pass, s.raw_fail) for s in samples] == [(1, 0), (2, 0), (2, 1), (3, 1)]
    # job change starts the new job's tally from zero
    s = d.parse("B,Fail")
    assert (s.job_name, s.raw_pass, s.raw_fail) == ("B", 0, 1)


def test_split_records_handles_partial_reads_and_bare_lf():
    d = TcpListenerDriver("", 5100, {})
    buf = bytearray(b"A,1,0\r\nA,2,")
    assert d.split_records(buf) == ["A,1,0"]
    buf.extend(b"0\nA,3,0\r\n")
    assert d.split_records(buf) == ["A,2,0", "A,3,0"]
    assert buf == bytearray()


def test_port_range():
    assert parse_port_range("5100-5119") == (5100, 5119)
    assert parse_port_range("6000") == (6000, 6000)


def _free_port_in_range() -> int:
    for port in range(5100, 5120):
        with socket.socket() as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    pytest.skip("no free port in 5100-5119")


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_api_rejects_port_outside_range_and_duplicates(client):
    login(client)
    r = client.post("/api/devices", json={"name": "L1", "host": "", "port": 23,
                                          "protocol": "tcp_listen"})
    assert r.status_code == 400 and "5100" in r.text
    assert client.post("/api/devices", json={"name": "L1", "host": "", "port": 5105,
                                             "protocol": "tcp_listen"}).status_code == 201
    r = client.post("/api/devices", json={"name": "L2", "host": "", "port": 5105,
                                          "protocol": "tcp_listen"})
    assert r.status_code == 409
    # a pushing camera cannot be polled
    did = client.get("/api/devices").json()[0]["id"]
    assert client.post(f"/api/devices/{did}/poll").status_code == 400


def test_camera_push_feeds_counters_and_survives_reset(client):
    from app.poller import _listen_devices, listener_manager

    login(client)
    port = _free_port_in_range()
    r = client.post("/api/devices", json={
        "name": "Pusher", "host": "", "port": port, "protocol": "tcp_listen",
        "protocol_config": {"job_field": 0, "pass_field": 1, "fail_field": 2}, "create_station": True,
    })
    assert r.status_code == 201, r.text
    sid = client.get("/api/stations").json()[0]["id"]

    listener_manager.sync(_listen_devices())
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as cam:
            assert _wait(lambda: client.get("/api/devices").json()[0]["connected"])
            # baseline, +15 pass/+1 fail, then an operator reset to 3/0
            cam.sendall(b"JOB_A,100,5\r\nJOB_A,115,6\r\n")
            cam.sendall(b"JOB_A,3,0\r\n")

            def totals():
                c = client.get(f"/api/stations/{sid}/counters").json()
                return (c[0]["total_pass"], c[0]["total_fail"]) if c else None

            assert _wait(lambda: totals() == (15 + 3, 1)), totals()  # counted from the baseline

            cam.sendall(b"JOB_B,1,1\r\n")
            assert _wait(lambda: client.get("/api/devices").json()[0]["current_job"] == "JOB_B")

        # camera hung up -> device goes offline
        assert _wait(lambda: not client.get("/api/devices").json()[0]["connected"])
        counters = client.get(f"/api/stations/{sid}/counters").json()
        assert {c["job_name"] for c in counters} == {"JOB_A", "JOB_B"}
        rows = client.get("/api/data/readings", params={"station_id": sid}).json()
        assert rows, "pushed records should be logged as readings"
    finally:
        listener_manager.sync([])


def test_listener_rejects_other_ip(client):
    from app.poller import _listen_devices, listener_manager

    login(client)
    port = _free_port_in_range()
    r = client.post("/api/devices", json={
        "name": "OnlyCam", "host": "10.9.9.9", "port": port, "protocol": "tcp_listen", "create_station": True,
    })
    sid = client.get("/api/stations").json()[0]["id"]
    listener_manager.sync(_listen_devices())
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as cam:
            cam.sendall(b"J,1,0\r\n")
            time.sleep(0.5)
        assert client.get(f"/api/stations/{sid}/counters").json() == []
    finally:
        listener_manager.sync([])
