import os
import uuid
import logging
import shutil
from typing import Optional

_ERR_USER_NOT_FOUND = "User not found"
_ERR_GROUP_NOT_FOUND = "Group not found"
_AUDIT_SETTINGS_UPDATE = "settings.update"

from fastapi import APIRouter, Depends, Request, HTTPException, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func

from app.deps import require_admin, get_db, AdminUser, DB
from app.security import validate_oidc_url
from app.models.user import User, user_groups, AuthType
from app.models.group import Group, group_spaces
from app.models.space import Space
from app.schemas.user import UserUpdate
from app.services.settings_service import (
    SettingsService,
    OIDC_ENABLED_KEY,
    OIDC_PROVIDER_URL_KEY,
    OIDC_CLIENT_ID_KEY,
    OIDC_CLIENT_SECRET_KEY,
    OIDC_SCOPES_KEY,
    LOCAL_LOGIN_ENABLED_KEY,
    BRAND_SSO_BUTTON_LABEL_KEY,
    BRAND_LOGIN_BG_URL_KEY,
    BRAND_FAVICON_URL_KEY,
    SESSION_MAX_AGE_KEY,
    CUSTOM_HEAD_HTML_KEY,
    FOOTER_TEXT_KEY,
    CUSTOM_FOOTER_HTML_KEY,
    NOTIFICATION_RETENTION_DAYS_KEY,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
from app.templates_setup import templates


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.get("/users", response_class=HTMLResponse)
async def list_users(request: Request, user: AdminUser, db: DB):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()

    groups_result = await db.execute(select(Group).order_by(Group.name))
    groups = groups_result.scalars().all()

    return templates.TemplateResponse(
        request,
        "admin/users.html",
        {"user": user, "users": users, "groups": groups},
    )


@router.post("/users")
async def create_user(
    request: Request,
    admin: AdminUser,
    db: DB,
    username: str = Form(...),
    name: str = Form(...),
    email: Optional[str] = Form(None),
    password: str = Form(...),
):
    from app.routers.auth import hash_password

    username = username.strip()
    name = name.strip()
    email = email.strip() if email else None

    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters.")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    existing = await db.scalar(select(func.count()).where(User.username == username))
    if existing:
        raise HTTPException(status_code=400, detail="Username already taken.")

    new_user = User(
        username=username,
        password_hash=hash_password(password),
        name=name or username,
        email=email or None,
        auth_type=AuthType.local,
        is_active=True,
        is_admin=False,
    )
    db.add(new_user)
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(db, "user.create", user=admin, resource_type="user",
                 resource_id=new_user.id, resource_name=new_user.display_name,
                 details={"username": username, "email": email}, request=request)
    logger.info(f"Local user '{username}' created by {admin.display_name}")
    return RedirectResponse(url="/admin/users", status_code=303)


@router.put("/users/{user_id}")
async def update_user(user_id: uuid.UUID, request: Request, payload: UserUpdate, admin: AdminUser, db: DB):
    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail=_ERR_USER_NOT_FOUND)

    # Prevent self-deactivation
    if payload.is_active is False and target.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account.")

    # Prevent self-demotion for the last admin
    if payload.is_admin is False and target.id == admin.id:
        admin_count = await db.scalar(
            select(func.count()).where(User.is_admin == True, User.is_active == True)  # noqa: E712
        )
        if admin_count and admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot remove admin from the last active admin.")

    changes = {}
    if payload.is_active is not None:
        changes["is_active"] = payload.is_active
        target.is_active = payload.is_active
    if payload.is_admin is not None:
        changes["is_admin"] = payload.is_admin
        target.is_admin = payload.is_admin
    if payload.name is not None:
        changes["name"] = payload.name
        target.name = payload.name

    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(db, "user.update", user=admin, resource_type="user",
                 resource_id=target.id, resource_name=target.display_name, details=changes, request=request)
    return {"message": "User updated"}


@router.delete("/users/{user_id}")
async def delete_user(user_id: uuid.UUID, request: Request, admin: AdminUser, db: DB):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account.")

    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail=_ERR_USER_NOT_FOUND)

    # Prevent deleting the last active admin
    if target.is_admin and target.is_active:
        admin_count = await db.scalar(
            select(func.count()).where(User.is_admin == True, User.is_active == True)  # noqa: E712
        )
        if admin_count and admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last active admin.")

    name_snap = target.display_name
    email_snap = target.display_email
    await db.delete(target)
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(db, "user.delete", user=admin, resource_type="user",
                 resource_id=str(user_id), resource_name=name_snap,
                 details={"email": email_snap}, request=request)
    await db.commit()
    logger.info(f"User {name_snap} deleted by {admin.display_name}")
    return {"message": "User deleted"}


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

@router.get("/groups", response_class=HTMLResponse)
async def list_groups(request: Request, user: AdminUser, db: DB):
    result = await db.execute(select(Group).order_by(Group.name))
    groups = result.scalars().all()

    spaces_result = await db.execute(select(Space).order_by(Space.name))
    spaces = spaces_result.scalars().all()

    users_result = await db.execute(select(User).order_by(User.name))
    users = users_result.scalars().all()

    return templates.TemplateResponse(
        request,
        "admin/users.html",
        {
"user": user,
            "groups": groups,
            "spaces": spaces,
            "all_users": users,
            "view": "groups",
        },
    )


@router.post("/groups")
async def create_group(request: Request, admin: AdminUser, db: DB, name: str = Form(...), description: str = Form("")):
    existing = await db.scalar(select(func.count()).where(Group.name == name))
    if existing:
        raise HTTPException(status_code=400, detail="Group name already exists")

    group = Group(name=name, description=description or None)
    db.add(group)
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(db, "group.create", user=admin, resource_type="group",
                 resource_id=group.id, resource_name=group.name, request=request)
    logger.info(f"Created group: {name}")
    return {"message": "Group created", "id": str(group.id)}


@router.delete("/groups/{group_id}")
async def delete_group(group_id: uuid.UUID, request: Request, admin: AdminUser, db: DB):
    result = await db.execute(select(Group).where(Group.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail=_ERR_GROUP_NOT_FOUND)
    gname = group.name
    await db.delete(group)
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(db, "group.delete", user=admin, resource_type="group",
                 resource_id=str(group_id), resource_name=gname, request=request)
    await db.commit()
    logger.info(f"Deleted group: {gname}")
    return {"message": "Group deleted"}


@router.post("/groups/{group_id}/users")
async def add_user_to_group(
    group_id: uuid.UUID, request: Request, admin: AdminUser, db: DB, user_id: uuid.UUID = Form(...)
):
    result = await db.execute(select(Group).where(Group.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail=_ERR_GROUP_NOT_FOUND)

    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail=_ERR_USER_NOT_FOUND)

    if target_user not in group.users:
        group.users.append(target_user)
        await db.commit()
        from app.services.audit_service import audit as _audit
        await _audit(db, "group.add_user", user=admin, resource_type="group",
                     resource_id=group.id, resource_name=group.name,
                     details={"user": target_user.display_email}, request=request)
        logger.info(f"Added {target_user.display_name} to group {group.name}")
    return {"message": "User added to group"}


@router.delete("/groups/{group_id}/users/{user_id}")
async def remove_user_from_group(group_id: uuid.UUID, user_id: uuid.UUID, request: Request, admin: AdminUser, db: DB):
    result = await db.execute(select(Group).where(Group.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail=_ERR_GROUP_NOT_FOUND)

    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail=_ERR_USER_NOT_FOUND)

    if target_user in group.users:
        group.users.remove(target_user)
        await db.commit()
        from app.services.audit_service import audit as _audit
        await _audit(db, "group.remove_user", user=admin, resource_type="group",
                     resource_id=group.id, resource_name=group.name,
                     details={"user": target_user.display_email}, request=request)
    return {"message": "User removed from group"}


@router.post("/groups/{group_id}/spaces")
async def grant_space_access(
    group_id: uuid.UUID, request: Request, admin: AdminUser, db: DB, space_id: uuid.UUID = Form(...)
):
    result = await db.execute(select(Group).where(Group.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail=_ERR_GROUP_NOT_FOUND)

    result = await db.execute(select(Space).where(Space.id == space_id))
    space = result.scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=404, detail="Space not found")

    if space not in group.spaces:
        group.spaces.append(space)
        await db.commit()
        from app.services.audit_service import audit as _audit
        await _audit(db, "group.add_space", user=admin, resource_type="group",
                     resource_id=group.id, resource_name=group.name,
                     details={"space": space.name}, request=request)
    return {"message": "Space access granted"}


@router.delete("/groups/{group_id}/spaces/{space_id}")
async def revoke_space_access(group_id: uuid.UUID, space_id: uuid.UUID, request: Request, admin: AdminUser, db: DB):
    result = await db.execute(select(Group).where(Group.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail=_ERR_GROUP_NOT_FOUND)

    result = await db.execute(select(Space).where(Space.id == space_id))
    space = result.scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=404, detail="Space not found")

    if space in group.spaces:
        group.spaces.remove(space)
        await db.commit()
        from app.services.audit_service import audit as _audit
        await _audit(db, "group.remove_space", user=admin, resource_type="group",
                     resource_id=group.id, resource_name=group.name,
                     details={"space": space.name}, request=request)
    return {"message": "Space access revoked"}


# ---------------------------------------------------------------------------
# System Settings (OIDC configuration)
# ---------------------------------------------------------------------------

@router.get("/settings")
async def settings_redirect():
    return RedirectResponse(url="/admin/settings/auth", status_code=302)


@router.get("/settings/auth", response_class=HTMLResponse)
async def settings_auth_page(request: Request, user: AdminUser, db: DB):
    local_login_enabled = await SettingsService.is_local_login_enabled(db)
    session_max_age = await SettingsService.get_session_max_age(db)
    oidc_cfg = await SettingsService.get_oidc_config(db)
    sso_button_label = await SettingsService.get(db, BRAND_SSO_BUTTON_LABEL_KEY) or ""
    saved = request.query_params.get("saved") == "1"
    oidc_display = dict(oidc_cfg) if oidc_cfg else {}
    has_client_secret = bool(oidc_display.pop("client_secret", None))
    return templates.TemplateResponse(request, "admin/settings_auth.html", {
        "user": user,
        "local_login_enabled": local_login_enabled,
        "session_max_age": session_max_age,
        "oidc": oidc_display,
        "has_client_secret": has_client_secret,
        "sso_button_label": sso_button_label,
        "saved": saved,
    })


@router.get("/settings/sso")
async def settings_sso_redirect():
    return RedirectResponse(url="/admin/settings/auth", status_code=301)


def _normalize_asset_url(url: str | None) -> str:
    """Migrate legacy /static/uploads/ paths to /assets/."""
    if url and url.startswith("/static/uploads/"):
        return "/assets/" + url[len("/static/uploads/"):]
    return url or ""


@router.get("/settings/notifications", response_class=HTMLResponse)
async def settings_notifications_page(request: Request, user: AdminUser, db: DB):
    retention_days = await SettingsService.get_notification_retention_days(db)
    saved = request.query_params.get("saved") == "1"
    return templates.TemplateResponse(request, "admin/settings_notifications.html", {
        "user": user,
        "retention_days": retention_days,
        "saved": saved,
    })


@router.post("/settings/notifications")
async def save_notification_settings(
    request: Request,
    admin: AdminUser,
    db: DB,
    retention_days: str = Form("0"),
):
    try:
        days = max(0, int(retention_days))
    except (ValueError, TypeError):
        days = 0
    await SettingsService.set(db, NOTIFICATION_RETENTION_DAYS_KEY, str(days))
    return RedirectResponse(url="/admin/settings/notifications?saved=1", status_code=303)


@router.get("/settings/brand", response_class=HTMLResponse)
async def settings_brand_page(request: Request, user: AdminUser, db: DB):
    brand = {
        "login_bg_url": _normalize_asset_url(await SettingsService.get(db, BRAND_LOGIN_BG_URL_KEY)),
        "favicon_url": _normalize_asset_url(await SettingsService.get(db, BRAND_FAVICON_URL_KEY)),
        "footer_text": await SettingsService.get(db, FOOTER_TEXT_KEY) or "",
    }
    saved = request.query_params.get("saved") == "1"
    return templates.TemplateResponse(request, "admin/settings_brand.html", {
        "user": user,
        "brand": brand,
        "saved": saved,
    })


@router.post("/settings/oidc")
async def save_oidc_settings(
    request: Request,
    admin: AdminUser,
    db: DB,
    oidc_enabled: Optional[str] = Form(None),
    oidc_provider_url: str = Form(""),
    oidc_client_id: str = Form(""),
    oidc_client_secret: str = Form(""),
    oidc_scopes: str = Form("openid email profile"),
    sso_button_label: Optional[str] = Form(None),
):
    updates: dict = {
        OIDC_ENABLED_KEY: "true" if oidc_enabled == "on" else "false",
        OIDC_PROVIDER_URL_KEY: oidc_provider_url.strip() or None,
        OIDC_CLIENT_ID_KEY: oidc_client_id.strip() or None,
        OIDC_SCOPES_KEY: oidc_scopes.strip() or "openid email profile",
        BRAND_SSO_BUTTON_LABEL_KEY: sso_button_label.strip() if sso_button_label else None,
    }

    if oidc_client_secret.strip():
        updates[OIDC_CLIENT_SECRET_KEY] = oidc_client_secret.strip()

    await SettingsService.set_many(db, updates)
    from app.services.audit_service import audit as _audit
    await _audit(db, _AUDIT_SETTINGS_UPDATE, user=admin, resource_type="settings", resource_name="oidc",
                 details={"enabled": oidc_enabled == "on", "provider_url": oidc_provider_url.strip() or None},
                 request=request)
    await db.commit()
    logger.info(f"OIDC settings updated by {admin.display_name}")
    return RedirectResponse(url="/admin/settings/auth?saved=1", status_code=303)


@router.post("/settings/auth")
async def save_auth_settings(
    request: Request,
    admin: AdminUser,
    db: DB,
    local_login_enabled: Optional[str] = Form(None),
    session_max_age: int = Form(28800),
):
    await SettingsService.set_many(db, {
        LOCAL_LOGIN_ENABLED_KEY: "true" if local_login_enabled == "on" else "false",
        SESSION_MAX_AGE_KEY: str(session_max_age),
    })
    from app.services.audit_service import audit as _audit
    await _audit(db, _AUDIT_SETTINGS_UPDATE, user=admin, resource_type="settings", resource_name="auth",
                 details={"local_login_enabled": local_login_enabled == "on", "session_max_age": session_max_age},
                 request=request)
    logger.info(f"Auth settings updated by {admin.display_name}")
    return RedirectResponse(url="/admin/settings/auth?saved=1", status_code=303)


@router.post("/settings/brand")
async def save_brand_settings(
    request: Request,
    admin: AdminUser,
    db: DB,
    login_bg_url: Optional[str] = Form(None),
    favicon_url: Optional[str] = Form(None),
    footer_text: Optional[str] = Form(None),
):
    await SettingsService.set_many(db, {
        BRAND_LOGIN_BG_URL_KEY: login_bg_url.strip() if login_bg_url else None,
        BRAND_FAVICON_URL_KEY: favicon_url.strip() if favicon_url else None,
        FOOTER_TEXT_KEY: footer_text.strip() if footer_text else None,
    })
    from app.templates_setup import site_customization
    site_customization["footer_text"] = footer_text.strip() if footer_text else ""
    from app.services.audit_service import audit as _audit
    await _audit(db, _AUDIT_SETTINGS_UPDATE, user=admin, resource_type="settings", resource_name="brand",
                 details={"login_bg_url": login_bg_url, "favicon_url": favicon_url, "footer_text": footer_text},
                 request=request)
    logger.info(f"Brand settings updated by {admin.display_name}")
    return RedirectResponse(url="/admin/settings/brand?saved=1", status_code=303)


@router.get("/settings/injection", response_class=HTMLResponse)
async def settings_injection_page(request: Request, user: AdminUser, db: DB):
    data = {
        "custom_head_html": await SettingsService.get(db, CUSTOM_HEAD_HTML_KEY) or "",
        "custom_footer_html": await SettingsService.get(db, CUSTOM_FOOTER_HTML_KEY) or "",
    }
    saved = request.query_params.get("saved") == "1"
    return templates.TemplateResponse(request, "admin/settings_injection.html", {
        "user": user,
        "data": data,
        "saved": saved,
    })


@router.post("/settings/injection")
async def save_injection_settings(
    request: Request,
    admin: AdminUser,
    db: DB,
    custom_head_html: Optional[str] = Form(None),
    custom_footer_html: Optional[str] = Form(None),
):
    await SettingsService.set_many(db, {
        CUSTOM_HEAD_HTML_KEY: custom_head_html.strip() if custom_head_html else None,
        CUSTOM_FOOTER_HTML_KEY: custom_footer_html.strip() if custom_footer_html else None,
    })
    from app.templates_setup import site_customization
    site_customization["custom_head_html"] = custom_head_html.strip() if custom_head_html else ""
    site_customization["custom_footer_html"] = custom_footer_html.strip() if custom_footer_html else ""
    from app.services.audit_service import audit as _audit
    await _audit(db, _AUDIT_SETTINGS_UPDATE, user=admin, resource_type="settings", resource_name="code_injection",
                 details={"header_len": len(custom_head_html or ""), "footer_len": len(custom_footer_html or "")},
                 request=request)
    logger.info(f"Code injection settings updated by {admin.display_name}")
    return RedirectResponse(url="/admin/settings/injection?saved=1", status_code=303)



_UPLOAD_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_UPLOAD_ALLOWED_EXTS = {".ico", ".png", ".jpg", ".jpeg", ".gif", ".webp"}


@router.post("/settings/upload")
async def upload_brand_asset(admin: AdminUser, file: UploadFile = File(...)):
    """Upload a brand asset (favicon or background image) and return its URL."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _UPLOAD_ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail=f"File type '{ext}' not allowed.")

    # Read with size cap — reject files exceeding 5 MB
    content = await file.read(_UPLOAD_MAX_BYTES + 1)
    if len(content) > _UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=400, detail="File must be 5 MB or smaller.")

    dest_dir = "app/static/uploads"
    os.makedirs(dest_dir, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    dest_path = os.path.join(dest_dir, filename)
    with open(dest_path, "wb") as f:
        f.write(content)
    logger.info(f"Brand asset uploaded by {admin.display_name}: {filename}")
    return {"url": f"/assets/{filename}"}


@router.post("/settings/oidc/test")
async def test_oidc_connection(admin: AdminUser, db: DB):
    """Quick test: fetch OIDC discovery document and return provider metadata."""
    import httpx

    oidc_cfg = await SettingsService.get_oidc_config(db)
    if not oidc_cfg:
        raise HTTPException(
            status_code=400,
            detail="OIDC not fully configured. Save provider URL, client ID, and client secret first.",
        )

    provider_url = oidc_cfg["provider_url"]
    url_error = validate_oidc_url(provider_url)
    if url_error:
        raise HTTPException(status_code=400, detail=f"OIDC provider URL invalid: {url_error}")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{provider_url}/.well-known/openid-configuration"
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "ok": True,
                "issuer": data.get("issuer"),
                "authorization_endpoint": data.get("authorization_endpoint"),
                "token_endpoint": data.get("token_endpoint"),
            }
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="OIDC connection test failed.")


# ---------------------------------------------------------------------------
# Audit Log
# ---------------------------------------------------------------------------

@router.get("/audit/users")
async def audit_user_suggest(user: AdminUser, db: DB, q: str = ""):
    """Return distinct user email+name pairs from audit_logs matching query."""
    from app.models.audit_log import AuditLog
    from sqlalchemy import or_
    if not q.strip():
        return []
    term = f"%{q.strip()}%"
    result = await db.execute(
        select(AuditLog.user_email, AuditLog.user_name)
        .distinct()
        .where(
            AuditLog.user_email.isnot(None),
            or_(
                AuditLog.user_email.ilike(term),
                AuditLog.user_name.ilike(term),
            ),
        )
        .limit(10)
    )
    return [{"email": r.user_email, "name": r.user_name} for r in result.all()]

@router.get("/audit", response_class=HTMLResponse)
async def audit_log_page(
    request: Request,
    user: AdminUser,
    db: DB,
    page: int = 1,
    per_page: int = 50,
    user_search: str = "",
    resource_type: str = "",
    action: str = "",
    date_from: str = "",
    date_to: str = "",
):
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import and_, or_
    from app.models.audit_log import AuditLog
    from app.services.audit_service import KNOWN_ACTIONS

    page = max(1, page)
    per_page = min(max(10, per_page), 200)

    # Default date range: past 7 days
    today = datetime.now(timezone.utc).date()
    if not date_from:
        date_from = (today - timedelta(days=7)).isoformat()
    if not date_to:
        date_to = today.isoformat()

    filters = []
    if user_search.strip():
        term = f"%{user_search.strip()}%"
        filters.append(or_(
            AuditLog.user_email.ilike(term),
            AuditLog.user_name.ilike(term),
        ))
    if resource_type:
        filters.append(AuditLog.resource_type == resource_type)
    if action:
        filters.append(AuditLog.action == action)
    try:
        dt_from = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        filters.append(AuditLog.created_at >= dt_from)
    except ValueError:
        pass
    try:
        dt_to = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        filters.append(AuditLog.created_at < dt_to)
    except ValueError:
        pass

    base_q = select(AuditLog).where(and_(*filters)) if filters else select(AuditLog)
    count_q = select(func.count()).select_from(base_q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    offset = (page - 1) * per_page
    entries_q = base_q.order_by(AuditLog.created_at.desc()).offset(offset).limit(per_page)
    entries = (await db.execute(entries_q)).scalars().all()

    # Distinct resource types actually present in the DB
    rt_result = await db.execute(
        select(AuditLog.resource_type).distinct().where(AuditLog.resource_type.isnot(None))
    )
    resource_types = sorted([r[0] for r in rt_result.all() if r[0]])

    total_pages = max(1, (total + per_page - 1) // per_page)

    return templates.TemplateResponse(request, "admin/audit.html", {
        "user": user,
        "entries": entries,
        "known_actions": KNOWN_ACTIONS,
        "resource_types": resource_types,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "filters": {
            "user_search": user_search,
            "resource_type": resource_type,
            "action": action,
            "date_from": date_from,
            "date_to": date_to,
        },
    })
