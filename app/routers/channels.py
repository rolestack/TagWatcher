import uuid
import json
import logging

from fastapi import APIRouter, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.deps import CurrentUser, DB
from app.models.notification import NotificationChannel, ChannelType
from app.models.space import Space
from app.schemas.notification import NotificationChannelUpdate
from app.services.notification_service import NotificationService
from app.config import settings
from app.templates_setup import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/channels", tags=["channels"])
notification_service = NotificationService()

_ERR_NOT_FOUND = "Channel not found"


def _require_admin(user) -> None:
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


async def _link_spaces(db, channel: NotificationChannel, space_ids: list[str]) -> None:
    ids = []
    for s in space_ids:
        try:
            ids.append(uuid.UUID(s))
        except (ValueError, TypeError):
            continue
    if ids:
        spaces = (await db.execute(select(Space).where(Space.id.in_(ids)))).scalars().all()
        channel.linked_spaces = list(spaces)
    else:
        channel.linked_spaces = []


@router.get("", response_class=HTMLResponse)
async def list_channels(request: Request, user: CurrentUser, db: DB):
    _require_admin(user)
    result = await db.execute(
        select(NotificationChannel).options(selectinload(NotificationChannel.linked_spaces))
    )
    channels = result.scalars().all()
    spaces = (await db.execute(select(Space))).scalars().all()
    spaces_data = [{"id": str(s.id), "name": s.name} for s in spaces]
    return templates.TemplateResponse(
        request,
        "channels/list.html",
        {
            "user": user,
            "channels": channels,
            "spaces": spaces,
            "spaces_data": spaces_data,
            "channel_types": [ct.value for ct in ChannelType],
        },
    )


@router.post("", response_class=RedirectResponse)
async def add_channel(
    request: Request,
    user: CurrentUser,
    db: DB,
    name: str = Form(...),
    channel_type: str = Form(...),
    config_json: str = Form("{}"),
    is_active: bool = Form(True),
    space_ids: list[str] = Form(default=[]),
):
    _require_admin(user)
    try:
        ct = ChannelType(channel_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid channel type: {channel_type}")
    try:
        config = json.loads(config_json)
    except json.JSONDecodeError:
        config = {}

    channel = NotificationChannel(
        space_id=None, name=name, channel_type=ct, config=config, is_active=is_active
    )
    db.add(channel)
    await db.flush()
    await _link_spaces(db, channel, space_ids)
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(db, "notification_channel.create", user=user, resource_type="notification_channel",
                 resource_id=channel.id, resource_name=channel.name,
                 details={"type": ct.value, "spaces": len(space_ids)}, request=request)
    logger.info(f"Added global notification channel: {name} ({ct})")
    return RedirectResponse(url="/channels", status_code=302)


@router.post("/{channel_id}/spaces")
async def update_channel_spaces(
    channel_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
    space_ids: list[str] = Form(default=[]),
):
    _require_admin(user)
    channel = await db.get(NotificationChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail=_ERR_NOT_FOUND)
    await _link_spaces(db, channel, space_ids)
    await db.commit()
    return RedirectResponse(url="/channels", status_code=302)


@router.put("/{channel_id}")
async def update_channel(
    channel_id: uuid.UUID,
    request: Request,
    payload: NotificationChannelUpdate,
    user: CurrentUser,
    db: DB,
):
    _require_admin(user)
    channel = await db.get(NotificationChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail=_ERR_NOT_FOUND)
    if payload.name is not None:
        channel.name = payload.name
    if payload.config is not None:
        channel.config = payload.config
    if payload.is_active is not None:
        channel.is_active = payload.is_active
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(db, "notification_channel.update", user=user, resource_type="notification_channel",
                 resource_id=channel.id, resource_name=channel.name, request=request)
    return {"message": "Channel updated"}


@router.delete("/{channel_id}")
async def delete_channel(
    channel_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    _require_admin(user)
    channel = await db.get(NotificationChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail=_ERR_NOT_FOUND)
    cname = channel.name
    await db.delete(channel)
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(db, "notification_channel.delete", user=user, resource_type="notification_channel",
                 resource_id=str(channel_id), resource_name=cname, request=request)
    return {"message": "Channel deleted"}


@router.post("/{channel_id}/test")
async def test_channel(
    channel_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    _require_admin(user)
    channel = await db.get(NotificationChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail=_ERR_NOT_FOUND)
    try:
        await notification_service.send_test_notification(channel, settings.APP_URL)
        return {"message": "Test notification sent"}
    except Exception as e:
        logger.error(f"Test notification failed for channel {channel_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send test: {str(e)}")
