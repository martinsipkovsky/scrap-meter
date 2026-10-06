"""FastAPI application entrypoint.

Creates the schema on startup, seeds the default admin, starts the background
poller, the listeners for devices that push data, the FTP backup
schedule and the linked WhatsApp client, and wires up the API
routers, HTML pages and static assets.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from . import __version__, commands, settings_store, stations
from .notifications import startup_notice
from .config import settings
from .database import Base, SessionLocal, engine, migrate_schema
from .dependencies import RedirectToLogin
from .backup_ftp import scheduler as backup_scheduler
from .notifiers.whatsapp_linked import link as whatsapp_link
from .poller import listener, poller
from .routers import (account, auth_routes, backup_admin, commands as commands_api, data, database_admin,
                      devices, notifications, pages, stations as stations_api, users)
from .seed import seed_admin
from .templating import templates


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The app owns its schema fully.
    Base.metadata.create_all(bind=engine)
    migrate_schema(engine)
    stations.upgrade(engine)  # 1.4 databases: every device becomes a station
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
    backup_scheduler.start()
    whatsapp_link.on_message = commands.handle_message  # "!status" in a WhatsApp group
    if settings.whatsapp_enabled:
        whatsapp_link.start()  # reconnects a linked phone; exits at once if none
    yield
    whatsapp_link.stop()
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
app.include_router(data.router)
app.include_router(commands_api.router)  # before notifications: /commands/... is more specific
app.include_router(notifications.router)
app.include_router(database_admin.router)
app.include_router(backup_admin.router)
# HTML pages (registered last so /api/* wins)
app.include_router(pages.router)
