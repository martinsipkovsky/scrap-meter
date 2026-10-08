"""A fake Discord bot API and a fake signal-cli-rest-api in one small HTTP
server, for tests and browser checks without real accounts.

Discord (base_url = http://host:port/discord):
  GET  /users/@me, GET /channels/<id>, GET/POST /channels/<id>/messages,
  POST /webhooks/<id>/<token>
Signal (SIGNAL_API_URL = http://host:port/signal):
  GET /v1/accounts, GET /v1/qrcodelink, GET /v1/groups/<number>,
  POST /v2/send, GET /v1/receive/<number>

Test helpers (also over HTTP, for a server running in a container):
  POST /fake/discord/<channel> {"author", "content"}  a user writes in a channel
  POST /fake/signal/<group internal id> {"source", "name", "message"}  a group message
  GET  /fake/sent   everything the app sent; POST /fake/reset

``python fake_messengers.py 8099`` runs it on its own.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOT_ID = "900000000000000001"
TOKEN = "fake-bot-token"
NUMBER = "+10000000000"
CHANNELS = {"111111111111111111": "alerts", "222222222222222222": "shop-floor"}
GROUPS = [{"id": "group.QUJDREVGRw==", "internal_id": "ABCDEFG=", "name": "Shift leaders"},
          {"id": "group.SElKS0xNTg==", "internal_id": "HIJKLMN=", "name": "Maintenance"}]
# a 1x1 PNG
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.messages = {c: [] for c in CHANNELS}  # Discord channel -> messages
        self.next_id = 1000
        self.sent = []  # [{"to", "text", "via"}]
        self.inbox = []  # Signal envelopes not yet received
        self.linked = True

    def discord_message(self, channel: str, author: dict, content: str) -> dict:
        with self.lock:
            self.next_id += 1
            m = {"id": str(1300000000000000000 + self.next_id), "channel_id": channel, "content": content,
                 "author": author, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                 "attachments": []}
            self.messages.setdefault(channel, []).append(m)
            return m


state = State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # quiet
        pass

    def _send(self, code: int, body=None, ctype="application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _path(self) -> tuple[str, dict]:
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        return path, params

    # ---- GET ----
    def do_GET(self) -> None:  # noqa: N802
        path, params = self._path()
        if path == "/fake/sent":
            return self._send(200, state.sent)
        if path.startswith("/discord/"):
            if self.headers.get("Authorization") != f"Bot {TOKEN}":
                return self._send(401, {"message": "401: Unauthorized", "code": 0})
            if path == "/discord/users/@me":
                return self._send(200, {"id": BOT_ID, "username": "scrap-bot", "bot": True})
            m = re.fullmatch(r"/discord/channels/(\d+)(/messages)?", path)
            if m and m.group(1) in CHANNELS:
                if not m.group(2):
                    return self._send(200, {"id": m.group(1), "name": CHANNELS[m.group(1)], "type": 0})
                rows = state.messages.get(m.group(1), [])
                after = params.get("after")
                if after:
                    rows = [r for r in rows if int(r["id"]) > int(after)]
                rows = sorted(rows, key=lambda r: int(r["id"]), reverse=True)  # newest first, like Discord
                return self._send(200, rows[: int(params.get("limit", 50))])
            return self._send(404, {"message": "Unknown Channel", "code": 10003})
        if path.startswith("/signal/"):
            if path == "/signal/v1/accounts":
                return self._send(200, [NUMBER] if state.linked else [])
            if path == "/signal/v1/qrcodelink":
                return self._send(200, PNG, "image/png")
            if path == f"/signal/v1/groups/{NUMBER}":
                return self._send(200, GROUPS)
            if path == f"/signal/v1/receive/{NUMBER}":
                with state.lock:
                    rows, state.inbox = state.inbox, []
                return self._send(200, rows)
            return self._send(400, {"error": "unknown path"})
        return self._send(404, {"error": "not found"})

    # ---- POST ----
    def do_POST(self) -> None:  # noqa: N802
        path, _ = self._path()
        body = self._body()
        if path == "/fake/reset":
            state.reset()
            return self._send(200, {"ok": True})
        m = re.fullmatch(r"/fake/discord/(\d+)", path)
        if m:
            author = {"id": "700000000000000007", "username": body.get("author", "operator"),
                      "global_name": body.get("author", "Operator")}
            return self._send(200, state.discord_message(m.group(1), author, body.get("content", "")))
        m = re.fullmatch(r"/fake/signal/(.+)", path)
        if m:
            ts = int(time.time() * 1000)
            with state.lock:
                state.inbox.append({"account": NUMBER, "envelope": {
                    "source": body.get("source", "+10000000099"), "sourceName": body.get("name", "Operator"),
                    "timestamp": ts, "dataMessage": {"timestamp": ts, "message": body.get("message", ""),
                                                     "groupInfo": {"groupId": m.group(1), "type": "DELIVER"}}}})
            return self._send(200, {"ok": True})
        if path.startswith("/discord/webhooks/"):
            state.sent.append({"to": "webhook", "text": body.get("content"), "via": "discord"})
            return self._send(204)
        m = re.fullmatch(r"/discord/channels/(\d+)/messages", path)
        if m:
            if self.headers.get("Authorization") != f"Bot {TOKEN}":
                return self._send(401, {"message": "401: Unauthorized", "code": 0})
            if m.group(1) not in CHANNELS:
                return self._send(404, {"message": "Unknown Channel", "code": 10003})
            msg = state.discord_message(m.group(1), {"id": BOT_ID, "username": "scrap-bot", "bot": True},
                                        body.get("content", ""))
            state.sent.append({"to": m.group(1), "text": body.get("content"), "via": "discord"})
            return self._send(200, msg)
        if path == "/signal/v2/send":
            if not state.linked or body.get("number") != NUMBER:
                return self._send(400, {"error": "User is not registered"})
            for r in body.get("recipients", []):
                state.sent.append({"to": r, "text": body.get("message"), "via": "signal"})
            return self._send(201, {"timestamp": str(int(time.time() * 1000))})
        return self._send(404, {"error": "not found"})


def start(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Start in a thread; returns (server, base URL)."""
    server = ThreadingHTTPServer(("127.0.0.1" if port == 0 else "0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 8099), Handler)
    srv.serve_forever()
