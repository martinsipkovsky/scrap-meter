"""Server-rendered HTML pages. Each page is gated by a permission; the nav is
built from the signed-in user's permissions so they only see what they can use.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import changelog
from ..database import get_db
from ..dependencies import require_page_permission, require_user
from ..models import PERMISSIONS, User
from ..templating import templates

router = APIRouter()


def _ctx(request: Request, user: User, **extra) -> dict:
    ctx = {"user": user, "all_permissions": PERMISSIONS}
    ctx.update(extra)
    return ctx


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(require_page_permission("view_dashboard"))):
    return templates.TemplateResponse(request, "dashboard.html", _ctx(request, user, page="dashboard"))


@router.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, user: User = Depends(require_page_permission("view_dashboard"))):
    return templates.TemplateResponse(request, "devices.html", _ctx(request, user, page="devices"))


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, user: User = Depends(require_page_permission("view_dashboard"))):
    return templates.TemplateResponse(request, "jobs.html", _ctx(request, user, page="jobs"))


@router.get("/data", response_class=HTMLResponse)
def data_page(request: Request, user: User = Depends(require_page_permission("view_data"))):
    return templates.TemplateResponse(request, "data.html", _ctx(request, user, page="data"))


@router.get("/scrap", response_class=HTMLResponse)
def scrap_page(request: Request, user: User = Depends(require_page_permission("view_data"))):
    return templates.TemplateResponse(request, "scrap.html", _ctx(request, user, page="scrap"))


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, user: User = Depends(require_page_permission("chat_room"))):
    return templates.TemplateResponse(request, "chat.html", _ctx(request, user, page="chat"))


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, user: User = Depends(require_page_permission("manage_notifications"))):
    return templates.TemplateResponse(request, "notifications.html", _ctx(request, user, page="notifications"))


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, user: User = Depends(require_page_permission("manage_users"))):
    return templates.TemplateResponse(request, "users.html", _ctx(request, user, page="users"))


@router.get("/stations", response_class=HTMLResponse)
def stations_page(request: Request, user: User = Depends(require_page_permission("view_dashboard"))):
    return templates.TemplateResponse(request, "stations.html", _ctx(request, user, page="stations"))


@router.get("/station/{station_id}", response_class=HTMLResponse)
@router.get("/device/{station_id}", response_class=HTMLResponse)  # 1.4 links
@router.get("/camera/{station_id}", response_class=HTMLResponse)  # older links
def station_page(station_id: int, request: Request, user: User = Depends(require_page_permission("view_dashboard"))):
    return templates.TemplateResponse(
        request, "station.html", _ctx(request, user, page="dashboard", station_id=station_id)
    )


@router.get("/database", response_class=HTMLResponse)
def database_page(request: Request, user: User = Depends(require_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can configure the database")
    return templates.TemplateResponse(request, "database.html", _ctx(request, user, page="database"))


@router.get("/rawdb", response_class=HTMLResponse)
def rawdb_page(request: Request, user: User = Depends(require_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can see the raw data")
    return templates.TemplateResponse(request, "rawdb.html", _ctx(request, user, page="rawdb"))


@router.get("/changelog", response_class=HTMLResponse)
def changelog_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "changelog.html",
                                      _ctx(request, user, page="changelog", releases=changelog.releases()))


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "account.html", _ctx(request, user, page="account"))
