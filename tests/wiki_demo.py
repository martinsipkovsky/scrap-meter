"""Turn a database seeded by perf_seed.py into a demo for the tutorial
screenshots (docs/wiki/images). Not a test; run it once in a throwaway
stack's web container after perf_seed.py:

    PYTHONPATH=/srv python /srv/tests/wiki_demo.py --fake http://wiki-fake:8099

Stations 1-4 get running simulated devices, station 3 is stopped by hand,
station 5's device is switched off (W03) and station 6's device points at a
closed port (E02). A comment, a manual entry, a Discord provider on the fake
server (fake_messengers.py), alert rules, the status command, a Chat room
with a few messages, a second user. No real accounts, tokens or numbers.
"""
from __future__ import annotations

import argparse
import datetime as dt

from app import chatroom, comments, commands, dev_options, stations
from app.auth import hash_password
from app.database import SessionLocal
from app.models import ChatCommand, ChatMessage, Device, NotificationProvider, NotificationRule, Station, User, utcnow
from app.production import stop


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", required=True, help="base URL of fake_messengers.py")
    a = ap.parse_args()
    db = SessionLocal()
    sts = db.query(Station).order_by(Station.sort_order).all()
    for i, st in enumerate(sts, start=1):
        dev = db.get(Device, st.source_list()[0]["device_id"])
        job = st.current_job
        dev.protocol_config = {"jobs": [job], "parts_per_poll": 2, "fail_ratio": 0.03 + 0.01 * i,
                               "reset_every": 0, "job_change_every": 0}
        dev.poll_interval = 5
        dev.enabled = i <= 4 or i == 6
        if i == 6:
            dev.protocol, dev.host, dev.port, dev.protocol_config = "tcp", "127.0.0.1", 1, {}
        st.last_pass_change_at = utcnow()
        if i == 3:
            stop(st)
    db.commit()

    st1 = sts[0]
    comments.add(db, st1, "Mould changed to cavity set B, first parts checked OK.", "admin")
    stations.add_entry(db, st1, 12, 3, note="Parts sorted by hand after the camera was cleaned", user="admin")

    discord_cfg = {"mode": "bot", "bot_token": "fake-bot-token", "channel_ids": ["111111111111111111", "222222222222222222"],
                   "base_url": a.fake + "/discord"}
    prov = NotificationProvider(name="Discord: shop floor", kind="discord", config=discord_cfg, enabled=True)
    db.add(prov)
    db.flush()
    db.add_all([
        NotificationRule(name="High scrap", condition="scrap_rate", threshold=0.05, severity="alert",
                         provider_ids=[prov.id], cooldown=1800),
        NotificationRule(name="Station offline", condition="disconnected", severity="warning",
                         provider_ids=[prov.id], cooldown=600),
        NotificationRule(name="Production started / stopped", condition="production_change", severity="info",
                         provider_ids=[prov.id], cooldown=0),
    ])
    if not db.query(ChatCommand).count():
        db.add(ChatCommand(**commands.DEFAULT_STATUS, group_ids=[]))
    db.add(User(username="operator", password_hash=hash_password("demo-operator-pw"), is_active=True,
                permissions=["view_dashboard", "view_data", "manual_entry", "chat_room"]))
    db.commit()

    chatroom.set_room({"kind": "discord", "chat": "222222222222222222", "name": "#shop-floor", "provider_id": prov.id})
    now = utcnow()
    for minutes, direction, author, text in (
        (42, "in", "Eva", "Line 2 is running the new job, first parts look good"),
        (40, "out", None, "📊 Status\nLine 1: ▶ in production, job L1_A\n   OK 4210 · NOK 118 · scrap 2.7%"),
        (25, "out", "admin", "Thanks Eva, I'll check line 3 after the break"),
        (12, "in", "Tomas", "Line 3 stopped for the mould change"),
        (3, "in", "Eva", "!status Line 2"),
    ):
        db.add(ChatMessage(kind="discord", chat="222222222222222222", direction=direction, author=author, text=text,
                           status="received" if direction == "in" else "sent",
                           created_at=now - dt.timedelta(minutes=minutes)))
    db.commit()
    dev_options.save({"whatsapp_linked": False, "signal": False})
    print("demo ready")


if __name__ == "__main__":
    main()
