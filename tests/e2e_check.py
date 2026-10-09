"""End-to-end check of a running stack: every tab and feature through the
HTTP API, with simulated devices (e2e_devices.py) and fake messengers
(fake_messengers.py). Not a pytest test: it changes data, so run it only on a
throwaway stack, from a container on that stack's network:

    PYTHONPATH=/srv python /srv/tests/e2e_check.py --base http://web:8000 \\
        --dev e2e-dev --fake http://e2e-fake:8099 --user admin --password ... --out /srv/tests/e2e.json

Prints one line per check and writes them as JSON (area, check, ok, detail).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import socket
import time
import traceback

import httpx

RESULTS: list[dict] = []
ADMIN_ONLY = ["/api/database", "/api/rawdb/tables", "/api/system"]


class Fail(Exception):
    pass


def check(area: str, name: str):
    """Decorator-free helper: with check(...) as c: ...; c.detail = '...'."""
    class _C:
        detail = ""

        def __enter__(self):
            return self

        def __exit__(self, et, ev, tb):
            ok = et is None
            detail = self.detail if ok else f"{et.__name__}: {ev}"
            RESULTS.append({"area": area, "check": name, "ok": ok, "detail": str(detail)[:400]})
            print(("PASS " if ok else "FAIL ") + f"[{area}] {name}" + (f" — {detail}" if detail else ""), flush=True)
            if not ok and et not in (Fail, AssertionError):
                traceback.print_exception(et, ev, tb)
            return True  # keep going
    return _C()


def wait(pred, timeout=30.0, step=1.0):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = pred()
        if last:
            return last
        time.sleep(step)
    raise Fail(f"timed out waiting ({last!r})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://web:8000")
    ap.add_argument("--web-host", default="web")
    ap.add_argument("--dev", default="e2e-dev")
    ap.add_argument("--fake", default="http://e2e-fake:8099")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--ports", default="5600-5619")
    ap.add_argument("--out")
    a = ap.parse_args()
    first_port = int(a.ports.split("-")[0])
    TCP_PORT, UDP_PORT, SLMP_PORT, PBI_PORT = first_port, first_port + 1, first_port + 2, first_port + 19
    c = httpx.Client(base_url=a.base, timeout=60, follow_redirects=False)
    fake = httpx.Client(base_url=a.fake, timeout=10)

    def get(path, **kw):
        r = c.get(path, **kw)
        if r.status_code >= 400:
            raise Fail(f"GET {path} -> {r.status_code} {r.text[:200]}")
        return r.json() if "json" in r.headers.get("content-type", "") else r

    def send(method, path, body=None, expect=None, **kw):
        r = c.request(method, path, json=body, **kw)
        if expect is not None:
            if r.status_code != expect:
                raise Fail(f"{method} {path} -> {r.status_code} (expected {expect}) {r.text[:200]}")
        elif r.status_code >= 400:
            raise Fail(f"{method} {path} -> {r.status_code} {r.text[:200]}")
        try:
            return r.json()
        except ValueError:
            return r

    # ---- sign in ------------------------------------------------------------
    with check("Sign in", "API without a login answers 401, pages redirect to /login") as k:
        assert c.get("/api/data/summary").status_code == 401
        r = c.get("/")
        assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]
    with check("Sign in", "Wrong password is refused") as k:
        assert c.post("/login", data={"username": a.user, "password": "wrong"}).status_code == 401
    with check("Sign in", "Administrator signs in") as k:
        r = c.post("/login", data={"username": a.user, "password": a.password})
        assert r.status_code == 303, r.text
        k.detail = "start page " + r.headers["location"]

    # ---- devices of every protocol, each with a station -----------------------
    dev = a.dev
    devices = {
        "simulator": dict(host="sim", port=0, protocol="simulator",
                          protocol_config={"jobs": ["SIM_A"], "parts_per_poll": 4, "fail_ratio": 0.1,
                                           "reset_every": 0, "job_change_every": 0}),
        "native": dict(host=dev, port=2323, protocol="tcp", protocol_config={
            "login": "admin", "password": "", "requests": {"job": "GVJobName", "pass": "GVPassCount",
                                                             "fail": "GVFailCount"}}),
        "datachannel": dict(host=dev, port=2424, protocol="datachannel",
                            protocol_config={"job_field": 0, "pass_field": 1, "fail_field": 2}),
        "modbus": dict(host=dev, port=5020, protocol="modbus",
                       protocol_config={"pass_register": 0, "fail_register": 2, "reg_width": 2,
                                        "word_order": "big", "job_string": "MB_JOB"}),
        "profinet": dict(host=dev, port=5020, protocol="profinet", protocol_config={
            "mode": "gateway", "gateway_host": dev, "gateway_port": 5020,
            "modbus": {"pass_register": 0, "fail_register": 2, "reg_width": 2, "job_string": "PN_JOB"}}),
        "tcp_listen": dict(host="0.0.0.0", port=TCP_PORT, protocol="tcp_listen",
                           protocol_config={"job_field": 0, "pass_field": 1, "fail_field": 2}),
        "udp_listen": dict(host="0.0.0.0", port=UDP_PORT, protocol="udp_listen",
                           protocol_config={"job_field": 0, "pass_field": 1, "fail_field": 2}),
        "slmp_listen": dict(host="0.0.0.0", port=SLMP_PORT, protocol="slmp_listen",
                            protocol_config={"mode": "counter", "pass_device": "D100", "fail_device": "D102",
                                             "default_job": "SLMP_JOB"}),
        "slmp": dict(host=a.web_host, port=SLMP_PORT, protocol="slmp",
                     protocol_config={"pass_device": "D100", "fail_device": "D102", "default_job": "PLC_JOB"}),
    }
    ids: dict[str, tuple[int, int]] = {}
    for key, d in devices.items():
        with check("Devices", f"Add a {d['protocol']} device with its station ({key})") as k:
            body = {"name": f"E2E {key}", "poll_interval": 2, "enabled": True, "create_station": True, **d}
            r = send("POST", "/api/devices", body, expect=201)
            sid = next(s["id"] for s in get("/api/stations") if s["name"] == f"E2E {key}")
            ids[key] = (r["id"], sid)
            k.detail = f"device {r['id']}, station {sid}"
    with check("Devices", "Add an OPC UA device and a station on its nodes") as k:
        r = send("POST", "/api/devices", {"name": "E2E opcua", "host": dev, "port": 4840, "protocol": "opcua",
                                         "poll_interval": 2, "protocol_config": {"endpoint": f"opc.tcp://{dev}:4840"}},
                 expect=201)
        st = send("POST", "/api/stations", {"name": "E2E opcua", "sources": [
            {"device_id": r["id"], "ok": "ns=2;s=Line1.Pass", "nok": "ns=2;s=Line1.Fail", "job": "ns=2;s=Line1.Job"}]},
                  expect=201)
        ids["opcua"] = (r["id"], st["id"])
    with check("Devices", "OPC UA browse lists the server's nodes") as k:
        r = send("POST", "/api/devices/opcua/browse", {"protocol_config": {"endpoint": f"opc.tcp://{dev}:4840"}})
        k.detail = json.dumps(r)[:160]
        assert "Line1" in json.dumps(r)

    time.sleep(6)  # listeners start on their ports

    def push_tcp(text):
        with socket.create_connection((a.web_host, TCP_PORT), timeout=5) as s:
            s.sendall(text.encode())
            time.sleep(0.5)

    def push_udp(text):
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(text.encode(), (a.web_host, UDP_PORT))

    def push_slmp(p, f):
        from app.protocols.slmp import build_write, parse_device
        with socket.create_connection((a.web_host, SLMP_PORT), timeout=5) as s:
            for spec, v in (("D100", p), ("D102", f)):
                code, num = parse_device(spec)
                s.sendall(build_write(code, num, [v & 0xFFFF]))
                s.recv(64)

    for n, (p, f) in enumerate(((10, 1), (30, 2), (60, 4))):
        try:
            push_tcp(f"TCP_JOB,{p},{f}\r\n")
            push_udp(f"UDP_JOB,{p},{f}\r\n")
            push_slmp(p, f)
        except OSError as exc:
            print("push failed", exc)
        time.sleep(3)

    def device_values(key):
        did = ids[key][0]
        return next((d for d in get("/api/devices") if d["id"] == did), {})

    for key in list(devices) + ["opcua"]:
        if key not in ids:
            continue
        with check("Devices", f"{key}: the device delivers values and its station counts") as k:
            if devices.get(key, {}).get("protocol") not in ("tcp_listen", "udp_listen", "slmp_listen") and key != "opcua":
                send("POST", f"/api/devices/{ids[key][0]}/poll")
            # a camera that connects for each record shows disconnected in between: values are what count
            push = devices.get(key, {}).get("protocol") in ("tcp_listen", "slmp_listen")
            dv = wait(lambda: (lambda d: d if (d.get("connected") or push) and d.get("last_values") else None)(
                device_values(key)), timeout=40)
            row = wait(lambda: next((s for s in get("/api/data/summary") if s["id"] == ids[key][1]
                                     and s.get("active_job") and (s["active_job"]["total_pass"] or 0) > 0), None),
                       timeout=60)
            k.detail = (f"values {json.dumps({x: dv['last_values'].get(x) for x in list(dv['last_values'])[:4]})}, "
                        f"job {row['current_job']}, OK {row['active_job']['total_pass']}")

    # ---- stations, production, problems --------------------------------------
    sim_sid = ids["simulator"][1]
    with check("Stations", "Stop and start production by hand") as k:
        send("POST", f"/api/stations/{sim_sid}/production/stop")
        assert next(s for s in get("/api/data/summary") if s["id"] == sim_sid)["production_state"] == "stopped"
        send("POST", f"/api/stations/{sim_sid}/production/start")
        assert next(s for s in get("/api/data/summary") if s["id"] == sim_sid)["production_state"] == "running"
    with check("Stations", "Reset counters sets the dashboard counters to zero") as k:
        send("POST", f"/api/stations/{sim_sid}/counters/reset")
        row = next(s for s in get("/api/data/summary") if s["id"] == sim_sid)
        assert row["active_job"]["total_pass"] < 10, row["active_job"]
        k.detail = f"OK after reset {row['active_job']['total_pass']}"
    with check("Stations", "Edit a station (idle timeout, HMI window) and the HMI framing check") as k:
        send("PATCH", f"/api/stations/{sim_sid}", {"idle_timeout_min": 20,
                                                  "hmi_windows": [{"name": "Camera", "url": a.base + "/healthz"}]})
        r = get(f"/api/stations/{sim_sid}/hmi/0/check", params={"origin": a.base})
        k.detail = json.dumps(r)[:200]
    with check("Stations", "Scrap warnings switch mutes and unmutes a station") as k:
        send("PUT", f"/api/stations/{sim_sid}/alerts", {"on": False})
        assert next(s for s in get("/api/data/summary") if s["id"] == sim_sid)["alerts_muted"]
        send("PUT", f"/api/stations/{sim_sid}/alerts", {"on": True})
        assert not next(s for s in get("/api/data/summary") if s["id"] == sim_sid)["alerts_muted"]
        assert len(get("/api/data/events")) >= 2
    with check("Dashboard", "Problem codes: E02 for a closed port, W03 for a disabled device") as k:
        r = send("POST", "/api/devices", {"name": "E2E broken", "host": "127.0.0.1", "port": 1, "protocol": "tcp",
                                         "poll_interval": 2, "create_station": True}, expect=201)
        ids["broken"] = (r["id"], next(s["id"] for s in get("/api/stations") if s["name"] == "E2E broken"))
        first = next(s for s in get("/api/data/summary") if s["id"] == ids["broken"][1])["problems"]
        row = wait(lambda: next((s for s in get("/api/data/summary") if s["id"] == ids["broken"][1]
                                 and s["problems"] and s["problems"][0]["code"] != "W01"), None), timeout=30)
        assert row["problems"][0]["code"] == "E02", row["problems"]
        send("PATCH", f"/api/devices/{ids['broken'][0]}", {"enabled": False})
        row = next(s for s in get("/api/data/summary") if s["id"] == ids["broken"][1])
        assert [p["code"] for p in row["problems"]] == ["W03"], row["problems"]
        k.detail = f"{first[0]['code'] if first else 'none'} before the first read, E02, then W03"
    with check("Dashboard", "Summary and OEE answer for every station") as k:
        rows = get("/api/data/summary")
        oee = get("/api/data/oee", params={"hours": 24})
        assert len(rows) == len(oee["stations"]) >= 11
        for s in oee["stations"]:
            assert s["availability"] is None or 0 <= s["availability"] <= 1
        k.detail = f"{len(rows)} stations, overall OEE {oee['overall']}"
    with check("Stations", "Station view with its chart over 1 h to 7 days") as k:
        for h in (1, 8, 24, 168):
            r = get(f"/api/data/stations/{sim_sid}", params={"hours": h})
            assert r["history"]["bars"]
        k.detail = f"OK {r['history']['ok']} / NOK {r['history']['nok']} over 7 days"

    # ---- jobs ------------------------------------------------------------------
    with check("Jobs", "Add a job before production and set its cycle time per shot") as k:
        j = send("POST", "/api/jobs", {"name": "E2E_NEXT"}, expect=201)
        send("PATCH", f"/api/jobs/{j['id']}", {"shot_s": 12, "pieces_per_shot": 4})
        sim_job = next(x for x in get("/api/jobs") if x["name"] == "SIM_A")
        send("PATCH", f"/api/jobs/{sim_job['id']}", {"shot_s": 2.0, "pieces_per_shot": 1})
        row = next(x for x in get("/api/jobs") if x["name"] == "E2E_NEXT")
        assert row["ideal_cycle_s"] == 3.0, row
        k.detail = "12 s per shot of 4 = 3 s per piece"
    with check("OEE", "A cycle time gives the simulator station a performance figure") as k:
        s = wait(lambda: next((x for x in get("/api/data/oee")["stations"] if x["id"] == sim_sid
                               and x["performance"] is not None), None), timeout=20)
        k.detail = f"A {s['availability']} P {s['performance']} Q {s['quality']} OEE {s['oee']}"

    # ---- manual entries, comments, data log --------------------------------------
    with check("Manual entries", "Add an entry, a correction (asks first), edit and delete") as k:
        e = send("POST", f"/api/stations/{sim_sid}/entries", {"ok": 7, "nok": 2, "note": "E2E sorted"}, expect=201)
        r = c.post(f"/api/stations/{sim_sid}/entries", json={"ok": -100000, "nok": 0})
        assert r.status_code == 409, r.text
        r = send("POST", f"/api/stations/{sim_sid}/entries", {"ok": -3, "nok": 0, "confirm": True}, expect=201)
        send("PUT", f"/api/data/readings/{e['id']}/entry", {"ok": 8, "nok": 2, "note": "E2E sorted again"})
        send("DELETE", f"/api/data/readings/{r['id']}", expect=204)
        k.detail = "entry +8/+2 kept, correction removed"
    with check("Comments", "Write, list and delete a comment") as k:
        cm = send("POST", f"/api/stations/{sim_sid}/comments", {"text": "E2E comment"}, expect=201)
        assert any(x["id"] == cm["id"] for x in get(f"/api/stations/{sim_sid}/comments"))
        send("DELETE", f"/api/comments/{cm['id']}", expect=204)
    with check("Data log", "Readings, exclude and include one, exclude a period") as k:
        rows = get("/api/data/readings", params={"station_id": sim_sid, "limit": 20})
        rid = next(r["id"] for r in rows if not r.get("manual"))
        send("PATCH", f"/api/data/readings/{rid}", {"excluded": True})
        assert any(r["id"] == rid for r in get("/api/data/readings", params={"excluded": True, "limit": 500}))
        send("PATCH", f"/api/data/readings/{rid}", {"excluded": False})
        now = dt.datetime.now(dt.timezone.utc)
        r = send("POST", "/api/data/readings/exclude", {"excluded": True, "station_id": sim_sid,
                                                       "start": (now - dt.timedelta(seconds=30)).isoformat(),
                                                       "end": now.isoformat()})
        send("POST", "/api/data/readings/exclude", {"excluded": False, "station_id": sim_sid,
                                                   "start": (now - dt.timedelta(seconds=30)).isoformat(),
                                                   "end": now.isoformat()})
        k.detail = f"period: {r}"

    # ---- scrap statistics ---------------------------------------------------------
    with check("Scrap statistics", "Today's figures add up and the Excel file downloads") as k:
        today = dt.date.today().isoformat()
        s = get("/api/data/scrap", params={"from": today, "to": today, "tz": "UTC"})
        per_station = sum(r["pass"] for r in s["per_station"] if not r["excluded_by_default"])
        assert s["overall"]["pass"] == sum(r["counted_pass"] for r in s["per_station"])
        assert s["per_hour"] is not None and s["previous"] and s["alert"]
        x = c.get("/api/data/scrap.xlsx", params={"from": today, "to": today, "tz": "UTC"})
        assert x.status_code == 200 and x.content[:2] == b"PK"
        k.detail = f"overall OK {s['overall']['pass']} NOK {s['overall']['fail']} (stations {per_station}), xlsx {len(x.content)} B"

    # ---- notifications, providers, rules, commands -------------------------------
    fake.post("/fake/reset")
    discord_cfg = {"mode": "bot", "bot_token": "fake-bot-token", "channel_ids": ["111111111111111111", "222222222222222222"],
                   "base_url": a.fake + "/discord"}
    with check("Notifications", "Discord bot and webhook providers, Test send reaches the fake Discord") as k:
        p1 = send("POST", "/api/notifications/providers", {"name": "E2E Discord", "kind": "discord", "enabled": True,
                                                            "config": discord_cfg}, expect=201)
        p2 = send("POST", "/api/notifications/providers", {"name": "E2E hook", "kind": "discord", "enabled": True,
                                                            "config": {"mode": "webhook", "webhook_url": a.fake + "/discord/webhooks/1/x"}},
                  expect=201)
        send("POST", f"/api/notifications/providers/{p1['id']}/test")
        send("POST", f"/api/notifications/providers/{p2['id']}/test")
        sent = fake.get("/fake/sent").json()
        assert {s["to"] for s in sent} >= {"111111111111111111", "222222222222222222", "webhook"}, sent
    with check("Notifications", "Signal (developer option on): linked, groups, provider, Test send") as k:
        send("PUT", "/api/ui-settings/dev-options", {"signal": True})
        st = get("/api/notifications/signal")
        assert st["state"] == "linked", st
        groups = get("/api/notifications/signal/groups")
        p3 = send("POST", "/api/notifications/providers", {"name": "E2E Signal", "kind": "signal", "enabled": True,
                                                            "config": {"to": [groups[0]["id"]]}}, expect=201)
        send("POST", f"/api/notifications/providers/{p3['id']}/test")
        assert fake.get("/fake/sent").json()[-1]["via"] == "signal"
        k.detail = f"{st['number']}, {len(groups)} groups"
    with check("Notifications", "Scrap rate rule sends an alert to Discord") as k:
        send("POST", "/api/notifications/rules", {"name": "E2E scrap", "condition": "scrap_rate", "threshold": 0.0001,
                                                 "severity": "alert", "provider_ids": [p1["id"]], "cooldown": 0,
                                                 "station_id": sim_sid, "enabled": True}, expect=201)
        sent = wait(lambda: [s for s in fake.get("/fake/sent").json() if "scrap" in (s["text"] or "").lower()
                             and "E2E simulator" in (s["text"] or "")], timeout=40)
        k.detail = sent[0]["text"][:120]
    with check("Notifications", "Alerts only in production (default): the idle broken device's alert is skipped and logged") as k:
        assert get("/api/notifications/policy") == {"production_only": True}
        send("PATCH", f"/api/devices/{ids['broken'][0]}", {"enabled": True})
        send("POST", "/api/notifications/rules", {"name": "E2E offline", "condition": "disconnected", "severity": "warning",
                                                 "provider_ids": [p1["id"]], "cooldown": 0, "enabled": True,
                                                 "station_id": ids["broken"][1]}, expect=201)
        row = wait(lambda: next((r for r in get("/api/notifications/logs") if r["skipped"]
                                 and "E2E broken" in r["message"]), None), timeout=40)
        assert not [s for s in fake.get("/fake/sent").json() if "E2E broken" in (s["text"] or "")]
        k.detail = row["detail"][:120]
    with check("Notifications", "Switch off: the disconnect rule sends an alert for the broken device") as k:
        send("PUT", "/api/notifications/policy", {"production_only": False})
        sent = wait(lambda: [s for s in fake.get("/fake/sent").json() if "E2E broken" in (s["text"] or "")], timeout=40)
        send("PUT", "/api/notifications/policy", {"production_only": True})
        k.detail = sent[0]["text"][:120]
    with check("Ping", "Devices are pinged; the Map shows server, devices, stations and their links") as k:
        send("PUT", "/api/ui-settings/ping", {"enabled": True, "interval_s": 5})
        dv = wait(lambda: next((d for d in get("/api/devices") if d["name"] == "E2E modbus" and d["ping"]
                                and d["ping"]["ok"]), None), timeout=40)
        g = get("/api/map")
        assert len(g["devices"]) == len(get("/api/devices")) and len(g["stations"]) == len(get("/api/stations"))
        assert {"station_id": ids["modbus"][1], "device_id": ids["modbus"][0]} in g["links"]
        send("PUT", "/api/map/layout", {"positions": {"server": [5, 5]}})
        assert get("/api/map")["server"]["pos"] == [5.0, 5.0]
        send("DELETE", "/api/map/layout")
        k.detail = f"E2E modbus: {dv['ping']['ms']} ms by {dv['ping']['method']} to {dv['ping']['target']}"
    with check("Ping", "Slow response rule: alert after 3 slow pings, then back to normal") as k:
        send("PUT", "/api/notifications/policy", {"production_only": False})
        rule = send("POST", "/api/notifications/rules", {"name": "E2E slow", "condition": "slow_response", "threshold": 0.0001,
                                                         "severity": "warning", "provider_ids": [p1["id"]], "cooldown": 3600,
                                                         "station_id": ids["modbus"][1], "enabled": True}, expect=201)
        slow = wait(lambda: [s for s in fake.get("/fake/sent").json() if "E2E modbus" in (s["text"] or "")
                             and "responds slowly" in s["text"]], timeout=60)
        send("PATCH", f"/api/notifications/rules/{rule['id']}", {"threshold": 100000})
        back = wait(lambda: [s for s in fake.get("/fake/sent").json() if "E2E modbus" in (s["text"] or "")
                             and "normally again" in s["text"]], timeout=30)
        send("PATCH", f"/api/notifications/rules/{rule['id']}", {"enabled": False})
        send("PUT", "/api/notifications/policy", {"production_only": True})
        send("PUT", "/api/ui-settings/ping", {"interval_s": 30})
        k.detail = slow[0]["text"][:90] + " / " + back[0]["text"][:70]
    with check("Chat commands", "!status in a Discord channel is answered there") as k:
        fake.post("/fake/discord/111111111111111111", json={"author": "Eva", "content": "!status E2E simulator"})
        reply = wait(lambda: [s for s in fake.get("/fake/sent").json() if s["to"] == "111111111111111111"
                              and "E2E simulator" in (s["text"] or "") and "OK" in s["text"]], timeout=30)
        k.detail = reply[-1]["text"][:120].replace("\n", " / ")
    with check("Chat commands", "!mute from a Signal group mutes the station, !unmute turns it on") as k:
        internal = get("/api/notifications/signal/groups")[0]["internal_id"]
        fake.post(f"/fake/signal/{internal}", json={"name": "Jan", "message": "!mute E2E simulator"})
        wait(lambda: next(s for s in get("/api/data/summary") if s["id"] == sim_sid)["alerts_muted"], timeout=30)
        fake.post(f"/fake/signal/{internal}", json={"name": "Jan", "message": "!unmute E2E simulator"})
        wait(lambda: not next(s for s in get("/api/data/summary") if s["id"] == sim_sid)["alerts_muted"], timeout=30)
        log = get("/api/notifications/commands/log")
        assert any(x["keyword"] == "mute" for x in log)
    with check("Chat commands", "Prefix change, preview, the chats list and !help") as k:
        send("PUT", "/api/notifications/commands/prefix", {"prefix": "#"})
        fake.post("/fake/discord/111111111111111111", json={"author": "Eva", "content": "#help"})
        wait(lambda: [s for s in fake.get("/fake/sent").json() if "Commands" in (s["text"] or "")], timeout=30)
        send("PUT", "/api/notifications/commands/prefix", {"prefix": "!"})
        cmd = get("/api/notifications/commands")["commands"][0]
        prev = send("POST", "/api/notifications/commands/preview", {**{k2: cmd[k2] for k2 in
                    ("keyword", "period", "hours", "header", "line", "footer", "timezone")}, "camera": ""})
        chats = get("/api/notifications/commands/chats")["chats"]
        k.detail = f"{len(chats)} chats; preview {json.dumps(prev)[:80]}"
    with check("Chat room", "A Discord channel as the room: messages in and out") as k:
        opt = next(o for o in get("/api/chat/options")["options"] if o["chat"] == "222222222222222222")
        send("PUT", "/api/chat/room", opt)
        fake.post("/fake/discord/222222222222222222", json={"author": "Tomas", "content": "E2E hello from the floor"})
        wait(lambda: any(m["text"] == "E2E hello from the floor" for m in get("/api/chat/messages")), timeout=30)
        m = send("POST", "/api/chat/messages", {"text": "E2E reply from the web"}, expect=201)
        assert m["status"] == "sent" and fake.get("/fake/sent").json()[-1]["text"].endswith("E2E reply from the web")
        send("PUT", "/api/chat/room", None)

    # ---- settings, developer options, users -------------------------------------------
    with check("Settings", "Own settings, defaults for everyone, reset") as k:
        send("PUT", "/api/ui-settings/defaults", {"accent": "teal"})
        r = send("PUT", "/api/ui-settings/mine", {"mode": "light", "start_page": "/scrap"})
        assert r["effective"]["mode"] == "light" and r["effective"]["accent"] == "teal"
        assert 'data-mode="light"' in c.get("/settings").text
        send("PUT", "/api/ui-settings/mine", {})
        send("PUT", "/api/ui-settings/defaults", {})
    with check("Settings", "Developer options off hide WhatsApp and Signal") as k:
        send("PUT", "/api/ui-settings/dev-options", {"signal": False, "whatsapp_linked": False})
        page = c.get("/notifications").text
        assert "Signal phone</h2>" not in page and "WhatsApp phone</h2>" not in page
        assert get("/api/notifications/signal")["state"] == "off"
    op = httpx.Client(base_url=a.base, timeout=30)
    with check("Users", "A user with view_dashboard only: allowed pages work, the rest is refused") as k:
        send("POST", "/api/users", {"username": "e2e-viewer", "password": "e2e-viewer-pw", "permissions": ["view_dashboard"]},
             expect=201)
        assert op.post("/login", data={"username": "e2e-viewer", "password": "e2e-viewer-pw"}).status_code == 303
        assert op.get("/api/data/summary").status_code == 200
        refused = {p: op.get(p).status_code for p in ["/api/data/readings", "/api/notifications/rules", "/api/users",
                                                      *ADMIN_ONLY]}
        assert all(v == 403 for v in refused.values()), refused
        assert op.post(f"/api/stations/{sim_sid}/production/stop").status_code == 403
        assert op.put("/api/ui-settings/dev-options", json={"signal": True}).status_code == 403
        assert op.get("/database").status_code in (302, 303, 403)
        k.detail = json.dumps(refused)
    with check("Users", "Change password, delete the user") as k:
        assert op.post("/api/account/password", json={"current_password": "e2e-viewer-pw",
                                                     "new_password": "e2e-viewer-pw2"}).status_code == 200
        uid = next(u["id"] for u in get("/api/users") if u["username"] == "e2e-viewer")
        send("DELETE", f"/api/users/{uid}", expect=204)

    # ---- export / import, database, backups, Power BI, raw data -----------------------
    with check("Devices", "Export the devices and import them back (existing names are updated)") as k:
        exp = get("/api/devices/export").json() if hasattr(get("/api/devices/export"), "json") else get("/api/devices/export")
        r = send("POST", "/api/devices/import", exp)
        k.detail = json.dumps(r)[:200]
    with check("Database", "Database tab, backup download, import of that backup") as k:
        info = get("/api/database")
        before = get("/api/rawdb/tables")
        dl = c.get("/api/database/backup/download")
        assert dl.status_code == 200 and dl.content[:2] == b"\x1f\x8b"
        first_line = gzip.decompress(dl.content).splitlines()[0]
        json.loads(first_line)
        up = c.post("/api/database/backup/inspect", files={"file": ("e2e.json.gz", dl.content, "application/gzip")})
        assert up.status_code == 200, up.text
        token = up.json()["token"]
        r = c.post("/api/database/backup/import", json={"token": token, "confirm": True})
        assert r.status_code == 200, r.text
        wait(lambda: c.post("/login", data={"username": a.user, "password": a.password}).status_code == 303, timeout=60)
        after = get("/api/rawdb/tables")
        cnt = lambda t: {x["name"]: x["rows"] for x in t}  # noqa: E731
        b, af = cnt(before), cnt(after)
        diff = {t: (b[t], af.get(t)) for t in ("stations", "devices", "jobs", "users", "notification_rules")
                if b.get(t) != af.get(t)}
        assert not diff, diff
        k.detail = f"{info.get('backend') or ''} restored; readings {b.get('readings')} -> {af.get('readings')}"
    with check("Database", "FTP backup: settings, test and a run to the FTP server") as k:
        send("PUT", "/api/database/backup/ftp", {"enabled": True, "host": dev, "port": 2121, "user": "e2e",
                                                "password": "e2e-ftp-pw", "folder": "/", "tls": False})
        send("POST", "/api/database/backup/ftp/test", {"enabled": True, "host": dev, "port": 2121, "user": "e2e",
                                                      "password": "e2e-ftp-pw", "folder": "/", "tls": False})
        r = send("POST", "/api/database/backup/ftp/run")
        k.detail = json.dumps(r)[:200]
        send("PUT", "/api/database/backup/ftp", {"enabled": False, "host": "", "port": 21, "user": "", "folder": "/"})
    with check("Power BI", "Power BI access on: the read-only login reads the views through the port") as k:
        send("PUT", "/api/database/reading/access", {"enabled": True, "port": PBI_PORT, "new_password": True})
        info = get("/api/database/reading")
        pw = info.get("password") or send("POST", "/api/database/reading/password").get("password")
        import psycopg
        conn = wait(lambda: _try_connect(psycopg, a.web_host, PBI_PORT, info.get("user", "powerbi"), pw, info), timeout=30)
        with conn:
            n = conn.execute("SELECT count(*) FROM powerbi_readings").fetchone()[0]
            denied = False
            try:
                conn.execute("SELECT count(*) FROM users")
            except Exception:  # noqa: BLE001
                denied = True
        assert n > 0 and denied
        send("POST", "/api/database/reading/daily/rebuild")
        send("PUT", "/api/database/reading/access", {"enabled": False})
        k.detail = f"{n} rows in powerbi_readings; users table refused"
    with check("System", "Live values, listening ports, poller and the sampled history") as k:
        s = get("/api/system")
        assert s["database"]["connected"] and s["database"]["server"].startswith("PostgreSQL")
        assert s["poller"]["running"] and s["poller"]["enabled"] >= 10
        ports = {p["port"] for p in s["listeners"]}
        assert {TCP_PORT, UDP_PORT, SLMP_PORT} <= ports, ports
        h = wait(lambda: (lambda x: x if x["points"] else None)(get("/api/system/history", params={"hours": 1})), timeout=90)
        k.detail = (f"CPU {s['cpu']['percent']}% of {s['cpu']['cores']} cores, memory {s['memory']['percent']}%, "
                    f"db {s['database']['size_bytes'] // 1048576} MB, ports {sorted(ports)}, {len(h['points'])} samples")
    with check("Raw data", "Tables, rows, edit a value, change log, undo, read-only SQL") as k:
        rows = get("/api/rawdb/tables/stations/rows", params={"limit": 5})
        key = rows["rows"][0]["id"] if "rows" in rows else rows["items"][0]["id"]
        send("PATCH", f"/api/rawdb/tables/stations/rows/{key}", {"values": {"sort_order": 77}})
        audit = get("/api/rawdb/audit")
        entry = audit[0] if isinstance(audit, list) else audit["rows"][0]
        send("POST", f"/api/rawdb/audit/{entry['id']}/undo")
        r = send("POST", "/api/rawdb/sql", {"query": "SELECT count(*) AS n FROM readings"})
        bad = c.post("/api/rawdb/sql", json={"query": "DELETE FROM readings"})
        assert bad.status_code >= 400
        k.detail = f"SQL {json.dumps(r)[:80]}; DELETE refused ({bad.status_code})"

    # ---- pages ---------------------------------------------------------------------
    with check("Pages", "Every page and the Tutorial open for an administrator") as k:
        pages = ["/", "/stations", f"/station/{sim_sid}", "/devices", "/jobs", "/data", "/scrap", "/chat",
                 "/notifications", "/users", "/database", "/rawdb", "/settings", "/account", "/changelog", "/tutorial",
                 "/tutorial/dashboard", "/tutorial/error-codes", "/tutorial/docs/user-guide", "/tutorial/images/dashboard.png"]
        bad = {p: c.get(p).status_code for p in pages}
        assert all(v == 200 for v in bad.values()), bad
        k.detail = f"{len(pages)} pages"

    if a.out:
        with open(a.out, "w") as f:
            json.dump(RESULTS, f, indent=1)
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")


def _try_connect(psycopg, host, port, user, pw, info):
    try:
        return psycopg.connect(host=host, port=port, user=user, password=pw,
                               dbname=info.get("database") or "cognex", connect_timeout=5, sslmode="disable")
    except Exception as exc:  # noqa: BLE001
        print("  power bi connect:", exc)
        return None


if __name__ == "__main__":
    main()
