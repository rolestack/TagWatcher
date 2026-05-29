import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict
from typing import Optional


class SpaceCreate(BaseModel):
    name: str
    description: Optional[str] = None


class SpaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: Optional[str] = None
    created_at: datetime


class DockerHostSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    host_url: str
    is_active: bool
    last_synced_at: Optional[datetime] = None


class SpaceDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: Optional[str] = None
    created_at: datetime
    docker_hosts: list[DockerHostSummary] = []
