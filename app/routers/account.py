import logging
import os
from typing import Optional

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.deps import CurrentUser, DB
from app.models.user import User, AuthType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/account", tags=["account"])
from app.templates_setup import templates

COMMON_TIMEZONES = [
    "UTC",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Vancouver", "America/Toronto", "America/Sao_Paulo",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Rome",
    "Europe/Madrid", "Europe/Amsterdam", "Europe/Stockholm", "Europe/Warsaw",
    "Europe/Helsinki", "Europe/Moscow",
    "Africa/Cairo", "Africa/Johannesburg",
    "Asia/Dubai", "Asia/Kolkata", "Asia/Bangkok", "Asia/Singapore",
    "Asia/Kuala_Lumpur", "Asia/Hong_Kong", "Asia/Shanghai", "Asia/Taipei",
    "Asia/Seoul", "Asia/Tokyo",
    "Australia/Perth", "Australia/Adelaide", "Australia/Sydney", "Australia/Melbourne",
    "Pacific/Auckland", "Pacific/Honolulu",
]

_SYSTEM_TZ = os.environ.get("TZ", "UTC")


@router.get("", response_class=HTMLResponse)
async def account_page(request: Request, user: CurrentUser):
    saved = request.query_params.get("saved") == "1"
    error = request.query_params.get("error")
    return templates.TemplateResponse(request, "account.html", {
        "user": user,
        "saved": saved,
        "error": error,
        "timezones": COMMON_TIMEZONES,
        "system_tz": _SYSTEM_TZ,
    })


@router.post("/profile")
async def update_profile(
    request: Request,
    user: CurrentUser,
    db: DB,
    name: str = Form(...),
    email: Optional[str] = Form(None),
):
    name = name.strip()
    email = email.strip() if email else None

    if not name:
        return RedirectResponse("/account?error=Display+name+cannot+be+empty.", status_code=303)

    result = await db.execute(select(User).where(User.id == user.id))
    db_user = result.scalar_one()
    db_user.name = name
    db_user.email = email or None
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(None, "account.update_profile", user=user, resource_type="user",
                 resource_id=user.id, resource_name=name,
                 details={"email": email}, request=request)
    logger.info(f"Profile updated for user {user.display_name}")
    return RedirectResponse("/account?saved=1", status_code=303)


@router.post("/timezone")
async def update_timezone(
    request: Request,
    user: CurrentUser,
    db: DB,
    timezone: str = Form(...),
):
    result = await db.execute(select(User).where(User.id == user.id))
    db_user = result.scalar_one()
    db_user.timezone = timezone if timezone != "system" else None
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(None, "account.update_timezone", user=user, resource_type="user",
                 resource_id=user.id, resource_name=user.display_name,
                 details={"timezone": timezone}, request=request)
    logger.info(f"Timezone set to '{timezone}' for user {user.display_name}")
    return RedirectResponse("/account?saved=1", status_code=303)


@router.post("/password")
async def change_password(
    request: Request,
    user: CurrentUser,
    db: DB,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    from app.routers.auth import verify_password, hash_password

    if user.auth_type != AuthType.local:
        raise HTTPException(status_code=403, detail="Password change not available for SSO accounts.")

    if not user.password_hash or not verify_password(current_password, user.password_hash):
        return RedirectResponse("/account?error=Current+password+is+incorrect.", status_code=303)

    if new_password != confirm_password:
        return RedirectResponse("/account?error=New+passwords+do+not+match.", status_code=303)

    if len(new_password) < 8:
        return RedirectResponse("/account?error=Password+must+be+at+least+8+characters.", status_code=303)

    result = await db.execute(select(User).where(User.id == user.id))
    db_user = result.scalar_one()
    db_user.password_hash = hash_password(new_password)
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(None, "account.change_password", user=user, resource_type="user",
                 resource_id=user.id, resource_name=user.display_name, request=request)
    logger.info(f"Password changed for user {user.display_name}")
    return RedirectResponse("/account?saved=1", status_code=303)
