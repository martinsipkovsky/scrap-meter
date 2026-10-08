"""Server-rendered HTML pages. Each page is gated by a permission; the nav is
built from the signed-in user's permissions so they only see what they can use.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from .. import changelog, wiki
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


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "settings.html", _ctx(request, user, page="settings"))


# ---- Tutorial (docs/wiki, app.wiki) -------------------------------------------
def _tutorial(request: Request, user: User, folder: str, name: str):
    page = wiki.render(folder, name)
    if page is None:
        raise HTTPException(404, "No such tutorial page")
    return templates.TemplateResponse(request, "tutorial.html", _ctx(
        request, user, page="tutorial", contents=wiki.contents(), current=name if folder == "wiki" else None,
        doc=page, guide=folder == "docs"))


@router.get("/tutorial", response_class=HTMLResponse)
def tutorial_home(request: Request, user: User = Depends(require_user)):
    return _tutorial(request, user, "wiki", "README")


@router.get("/tutorial/images/{name}")
def tutorial_image(name: str, _: User = Depends(require_user)):
    path = wiki.image(name)
    if path is None:
        raise HTTPException(404, "No such picture")
    return FileResponse(path, headers={"Cache-Control": "max-age=86400"})


@router.get("/tutorial/docs/{name}", response_class=HTMLResponse)
def tutorial_guide(name: str, request: Request, user: User = Depends(require_user)):
    """A longer guide from docs (the user guide, notifications, ...)."""
    return _tutorial(request, user, "docs", name)


@router.get("/tutorial/{name}", response_class=HTMLResponse)
def tutorial_page(name: str, request: Request, user: User = Depends(require_user)):
    return _tutorial(request, user, "wiki", name)
