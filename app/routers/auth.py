import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

_SETUP_TEMPLATE = "setup.html"
_SETUP_URL = "/auth/setup"

from fastapi import APIRouter, Depends, Request, HTTPException, status, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, insert
import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
import bcrypt
import hashlib

from app.config import settings
from app import database
from app.security import check_login_rate_limit
from app.config_file import save_database_url, is_db_configured
from app.database import get_db
from app.models.user import User, AuthType, user_groups as user_groups_table
from app.models.group import Group
from app.deps import encode_session, DB
from app.services.settings_service import (
    SettingsService,
    BRAND_SSO_BUTTON_LABEL_KEY,
    BRAND_LOGIN_BG_URL_KEY,
    SESSION_MAX_AGE_KEY,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])
from app.templates_setup import templates


def _prehash(plain: str) -> bytes:
    return hashlib.sha256(plain.encode()).hexdigest().encode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(_prehash(plain), hashed.encode())


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_prehash(plain), bcrypt.gensalt()).decode()


def _get_redirect_uri(request: Request) -> str:
    return f"{settings.APP_URL.rstrip('/')}/auth/callback"


# ---------------------------------------------------------------------------
# Setup — first-run wizard
#   Step 1 (GET /auth/setup, no DB):      Database configuration
#   Step 2 (GET /auth/setup, DB ready):   Create local admin account
# ---------------------------------------------------------------------------

def _setup_step(request: Request) -> int:
    """Return which setup step we're on."""
    if not database.is_initialized():
        return 1
    return 2


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    step = _setup_step(request)

    # Already fully set up — go to dashboard
    if step == 2 and database.is_initialized():
        try:
            maker = database.get_session_maker()
        except RuntimeError:
            # Another worker configured DB but this worker hasn't loaded it yet
            from app.config_file import get_database_url
            url = get_database_url()
            await database.initialize(url)
            maker = database.get_session_maker()

        async with maker() as db:
            count = await db.scalar(select(func.count()).where(User.is_admin == True))  # noqa: E712
        if count and count > 0:
            return RedirectResponse(url="/", status_code=302)

    return templates.TemplateResponse(
        request,
        _SETUP_TEMPLATE, {"step": step, "error": None}
    )


# ── Step 1: Database configuration ─────────────────────────────────────────

@router.post("/setup/database/test")
async def test_db_connection(
    db_host: str = Form(...),
    db_port: int = Form(5432),
    db_name: str = Form(...),
    db_user: str = Form(...),
    db_password: str = Form(...),
    db_ssl: str = Form("prefer"),
):
    """AJAX endpoint — test DB credentials without saving them."""
    url = (
        f"postgresql+asyncpg://{db_user}:{db_password}"
        f"@{db_host}:{db_port}/{db_name}"
        f"?ssl={db_ssl}"
    )
    try:
        await database.test_connection(url)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/setup/database", response_class=HTMLResponse)
async def setup_database(
    request: Request,
    db_host: str = Form(...),
    db_port: int = Form(5432),
    db_name: str = Form(...),
    db_user: str = Form(...),
    db_password: str = Form(...),
    db_ssl: str = Form("prefer"),
):
    """Save DB config, initialize engine, create tables, then proceed to step 2."""
    def err(msg: str):
        return templates.TemplateResponse(
            request,
            _SETUP_TEMPLATE, {"step": 1, "error": msg}, status_code=400
        )

    url = (
        f"postgresql+asyncpg://{db_user}:{db_password}"
        f"@{db_host}:{db_port}/{db_name}"
        f"?ssl={db_ssl}"
    )

    try:
        await database.test_connection(url)
    except Exception as exc:
        return err(f"Cannot connect to database: {exc}")

    try:
        save_database_url(url)
        await database.initialize(url, debug=settings.DEBUG)
        await database.init_tables()
    except Exception as exc:
        return err(f"Database initialization failed: {exc}")

    logger.info(f"Database configured: {db_host}:{db_port}/{db_name}")
    return RedirectResponse(url=_SETUP_URL, status_code=302)


# ── Step 2: Create local admin account ─────────────────────────────────────

@router.post("/setup/admin", response_class=HTMLResponse)
async def setup_admin(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(...),
    email: Optional[str] = Form(None),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    """Create the initial local admin account."""
    if not database.is_initialized():
        return RedirectResponse(url=_SETUP_URL, status_code=302)

    # Multi-worker: ensure this worker's engine is initialized
    try:
        maker = database.get_session_maker()
    except RuntimeError:
        # Another worker configured DB but this worker hasn't loaded it yet
        from app.config_file import get_database_url
        url = get_database_url()
        await database.initialize(url)
        maker = database.get_session_maker()

    def err(msg: str):
        return templates.TemplateResponse(
            request,
            _SETUP_TEMPLATE, {"step": 2, "error": msg}, status_code=400
        )

    async with maker() as db:
        count = await db.scalar(select(func.count()).where(User.is_admin == True))  # noqa: E712
    if count and count > 0:
        return RedirectResponse(url="/", status_code=302)

    username = username.strip()
    display_name = display_name.strip()
    email = email.strip() if email else None

    if len(username) < 3:
        return err("Username must be at least 3 characters.")
    if len(password) < 8:
        return err("Password must be at least 8 characters.")
    if password != confirm_password:
        return err("Passwords do not match.")

    async with maker() as db:
        existing = await db.scalar(select(func.count()).where(User.username == username))
        if existing:
            return err("Username is already taken.")

        admin = User(
            username=username,
            password_hash=hash_password(password),
            auth_type=AuthType.local,
            email=email or None,
            name=display_name,
            is_active=True,
            is_admin=True,
        )
        db.add(admin)
        await db.commit()

    logger.info(f"Initial admin account created: {username}")
    return RedirectResponse(url="/auth/login?setup=1", status_code=302)


# ---------------------------------------------------------------------------
# Unified login page (local + optional SSO)
# ---------------------------------------------------------------------------

async def _login_context(db, error=None, setup=False):
    """Build template context for the login page."""
    oidc_available = await SettingsService.is_oidc_configured(db)
    local_enabled = await SettingsService.is_local_login_enabled(db)
    sso_label = await SettingsService.get(db, BRAND_SSO_BUTTON_LABEL_KEY) or "Sign in with SSO"
    raw_bg = await SettingsService.get(db, BRAND_LOGIN_BG_URL_KEY) or ""
    login_bg_url = "/assets/" + raw_bg[len("/static/uploads/"):] if raw_bg.startswith("/static/uploads/") else raw_bg
    return {
        "oidc_available": oidc_available,
        "local_enabled": local_enabled,
        "error": error,
        "setup_complete": setup,
        "sso_label": sso_label,
        "login_bg_url": login_bg_url,
    }


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    error: Optional[str] = None,
    setup: Optional[str] = None,
):
    if not database.is_initialized():
        return RedirectResponse(url=_SETUP_URL, status_code=302)

    maker = database.get_session_maker()
    async with maker() as db:
        count = await db.scalar(select(func.count()).where(User.is_admin == True))  # noqa: E712
        if not count:
            return RedirectResponse(url=_SETUP_URL, status_code=302)
        ctx = await _login_context(db, error=error, setup=setup == "1")

    if not ctx["local_enabled"] and ctx["oidc_available"]:
        return RedirectResponse(url="/auth/oidc", status_code=302)

    return templates.TemplateResponse(request, "login.html", ctx)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if not database.is_initialized():
        return RedirectResponse(url=_SETUP_URL, status_code=302)

    client_ip = request.client.host if request.client else "unknown"
    if not check_login_rate_limit(client_ip):
        logger.warning(f"Login rate limit exceeded for IP {client_ip}")
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Please wait a moment and try again.",
        )

    maker = database.get_session_maker()
    async with maker() as db:
        async def err(msg: str):
            ctx = await _login_context(db, error=msg)
            return templates.TemplateResponse(request, "login.html", ctx, status_code=401)

        result = await db.execute(
            select(User).where(User.username == username, User.auth_type == AuthType.local)
        )
        user = result.scalar_one_or_none()

        if not user or not user.password_hash or not verify_password(password, user.password_hash):
            if database.is_initialized():
                try:
                    from app.services.audit_service import audit as _audit
                    await _audit(db, "user.login_failed", resource_type="user",
                                 resource_name=username,
                                 details={"reason": "invalid credentials"}, request=request)
                except Exception:
                    pass
            return await err("Invalid username or password.")

        if not user.is_active:
            return await err("This account has been deactivated.")

        max_age = await SettingsService.get_session_max_age(db)
        from app.services.audit_service import audit as _audit
        await _audit(db, "user.login", user=user, resource_type="user",
                     resource_id=user.id, resource_name=user.display_name, request=request)

    session_value = encode_session({"user_id": str(user.id)})
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=session_value,
        httponly=True,
        samesite="lax",
        max_age=max_age,
        secure=request.url.scheme == "https",
    )
    logger.info(f"Local login: {username}")
    return response


# Keep /auth/local-login as alias for backward compatibility
@router.get("/local-login", response_class=HTMLResponse)
async def local_login_redirect(request: Request):
    qs = request.url.query
    return RedirectResponse(url=f"/auth/login{'?' + qs if qs else ''}", status_code=302)


# ---------------------------------------------------------------------------
# OIDC login
# ---------------------------------------------------------------------------


@router.get("/oidc")
async def oidc_redirect(request: Request, db: DB):
    """Redirect to the OIDC provider."""
    oidc_cfg = await SettingsService.get_oidc_config(db)
    if not oidc_cfg or not oidc_cfg.get("enabled"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC is not configured. Please ask your administrator.",
        )

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    redirect_uri = _get_redirect_uri(request)

    # Discovery endpoint is public — use plain httpx, not the OAuth2 client
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            disc = await http.get(
                f"{oidc_cfg['provider_url']}/.well-known/openid-configuration"
            )
            disc.raise_for_status()
            authorization_endpoint = disc.json()["authorization_endpoint"]
    except Exception as exc:
        logger.error(f"OIDC discovery failed: {exc}")
        return RedirectResponse(
            url="/auth/login?error=SSO+provider+unavailable.+Check+OIDC+settings.",
            status_code=302,
        )

    async with AsyncOAuth2Client(
        client_id=oidc_cfg["client_id"],
        client_secret=oidc_cfg["client_secret"],
        scope=oidc_cfg["scopes"],
        redirect_uri=redirect_uri,
        code_challenge_method="S256",
    ) as client:
        auth_url, _ = client.create_authorization_url(
            authorization_endpoint,
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
        )

    secure = request.url.scheme == "https"
    response = RedirectResponse(url=auth_url, status_code=302)
    for key, val in [("oidc_state", state), ("oidc_nonce", nonce), ("oidc_cv", code_verifier)]:
        response.set_cookie(key=key, value=val, httponly=True, samesite="lax",
                            max_age=600, secure=secure)
    return response


async def _exchange_oidc_code_for_userinfo(
    oidc_cfg: dict, code: str, redirect_uri: str,
    code_verifier: str, stored_state: str,
) -> dict:
    """Exchange an authorization code for user info claims. Raises HTTPException on failure."""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            disc = await http.get(f"{oidc_cfg['provider_url']}/.well-known/openid-configuration")
            disc.raise_for_status()
            disc_data = disc.json()
            token_endpoint = disc_data["token_endpoint"]
            userinfo_endpoint = disc_data["userinfo_endpoint"]
    except Exception as exc:
        logger.error(f"OIDC discovery failed: {exc}")
        raise HTTPException(status_code=503, detail="OIDC provider unavailable.")

    async with AsyncOAuth2Client(
        client_id=oidc_cfg["client_id"],
        client_secret=oidc_cfg["client_secret"],
        scope=oidc_cfg["scopes"],
        redirect_uri=redirect_uri,
        state=stored_state,
        code_challenge_method="S256",
    ) as client:
        try:
            await client.fetch_token(
                token_endpoint, code=code,
                redirect_uri=redirect_uri, code_verifier=code_verifier,
            )
        except Exception as exc:
            logger.error(f"Token exchange failed: {exc}")
            raise HTTPException(status_code=400, detail="Token exchange failed.")

        try:
            userinfo_resp = await client.get(userinfo_endpoint)
            userinfo_resp.raise_for_status()
            return userinfo_resp.json()
        except Exception as exc:
            logger.error(f"Userinfo fetch failed: {exc}")
            raise HTTPException(status_code=400, detail="Failed to fetch user info.")


async def _sync_oidc_user(
    db: AsyncSession, sub: str, email: Optional[str], name: Optional[str], picture: Optional[str]
) -> User:
    """Create or update the local User record for an OIDC sub claim."""
    result = await db.execute(select(User).where(User.oidc_sub == sub))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(
            oidc_sub=sub, email=email, name=name, picture=picture,
            auth_type=AuthType.oidc, is_active=True,
            is_admin=False,  # admin must be granted explicitly by an existing admin
        )
        db.add(user)
        await db.flush()  # materialise so user.id is available for join table inserts
        logger.info(f"New OIDC user registered: {email or sub}")
    else:
        user.email = email
        user.name = name or user.name
        user.picture = picture
        user.updated_at = datetime.now(timezone.utc)
        logger.info(f"OIDC user updated: {email or sub}")
    return user


async def _sync_oidc_groups(db: AsyncSession, user: User, raw_groups: list) -> None:
    """Sync group memberships from OIDC claims, using the join table directly."""
    group_names = [
        g.lstrip("/").strip()
        for g in raw_groups
        if isinstance(g, str) and g.strip()
    ]
    group_ids: list = []
    for gname in group_names:
        result = await db.execute(select(Group).where(Group.name == gname))
        grp = result.scalar_one_or_none()
        if grp is None:
            grp = Group(name=gname)
            db.add(grp)
            await db.flush()
            logger.info(f"Auto-created OIDC group: {gname}")
        group_ids.append(grp.id)

    await db.execute(delete(user_groups_table).where(user_groups_table.c.user_id == user.id))
    for gid in group_ids:
        await db.execute(insert(user_groups_table).values(user_id=user.id, group_id=gid))

    user.is_admin = any(n == "Administrator" for n in group_names)


@router.get("/callback")
async def oidc_callback(
    request: Request,
    db: DB,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    if error:
        logger.warning(f"OIDC error: {error}")
        return RedirectResponse(url=f"/auth/login?error={error}", status_code=302)

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code.")

    stored_state = request.cookies.get("oidc_state")
    if not stored_state or stored_state != state:
        raise HTTPException(status_code=400, detail="Invalid state parameter.")

    code_verifier = request.cookies.get("oidc_cv")
    if not code_verifier:
        raise HTTPException(status_code=400, detail="Missing PKCE verifier.")

    oidc_cfg = await SettingsService.get_oidc_config(db)
    if not oidc_cfg:
        raise HTTPException(status_code=503, detail="OIDC not configured.")

    redirect_uri = _get_redirect_uri(request)
    userinfo = await _exchange_oidc_code_for_userinfo(
        oidc_cfg, code, redirect_uri, code_verifier, stored_state
    )

    sub = userinfo.get("sub")
    if not sub:
        raise HTTPException(status_code=400, detail="Missing 'sub' in userinfo.")

    email = userinfo.get("email")
    name = userinfo.get("name", email or sub)
    picture = userinfo.get("picture")

    user = await _sync_oidc_user(db, sub, email, name, picture)

    raw_groups = userinfo.get("groups", [])
    if isinstance(raw_groups, list):
        await _sync_oidc_groups(db, user, raw_groups)

    await db.commit()
    await db.refresh(user)

    if not user.is_active:
        return RedirectResponse(url="/auth/login?error=Account+is+deactivated", status_code=302)

    from app.services.audit_service import audit as _audit
    await _audit(db, "user.login", user=user, resource_type="user",
                 resource_id=user.id, resource_name=user.display_name,
                 details={"method": "oidc"}, request=request)

    max_age = await SettingsService.get_session_max_age(db)
    session_value = encode_session({"user_id": str(user.id)})
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=session_value,
        httponly=True,
        samesite="lax",
        max_age=max_age,
        secure=request.url.scheme == "https",
    )
    response.delete_cookie("oidc_state")
    response.delete_cookie("oidc_nonce")
    response.delete_cookie("oidc_cv")
    return response


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@router.get("/logout")
async def logout(request: Request, reason: Optional[str] = None):
    # Best-effort audit before clearing the session
    if database.is_initialized():
        try:
            from app.services.audit_service import audit as _audit
            maker = database.get_session_maker()
            async with maker() as db:
                from app.deps import decode_session as _decode_session
                cookie = request.cookies.get(settings.SESSION_COOKIE_NAME)
                if cookie:
                    payload = _decode_session(cookie)
                    if payload:
                        import uuid as _uuid
                        from sqlalchemy import select as _select
                        result = await db.execute(_select(User).where(User.id == _uuid.UUID(payload["user_id"])))
                        session_user = result.scalar_one_or_none()
                        if session_user:
                            await _audit(db, "user.logout", user=session_user, resource_type="user",
                                         resource_id=session_user.id, request=request)
        except Exception:
            pass
    redirect_url = "/auth/login?error=Your+account+has+been+deactivated." if reason == "deactivated" else "/auth/login"
    response = RedirectResponse(url=redirect_url, status_code=302)
    response.delete_cookie(settings.SESSION_COOKIE_NAME)
    return response
