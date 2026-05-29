import uuid
from datetime import datetime
from pydantic import BaseModel, EmailStr, ConfigDict
from typing import Optional


class GroupRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    oidc_sub: str
    email: str
    name: str
    picture: Optional[str] = None
    is_active: bool
    is_admin: bool
    created_at: datetime
    updated_at: datetime
    groups: list[GroupRef] = []


class UserUpdate(BaseModel):
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None
    name: Optional[str] = None
