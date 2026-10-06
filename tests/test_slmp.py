"""SLMP: 3E binary frames, the app as SLMP server (camera writes to it), and
the SLMP client reading registers from an SLMP server."""
import socket
import struct
import time

import pytest

from app.protocols.base import ProtocolError
from app.protocols.slmp import (build_read, build_write, device_name, frame_length,
                                parse_device, parse_response, words_to_ascii)
from app.protocols.slmp_server import PlcMemory, SlmpServerDriver


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


def _free_port() -> int:
    for port in range(5100, 5120):
        with socket.socket() as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    pytest.skip("no free port in 5100-5119")


def ascii_words(text: str, length: int) -> list[int]:
    raw = text.encode().ljust(length * 2, b"\x00")
    return list(struct.unpack(f"<{length}H", raw))


def test_read_frame_matches_mitsubishi_reference():
    # SLMP reference manual: batch read D100, 3 points, word units, 3E binary
    assert build_read(0xA8, 100, 3).hex(" ") == (
        "50 00 00 ff ff 03 00 0c 00 10 00 01 04 00 00 64 00 00 a8 03 00")


def test_device_parsing():
    assert parse_device("D100") == (0xA8, 100)
    assert parse_device("w1A") == (0xB4, 0x1A)
    assert parse_device("ZR1000") == (0xB0, 1000)
    assert parse_device("DX10") == (0xA2, 0x10)
    assert parse_device("WAB") == (0xB4, 0xAB)
    assert device_name(0xB4, 0x1A) == "W1A"
    with pytest.raises(ProtocolError):
        parse_device("Q12")


def test_server_answers_reads_and_writes_and_rejects_unknown_commands():
    d = SlmpServerDriver("", 5100, {"preset": {"D0": 7}})
    resp, written = d.handle_frame(build_read(0xA8, 0, 2))
    assert not written and struct.unpack("<2H", parse_response(resp)) == (7, 0)
    resp, written = d.handle_frame(build_write(0xA8, 10, [1, 2, 3]))
    assert written and parse_response(resp) == b""
    assert d.memory.get("D10", 3) == [1, 2, 3]
    unknown = bytearray(build_read(0xA8, 0, 1))
    unknown[11:13] = b"\x19\x06"  # command 0619 (loopback test)
    with pytest.raises(ProtocolError, match="C059"):
        parse_response(d.handle_frame(bytes(unknown))[0])


def test_bit_units_and_bit_devices():
    mem = PlcMemory()
    mem.write_bits(0x90, 0, [1, 0, 1])          # M0..M2
    assert mem.read_words(0x90, 0, 1) == [0b101]
    mem.write_bits(0xA8, 17, [1])               # D1 bit 1
    assert mem.read_words(0xA8, 1, 1) == [2]


def test_event_mode_counts_every_trigger_change_including_repeat_passes():
    d = SlmpServerDriver("", 5100, {"mode": "event", "trigger_device": "D110",
                                    "status_device": "D111", "pass_values": [1],
                                    "job_device": "D200", "job_length": 4})
    d.handle_frame(build_write(0xA8, 200, ascii_words("JOBX", 4)))
    assert d.sample() is None                   # no inspection yet
    results = []
    for rid, status in [(1, 1), (2, 1), (3, 0), (4, 1)]:
        d.handle_frame(build_write(0xA8, 110, [rid, status]))
        s = d.sample()
        results.append((s.job_name, s.raw_pass, s.raw_fail))
    assert results == [("JOBX", 1, 0), ("JOBX", 2, 0), ("JOBX", 2, 1), ("JOBX", 3, 1)]
    d.handle_frame(build_write(0xA8, 110, [4, 1]))  # same ID again: not a new part
    assert d.sample() is None


def test_camera_writes_to_app_and_slmp_client_reads_it_back(client):
    from app.poller import _listen_devices, slmp_server_manager

    login(client)
    port = _free_port()
    r = client.post("/api/devices", json={
        "name": "SlmpCam", "host": "", "port": port, "protocol": "slmp_listen",
        "protocol_config": {"pass_device": "D100", "fail_device": "D102", "width": 2,
                            "job_device": "D200", "job_length": 4},
        "create_station": True,
    })
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    sid = client.get("/api/stations").json()[0]["id"]
    slmp_server_manager.sync(_listen_devices("slmp_listen"))

    def camera():
        return [d for d in client.get("/api/devices").json() if d["id"] == did][0]

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as cam:
            def send(frame):
                cam.sendall(frame)
                buf = b""
                while frame_length(buf) is None or len(buf) < frame_length(buf):
                    buf += cam.recv(4096)
                return parse_response(buf)

            assert _wait(lambda: camera()["connected"])
            send(build_write(0xA8, 200, ascii_words("JOB_S", 4)))
            # 32-bit counters, low word first: pass 70000, fail 5
            send(build_write(0xA8, 100, [70000 & 0xFFFF, 70000 >> 16, 5, 0]))
            send(build_write(0xA8, 100, [70010 & 0xFFFF, 70010 >> 16, 6, 0]))
            send(build_write(0xA8, 100, [3, 0, 0, 0]))  # camera counter reset

            def totals():
                c = client.get(f"/api/stations/{sid}/counters").json()
                return (c[0]["job_name"], c[0]["total_pass"], c[0]["total_fail"]) if c else None

            assert _wait(lambda: totals() == ("JOB_S", 70013, 6)), totals()
            assert words_to_ascii(list(struct.unpack("<4H", send(build_read(0xA8, 200, 4))))) == "JOB_S"

            # the app's SLMP client can poll the same registers as if they were a PLC
            r = client.post("/api/devices", json={
                "name": "PlcReader", "host": "127.0.0.1", "port": port, "protocol": "slmp",
                "protocol_config": {"pass_device": "D100", "fail_device": "D102", "width": 2,
                                    "job_device": "D200", "job_length": 4},
            })
            assert r.status_code == 201, r.text
            read = client.post(f"/api/devices/{r.json()['id']}/poll")
            assert read.status_code == 200, read.text
            assert read.json()["values"]["job"] == "JOB_S"
        assert _wait(lambda: not camera()["connected"])
    finally:
        slmp_server_manager.sync([])


def test_slmp_client_reports_unreachable_plc(client):
    login(client)
    r = client.post("/api/devices", json={
        "name": "NoPlc", "host": "127.0.0.1", "port": 1, "protocol": "slmp",
        "protocol_config": {"pass_device": "D100", "timeout": 1},
    })
    read = client.post(f"/api/devices/{r.json()['id']}/poll")
    assert read.status_code == 502 and "SLMP" in read.text
