import uuid
import logging
from typing import Optional, Annotated
from fastapi import Depends, Request, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from itsdangerous import TimestampSigner, BadSignature, SignatureExpired
import json
import base64

from app.database import get_db
from app.models.user import User
from app.models.space import Space
from app.models.group import group_spaces
from app.config import settings
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

signer = TimestampSigner(settings.SECRET_KEY)


def encode_session(data: dict) -> str:
    """Encode a dict into a signed cookie value."""
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    signed = signer.sign(payload).decode()
    return signed


def decode_session(value: str, max_age: int = settings.SESSION_MAX_AGE) -> Optional[dict]:
    """Decode and verify a signed cookie value. Returns None if invalid/expired."""
    try:
        unsigned = signer.unsign(value, max_age=max_age)
        data = json.loads(base64.urlsafe_b64decode(unsigned).decode())
        return data
    except (BadSignature, SignatureExpired, Exception) as e:
        logger.debug(f"Session decode failed: {e}")
        return None


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Read JWT/session from cookie and return the current User, or redirect to login."""
    cookie_value = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not cookie_value:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/auth/login"},
        )

    max_age = await SettingsService.get_session_max_age(db)
    session_data = decode_session(cookie_value, max_age=max_age)
    if not session_data or "user_id" not in session_data:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/auth/login"},
        )

    user_id = session_data["user_id"]
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/auth/login"},
        )

    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/auth/login"},
        )

    # Slide session expiry by re-signing with a fresh timestamp
    request.state.session_refresh = encode_session({"user_id": user_id})
    request.state.session_max_age = max_age
    request.state.session_secure = request.url.scheme == "https"

    return user


async def get_current_active_user(
    user: User = Depends(get_current_user),
) -> User:
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/auth/logout?reason=deactivated"},
        )
    return user


async def require_admin(
    user: User = Depends(get_current_active_user),
) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user


async def get_space_access(
    space_id: uuid.UUID,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> Space:
    """Return the space if user's groups have access, or raise 403/404."""
    result = await db.execute(select(Space).where(Space.id == space_id))
    space = result.scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")

    # Admins and Administrator group members have access to all spaces
    if user.is_admin or any(g.name == "Administrator" for g in user.groups):
        return space

    # Check if any of the user's groups has access to this space
    user_group_ids = [g.id for g in user.groups]
    if not user_group_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to this space")

    result = await db.execute(
        select(group_spaces).where(
            group_spaces.c.group_id.in_(user_group_ids),
            group_spaces.c.space_id == space_id,
        )
    )
    access = result.first()
    if not access:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to this space")
    return space


# Type aliases for use with Annotated
CurrentUser = Annotated[User, Depends(get_current_active_user)]
AdminUser = Annotated[User, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]
