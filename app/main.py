"""FastAPI application entrypoint.

Creates the schema on startup, seeds the default admin, starts the background
poller, the listeners for devices that push data, the FTP backup
schedule, the linked WhatsApp client and the Chat room, and wires up the API
routers, HTML pages and static assets.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from . import __version__, chatroom, commands, daily, dev_options, jobs, messengers, powerbi_access, reporting, settings_store, stations
from .notifications import startup_notice
from .config import settings
from .database import Base, SessionLocal, active_url, engine, migrate_schema
from .dependencies import RedirectToLogin
from .backup_ftp import scheduler as backup_scheduler
from .notifiers.whatsapp_linked import link as whatsapp_link
from .poller import listener, poller
from .routers import (account, auth_routes, backup_admin, chatroom as chatroom_api, commands as commands_api, comments as comments_api, data,
                      database_admin,
                      devices, jobs as jobs_api, notifications, pages, rawdb, stations as stations_api,
                      ui_settings as ui_settings_api, users)
from .seed import seed_admin
from .templating import templates

log = logging.getLogger("cognex.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The app owns its schema fully.
    Base.metadata.create_all(bind=engine)
    migrate_schema(engine)
    stations.upgrade(engine)  # 1.4 databases: every device becomes a station
    jobs.upgrade(engine)  # 1.6 databases: station cycle times move to the jobs
    reporting.ensure_views(engine)  # read-only views for Power BI and other reports
    chatroom.upgrade(engine)  # users from before the Chat room get its permission
    with SessionLocal() as db:
        dev_options.upgrade(db)  # 1.21: servers that used WhatsApp linking / Signal keep them on
    if powerbi_access.load().get("enabled"):
        error = powerbi_access.apply(engine, active_url)  # the Power BI port, switched on on the Database tab
        if error:
            log.warning("Power BI access: %s", error)
    db = SessionLocal()
    try:
        seed_admin(db)
        commands.seed_defaults(db)
    finally:
        db.close()
    if settings.poll_enabled:
        poller.start()
        listener.start()
        previous = settings_store.load("app_version")
        if previous != __version__:
            settings_store.save("app_version", __version__)
        startup_notice(__version__, previous)
        daily.scheduler.start()  # daily data for reports
        messengers.reader.start()  # Discord channels and Signal groups: commands and the Chat room
    backup_scheduler.start()
    chatroom.install()  # the Chat room keeps its chat's messages and hands "!status" to the commands
    if settings.whatsapp_enabled and dev_options.enabled("whatsapp_linked"):
        whatsapp_link.start()  # reconnects a linked phone; exits at once if none
    yield
    whatsapp_link.stop()
    chatroom.telegram_reader.stop()
    messengers.reader.stop()
    daily.scheduler.stop()
    powerbi_access.forwarder.stop()
    backup_scheduler.stop()
    listener.stop()
    poller.stop()


app = FastAPI(title="Scrap Meter", lifespan=lifespan)

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.exception_handler(RedirectToLogin)
async def _redirect_to_login(request: Request, exc: RedirectToLogin):
    return RedirectResponse(url="/login", status_code=303)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__}


# JSON API
app.include_router(auth_routes.router)
app.include_router(account.router)
app.include_router(users.router)
app.include_router(devices.router)
app.include_router(stations_api.router)
app.include_router(jobs_api.router)
app.include_router(comments_api.router)
app.include_router(data.router)
app.include_router(commands_api.router)  # before notifications: /commands/... is more specific
app.include_router(notifications.router)
app.include_router(chatroom_api.router)
app.include_router(database_admin.router)
app.include_router(backup_admin.router)
app.include_router(rawdb.router)
app.include_router(ui_settings_api.router)
# HTML pages (registered last so /api/* wins)
app.include_router(pages.router)
