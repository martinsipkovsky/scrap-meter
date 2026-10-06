"""A small OPC UA simulation server for testing the opcua protocol.

Run it on its own to try the app against it:

    python tests/opcua_sim.py --port 4840 [--user operator:secret] [--no-anonymous]
                              [--values-file values.json]

It serves ns=2;s=Line1.Pass / .Fail / .Total / .Job (a few parts a second,
about 3 % fail) under Objects > Line1, with security None, Basic256Sha256
Sign and SignAndEncrypt. With ``--values-file`` the counters do not run by
themselves: they take the values in the JSON file, e.g. {"Pass": 100,
"Job": "A"}, whenever it changes. ``--user`` adds a username login; ``--no-anonymous``
then requires it. The tests start it in a thread with ``SimServer``.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import tempfile
import threading
from pathlib import Path

from asyncua import Server, ua
from asyncua.crypto.cert_gen import setup_self_signed_certificate
from asyncua.crypto.permission_rules import User, UserRole
from asyncua.server.user_managers import UserManager
from cryptography.x509.oid import ExtendedKeyUsageOID

NS_URI = "urn:scrap-meter:sim"


class _Users(UserManager):
    def __init__(self, users: dict[str, str], anonymous: bool):
        self.users, self.anonymous = users, anonymous

    def get_user(self, iserver, username=None, password=None, certificate=None):
        if username is None:
            return User(role=UserRole.User) if self.anonymous else None
        if self.users.get(username) == password:
            return User(role=UserRole.User)
        return None


class SimServer:
    """Runs the server on its own thread; counters change only through set()."""

    def __init__(self, port: int, users: dict[str, str] | None = None, anonymous: bool = True,
                 auto: bool = False, values_file: str | None = None):
        self.port, self.users, self.anonymous, self.auto = port, users or {}, anonymous, auto
        self.values_file, self._mtime = values_file, None
        self.values = {"Pass": 0, "Fail": 0, "Total": 0, "Job": "JOB_A"}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        self.endpoint = f"opc.tcp://127.0.0.1:{port}/sim"

    def start(self) -> "SimServer":
        self._thread.start()
        if not self._ready.wait(30):
            raise RuntimeError("OPC UA sim server did not start")
        return self

    def stop(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)

    def set(self, **values) -> None:
        self.values.update(values)
        fut = asyncio.run_coroutine_threadsafe(self._write(), self._loop)
        fut.result(5)

    async def _write(self) -> None:
        for name, node in self._nodes.items():
            vt = ua.VariantType.String if name == "Job" else ua.VariantType.UInt32
            await node.write_value(ua.Variant(self.values[name], vt))

    async def _read_values_file(self) -> None:
        import json
        import os

        try:
            mtime = os.path.getmtime(self.values_file)
            if mtime == self._mtime:
                return
            self._mtime = mtime
            with open(self.values_file, encoding="utf-8") as f:
                new = json.load(f)
        except (OSError, ValueError):
            return
        self.values.update({k: v for k, v in new.items() if k in self.values})
        await self._write()
        print("values:", self.values, flush=True)

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        tmp = Path(tempfile.mkdtemp(prefix="opcua-sim-"))
        cert, key = tmp / "server_cert.der", tmp / "server_key.pem"
        app_uri = "urn:scrap-meter:sim-server"
        await setup_self_signed_certificate(key, cert, app_uri, "localhost",
                                            [ExtendedKeyUsageOID.SERVER_AUTH], {"commonName": "OPC UA sim"})
        server = Server(user_manager=_Users(self.users, self.anonymous))
        await server.init()
        server.set_endpoint(f"opc.tcp://0.0.0.0:{self.port}/sim")
        server.set_server_name("Scrap Meter OPC UA sim")
        await server.set_application_uri(app_uri)
        server.set_security_policy([
            ua.SecurityPolicyType.NoSecurity,
            ua.SecurityPolicyType.Basic256Sha256_Sign,
            ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
        ])
        await server.load_certificate(str(cert))
        await server.load_private_key(str(key))
        server.set_identity_tokens([ua.UserNameIdentityToken] + ([ua.AnonymousIdentityToken] if self.anonymous else []))
        idx = await server.register_namespace(NS_URI)
        line = await server.nodes.objects.add_folder(ua.NodeId("Line1", idx), "Line1")
        self._nodes = {}
        for name in ("Pass", "Fail", "Total"):
            self._nodes[name] = await line.add_variable(
                ua.NodeId(f"Line1.{name}", idx), name, ua.Variant(0, ua.VariantType.UInt32))
        self._nodes["Job"] = await line.add_variable(
            ua.NodeId("Line1.Job", idx), "Job", ua.Variant("JOB_A", ua.VariantType.String))
        async with server:
            await self._write()
            self._ready.set()
            while not self._stop.is_set():
                if self.values_file:
                    await self._read_values_file()
                elif self.auto:
                    n = random.randint(1, 4)
                    bad = sum(random.random() < 0.03 for _ in range(n))
                    self.values["Pass"] += n - bad
                    self.values["Fail"] += bad
                    self.values["Total"] += n
                    await self._write()
                try:
                    await asyncio.wait_for(self._stop.wait(), 1)
                except asyncio.TimeoutError:
                    pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=4840)
    ap.add_argument("--user", action="append", default=[], help="name:password (repeatable)")
    ap.add_argument("--no-anonymous", action="store_true")
    ap.add_argument("--values-file", help="JSON file with the values to serve (no automatic counting)")
    a = ap.parse_args()
    users = dict(u.split(":", 1) for u in a.user)
    sim = SimServer(a.port, users, anonymous=not a.no_anonymous, auto=True, values_file=a.values_file).start()
    print(f"OPC UA sim server on opc.tcp://<this host>:{a.port}/sim  (Ctrl+C to stop)", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()
