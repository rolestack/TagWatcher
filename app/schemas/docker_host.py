import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict
from typing import Optional


class DockerHostCreate(BaseModel):
    name: str
    host_url: str
    use_tls: bool = False
    tls_ca: Optional[str] = None
    tls_cert: Optional[str] = None
    tls_key: Optional[str] = None
    is_active: bool = True


class DockerHostRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    space_id: uuid.UUID
    name: str
    host_url: str
    use_tls: bool
    is_active: bool
    last_synced_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class DockerHostUpdate(BaseModel):
    name: Optional[str] = None
    host_url: Optional[str] = None
    use_tls: Optional[bool] = None
    tls_ca: Optional[str] = None
    tls_cert: Optional[str] = None
    tls_key: Optional[str] = None
    is_active: Optional[bool] = None
