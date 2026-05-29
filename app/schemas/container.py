import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict
from typing import Optional


class TrackedContainerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    docker_host_id: uuid.UUID
    container_id: str
    name: str
    image: str
    tag: str
    digest: Optional[str] = None
    latest_tag: Optional[str] = None
    latest_digest: Optional[str] = None
    release_date: Optional[datetime] = None
    has_update: bool
    status: str
    last_checked_at: Optional[datetime] = None
    created_at: datetime
