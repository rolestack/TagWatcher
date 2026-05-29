import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict
from typing import Optional, Any
from app.models.notification import ChannelType


class NotificationChannelCreate(BaseModel):
    name: str
    channel_type: ChannelType
    config: dict[str, Any] = {}
    is_active: bool = True


class NotificationChannelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    space_id: uuid.UUID
    name: str
    channel_type: ChannelType
    config: dict[str, Any] = {}
    is_active: bool
    created_at: datetime


class NotificationChannelUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[dict[str, Any]] = None
    is_active: Optional[bool] = None


class NotificationLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    container_id: uuid.UUID
    channel_id: uuid.UUID
    old_tag: Optional[str] = None
    old_digest: Optional[str] = None
    new_tag: Optional[str] = None
    new_digest: Optional[str] = None
    release_date: Optional[datetime] = None
    status: str
    error_message: Optional[str] = None
    sent_at: datetime
