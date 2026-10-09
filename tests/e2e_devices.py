"""Simulated devices for the end-to-end check (e2e_check.py), all in one
process. Not a test: run it in a container on the stack's network.

  Modbus/TCP server  :5020  holding registers 0-1 pass, 2-3 fail (32-bit, big word order)
  Native Mode server :2323  login, then GVJobName / GVPassCount / GVFailCount
  Data Channel       :2424  sends "DC_JOB,<pass>,<fail>" every second
  OPC UA server      :4840  ns=2;s=Line1.Pass / .Fail / .Total / .Job (tests/opcua_sim.py)
  FTP server         :2121  user e2e / e2e-ftp-pw, folder /tmp/ftp (for the backup check)

The counters grow a few parts a second, with some NOK.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))

COUNTS = {"pass": 0, "fail": 0}


def tick() -> None:
    n = 0
    while True:
        time.sleep(1)
        n += 1
        COUNTS["pass"] += 3
        if n % 4 == 0:
            COUNTS["fail"] += 1


def native_mode() -> None:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", 2323))
    srv.listen()

    def serve(conn: socket.socket) -> None:
        with conn:
            conn.sendall(b"Welcome to In-Sight(tm) 2000\r\nUser: ")
            conn.recv(256)
            conn.sendall(b"Password: ")
            conn.recv(256)
            conn.sendall(b"User Logged In\r\n")
            while True:
                data = conn.recv(256)
                if not data:
                    return
                cmd = data.decode().strip()
                reply = {"GVJobName": "NM_JOB", "GVPassCount": str(COUNTS["pass"]),
                         "GVFailCount": str(COUNTS["fail"])}.get(cmd, "0")
                conn.sendall((reply + "\r\n").encode())

    while True:
        c, _ = srv.accept()
        threading.Thread(target=serve, args=(c,), daemon=True).start()


def data_channel() -> None:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", 2424))
    srv.listen()

    def serve(conn: socket.socket) -> None:
        try:
            while True:
                conn.sendall(f"DC_JOB,{COUNTS['pass']},{COUNTS['fail']}\r\n".encode())
                time.sleep(1)
        except OSError:
            conn.close()

    while True:
        c, _ = srv.accept()
        threading.Thread(target=serve, args=(c,), daemon=True).start()


def modbus() -> None:
    from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext
    from pymodbus.server import StartAsyncTcpServer

    block = ModbusSequentialDataBlock(0, [0] * 20)
    ctx = ModbusServerContext(slaves=ModbusSlaveContext(hr=block), single=True)

    async def run() -> None:
        async def update() -> None:
            while True:
                p, f = COUNTS["pass"], COUNTS["fail"]
                block.setValues(1, [p >> 16, p & 0xFFFF, f >> 16, f & 0xFFFF])  # address 0 (+1 offset)
                await asyncio.sleep(0.5)

        asyncio.create_task(update())
        await StartAsyncTcpServer(context=ctx, address=("0.0.0.0", 5020))

    asyncio.run(run())


def opcua() -> None:
    from opcua_sim import SimServer

    SimServer(4840, auto=True).start()
    while True:
        time.sleep(3600)


def ftp() -> None:
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    os.makedirs("/tmp/ftp", exist_ok=True)
    auth = DummyAuthorizer()
    auth.add_user("e2e", "e2e-ftp-pw", "/tmp/ftp", perm="elradfmwMT")
    handler = FTPHandler
    handler.authorizer = auth
    handler.passive_ports = range(30000, 30010)
    FTPServer(("0.0.0.0", 2121), handler).serve_forever()


if __name__ == "__main__":
    for fn in (tick, native_mode, data_channel, modbus, opcua, ftp):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    print("e2e devices running", flush=True)
    while True:
        time.sleep(3600)
