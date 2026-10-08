"""Notification rules, providers, logs, a test-send endpoint, the linked
WhatsApp phone (QR login, groups, log out) and the linked Signal phone."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from .. import dev_options, notifications, notifiers
from ..database import get_db
from ..dependencies import require_permission
from ..models import NotificationLog, NotificationProvider, NotificationRule, User
from ..notifiers import signal
from ..notifiers.base import NotifierError
from ..notifiers.whatsapp_linked import link as whatsapp_link
from ..schemas import ProviderCreate, ProviderUpdate, RuleCreate, RuleUpdate

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

@router.get("/notifier-kinds")
def notifier_kinds(_: User = Depends(require_permission("manage_notifications"))):
    # Signal only while it is on in the Developer options
    return [k for k in notifiers.describe() if k["key"] != "signal" or dev_options.enabled("signal")]


# ---- rules ----------------------------------------------------------------
@router.get("/conditions")
def conditions(_: User = Depends(require_permission("manage_notifications"))):
    """What a rule can send, and the severities it can be sent with."""
    return {
        "conditions": [{"key": k, **v} for k, v in notifications.CONDITIONS.items()],
        "severities": [{"key": k, "label": v} for k, v in notifications.SEVERITIES.items()],
    }


def _check_rule(data: dict) -> None:
    if data.get("condition") is not None and data["condition"] not in notifications.CONDITIONS:
        raise HTTPException(400, f"condition must be one of {sorted(notifications.CONDITIONS)}")
    if data.get("severity") is not None and data["severity"] not in notifications.SEVERITIES:
        raise HTTPException(400, f"severity must be one of {sorted(notifications.SEVERITIES)}")
    if data.get("thresholds"):
        data["thresholds"] = {str(k): v for k, v in data["thresholds"].items() if v is not None}


@router.get("/rules")
def list_rules(db: Session = Depends(get_db), _: User = Depends(require_permission("manage_notifications"))):
    return db.query(NotificationRule).order_by(NotificationRule.id).all()


@router.post("/rules", status_code=201)
def create_rule(
    payload: RuleCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_notifications")),
):
    data = payload.model_dump()
    _check_rule(data)
    rule = NotificationRule(**data)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.patch("/rules/{rule_id}")
def update_rule(
    rule_id: int,
    payload: RuleUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_notifications")),
):
    rule = db.get(NotificationRule, rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    data = payload.model_dump(exclude_unset=True)
    _check_rule(data)
    for key, value in data.items():
        setattr(rule, key, value)
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int, db: Session = Depends(get_db), _: User = Depends(require_permission("manage_notifications"))):
    rule = db.get(NotificationRule, rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    db.delete(rule)
    db.commit()


# ---- providers ------------------------------------------------------------
@router.get("/providers")
def list_providers(db: Session = Depends(get_db), _: User = Depends(require_permission("manage_notifications"))):
    return db.query(NotificationProvider).order_by(NotificationProvider.id).all()


@router.post("/providers", status_code=201)
def create_provider(
    payload: ProviderCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_notifications")),
):
    if payload.kind not in notifiers.available():
        raise HTTPException(400, f"Unknown notifier kind: {payload.kind}")
    provider = NotificationProvider(**payload.model_dump())
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


@router.patch("/providers/{provider_id}")
def update_provider(
    provider_id: int,
    payload: ProviderUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_notifications")),
):
    provider = db.get(NotificationProvider, provider_id)
    if not provider:
        raise HTTPException(404, "Provider not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(provider, key, value)
    db.commit()
    db.refresh(provider)
    return provider


@router.delete("/providers/{provider_id}", status_code=204)
def delete_provider(provider_id: int, db: Session = Depends(get_db), _: User = Depends(require_permission("manage_notifications"))):
    provider = db.get(NotificationProvider, provider_id)
    if not provider:
        raise HTTPException(404, "Provider not found")
    db.delete(provider)
    db.commit()


@router.post("/providers/{provider_id}/test")
def test_provider(
    provider_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("manage_notifications")),
):
    provider = db.get(NotificationProvider, provider_id)
    if not provider:
        raise HTTPException(404, "Provider not found")
    try:
        notifier = notifiers.get_notifier(provider.kind, provider.config)
        notifier.send("Scrap Meter test notification ✅")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Send failed: {exc}") from exc
    return {"ok": True}


# ---- linked WhatsApp phone ---------------------------------------------------
@router.get("/whatsapp")
def whatsapp_status(_: User = Depends(require_permission("manage_notifications"))):
    return whatsapp_link.status()


@router.post("/whatsapp/link")
def whatsapp_start_link(_: User = Depends(require_permission("manage_notifications"))):
    """Start the client; without a linked phone it shows a QR code to scan."""
    if not dev_options.enabled("whatsapp_linked"):
        raise HTTPException(409, "The WhatsApp virtual client is off (Settings → Developer options)")
    whatsapp_link.start(pair=True)
    return whatsapp_link.status()


@router.post("/whatsapp/cancel")
def whatsapp_cancel(_: User = Depends(require_permission("manage_notifications"))):
    """Stop a QR login that is waiting to be scanned."""
    if whatsapp_link.state == "connected":
        raise HTTPException(400, "A phone is linked; use Log out to unlink it")
    whatsapp_link.stop()
    whatsapp_link.state, whatsapp_link.qr, whatsapp_link.error = "not_linked", None, None
    whatsapp_link.start()  # a phone linked earlier reconnects; otherwise it exits at once
    return whatsapp_link.status()


@router.post("/whatsapp/logout")
def whatsapp_logout(_: User = Depends(require_permission("manage_notifications"))):
    """Unlink the phone: the app disappears from the phone's Linked devices."""
    try:
        whatsapp_link.logout()
    except NotifierError as exc:
        raise HTTPException(502, str(exc)) from exc
    return whatsapp_link.status()


@router.get("/whatsapp/groups")
def whatsapp_groups(_: User = Depends(require_permission("manage_notifications"))):
    try:
        return whatsapp_link.groups()
    except NotifierError as exc:
        raise HTTPException(409, str(exc)) from exc


# ---- linked Signal phone (signal-cli-rest-api, app.notifiers.signal) -------------
@router.get("/signal")
def signal_status(_: User = Depends(require_permission("manage_notifications"))):
    return signal.status()


@router.get("/signal/qr")
def signal_qr(_: User = Depends(require_permission("manage_notifications"))):
    """A QR code (PNG) to link the Signal service to a phone."""
    try:
        return Response(signal.qr_png(), media_type="image/png", headers={"Cache-Control": "no-store"})
    except NotifierError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/signal/groups")
def signal_groups(_: User = Depends(require_permission("manage_notifications"))):
    try:
        return signal.groups(refresh=True)
    except NotifierError as exc:
        raise HTTPException(409, str(exc)) from exc


# ---- logs -----------------------------------------------------------------
@router.get("/logs")
def list_logs(limit: int = 100, db: Session = Depends(get_db), _: User = Depends(require_permission("manage_notifications"))):
    rows = db.query(NotificationLog).order_by(NotificationLog.created_at.desc()).limit(min(limit, 500)).all()
    return [
        {
            "id": r.id,
            "message": r.message,
            "delivered": r.delivered,
            "detail": r.detail,
            "created_at": r.created_at,
        }
        for r in rows
    ]
