import uuid
import logging

_ERR_CHANNEL_NOT_FOUND = "Channel not found"
from fastapi import APIRouter, Depends, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import json

from app.deps import get_current_active_user, get_db, CurrentUser, DB, get_space_access
from app.models.notification import NotificationChannel, ChannelType
from app.models.space import Space
from app.schemas.notification import NotificationChannelCreate, NotificationChannelUpdate
from app.services.notification_service import NotificationService
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/spaces/{space_id}/notifications", tags=["notifications"])
from app.templates_setup import templates

notification_service = NotificationService()


@router.get("", response_class=HTMLResponse)
async def list_channels(
    space_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    space = await get_space_access(space_id, user, db)

    result = await db.execute(
        select(NotificationChannel).where(NotificationChannel.space_id == space_id)
    )
    channels = result.scalars().all()

    return templates.TemplateResponse(
        request,
        "notifications/channels.html",
        {
"user": user,
            "space": space,
            "channels": channels,
            "channel_types": [ct.value for ct in ChannelType],
        },
    )


@router.post("", response_class=RedirectResponse)
async def add_channel(
    space_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
    name: str = Form(...),
    channel_type: str = Form(...),
    config_json: str = Form("{}"),
    is_active: bool = Form(True),
):
    space = await get_space_access(space_id, user, db)

    try:
        ct = ChannelType(channel_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid channel type: {channel_type}")

    try:
        config = json.loads(config_json)
    except json.JSONDecodeError:
        config = {}

    channel = NotificationChannel(
        space_id=space_id,
        name=name,
        channel_type=ct,
        config=config,
        is_active=is_active,
    )
    db.add(channel)
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(db, "notification_channel.create", user=user, resource_type="notification_channel",
                 resource_id=channel.id, resource_name=channel.name,
                 details={"type": ct.value, "space": space.name}, request=request)
    logger.info(f"Added notification channel: {name} ({ct}) to space {space.name}")
    return RedirectResponse(url=f"/spaces/{space_id}/notifications", status_code=302)


@router.put("/{channel_id}")
async def update_channel(
    space_id: uuid.UUID,
    channel_id: uuid.UUID,
    request: Request,
    payload: NotificationChannelUpdate,
    user: CurrentUser,
    db: DB,
):
    space = await get_space_access(space_id, user, db)

    result = await db.execute(
        select(NotificationChannel).where(
            NotificationChannel.id == channel_id,
            NotificationChannel.space_id == space_id,
        )
    )
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail=_ERR_CHANNEL_NOT_FOUND)

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
    space_id: uuid.UUID,
    channel_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    space = await get_space_access(space_id, user, db)

    result = await db.execute(
        select(NotificationChannel).where(
            NotificationChannel.id == channel_id,
            NotificationChannel.space_id == space_id,
        )
    )
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail=_ERR_CHANNEL_NOT_FOUND)

    cname = channel.name
    await db.delete(channel)
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(db, "notification_channel.delete", user=user, resource_type="notification_channel",
                 resource_id=str(channel_id), resource_name=cname, request=request)
    return {"message": "Channel deleted"}


@router.post("/{channel_id}/test")
async def test_channel(
    space_id: uuid.UUID,
    channel_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    """Send a test notification to the channel."""
    space = await get_space_access(space_id, user, db)

    result = await db.execute(
        select(NotificationChannel).where(
            NotificationChannel.id == channel_id,
            NotificationChannel.space_id == space_id,
        )
    )
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail=_ERR_CHANNEL_NOT_FOUND)

    try:
        await notification_service.send_test_notification(channel, settings.APP_URL)
        from app.services.audit_service import audit as _audit
        await _audit(db, "notification_channel.test", user=user, resource_type="notification_channel",
                     resource_id=channel.id, resource_name=channel.name, request=request)
        return {"message": "Test notification sent"}
    except Exception as e:
        logger.error(f"Test notification failed for channel {channel_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send test: {str(e)}")
