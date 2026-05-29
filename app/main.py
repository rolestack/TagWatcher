import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app import database
from app.config_file import get_database_url

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class _HealthCheckFilter(logging.Filter):
    """Suppress noisy health-probe access log lines."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "GET /health" in msg:
            return False
        # /auth/setup from loopback = Docker health check, not a real auth event
        if "GET /auth/setup" in msg and ("127.0.0.1" in msg or "::1" in msg):
            return False
        return True


logging.getLogger("uvicorn.access").addFilter(_HealthCheckFilter())

_DEFAULT_SECRET = "change-me-in-production-use-long-random-string"
if settings.SECRET_KEY == _DEFAULT_SECRET or len(settings.SECRET_KEY) < 32:
    if settings.DEBUG:
        logger.warning(
            "SECRET_KEY is using the insecure default. Set a strong random value in production!"
        )
    else:
        raise RuntimeError(
            "SECRET_KEY must be changed from the default value before running in production. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )


DEFAULT_GROUPS = ["Administrator", "User"]


async def _ensure_default_groups() -> None:
    from sqlalchemy import select
    from app.models.group import Group

    maker = database.get_session_maker()
    async with maker() as db:
        for name in DEFAULT_GROUPS:
            exists = await db.scalar(select(Group).where(Group.name == name))
            if not exists:
                db.add(Group(name=name))
                logger.info(f"Created default group: {name}")
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {settings.APP_NAME}...")

    # Try to load a previously saved database URL (config file or env var).
    db_url = get_database_url()
    if db_url:
        try:
            await database.initialize(db_url, debug=settings.DEBUG)
            await database.init_tables()
            await database.run_migrations()
            await _ensure_default_groups()
            logger.info("Database ready.")
        except Exception as exc:
            logger.error(f"Database initialization failed: {exc}")
            logger.warning("Starting in setup mode — database unreachable.")
    else:
        logger.warning(
            "No DATABASE_URL configured. Starting in setup mode."
        )

    # Load site customization into template cache.
    if database.is_initialized():
        from app.services.settings_service import (
            SettingsService,
            CUSTOM_HEAD_HTML_KEY, FOOTER_TEXT_KEY, CUSTOM_FOOTER_HTML_KEY,
        )
        from app.templates_setup import site_customization
        from app.database import get_session_maker
        async with get_session_maker()() as _db:
            site_customization["custom_head_html"] = await SettingsService.get(_db, CUSTOM_HEAD_HTML_KEY) or ""
            site_customization["footer_text"] = await SettingsService.get(_db, FOOTER_TEXT_KEY) or ""
            site_customization["custom_footer_html"] = await SettingsService.get(_db, CUSTOM_FOOTER_HTML_KEY) or ""

    # Initialize file logging (reads logging.properties if present).
    from app.logging_config import setup_file_logging
    setup_file_logging()
    logger.info("File logging configured.")

    # Start the update-check scheduler only when DB is ready.
    if database.is_initialized():
        from app.services.scheduler import scheduler, register_host_job
        from app.models.docker_host import DockerHost
        from sqlalchemy import select as _sa_select

        scheduler.start()

        # Load all active hosts and register per-host scheduler jobs.
        _maker = database.get_session_maker()
        async with _maker() as _db:
            _result = await _db.execute(
                _sa_select(DockerHost).where(
                    DockerHost.is_active == True,       # noqa: E712
                    DockerHost.auto_check_updates == True,  # noqa: E712
                )
            )
            _hosts = _result.scalars().all()
            for _host in _hosts:
                register_host_job(_host)

        logger.info(
            f"Scheduler started — {len(_hosts)} host job(s) registered "
            f"(global default: {settings.CHECK_INTERVAL_MINUTES} min)"
        )

    yield

    from app.services.scheduler import scheduler as sched
    if sched.running:
        sched.shutdown(wait=False)
    logger.info(f"{settings.APP_NAME} shut down.")


app = FastAPI(
    title=settings.APP_NAME,
    description="Docker container image update monitoring",
    version="1.0.0",
    debug=settings.DEBUG,
    lifespan=lifespan,
    docs_url="/api/docs" if settings.DEBUG else None,
    redoc_url="/api/redoc" if settings.DEBUG else None,
)

# ---------------------------------------------------------------------------
# Reverse proxy headers
# ---------------------------------------------------------------------------

if settings.BEHIND_PROXY:
    class ProxyHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            forwarded_for = request.headers.get("X-Forwarded-For")
            if forwarded_for:
                request.scope["client"] = (forwarded_for.split(",")[0].strip(), 0)
            proto = request.headers.get("X-Forwarded-Proto")
            if proto:
                request.scope["scheme"] = proto
            return await call_next(request)

    app.add_middleware(ProxyHeadersMiddleware)

# ---------------------------------------------------------------------------
# Setup guard — redirect to /auth/setup until everything is configured
# ---------------------------------------------------------------------------

_SETUP_BYPASS = (
    "/auth/setup",
    "/auth/login",
    "/auth/local-login",
    "/auth/logout",
    "/static",
    "/assets",
    "/favicon.ico",
    "/brand/favicon",
    "/health",
)


class SetupGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in _SETUP_BYPASS):
            return await call_next(request)

        # Step 1: DB not yet configured
        if not database.is_initialized():
            return RedirectResponse(url="/auth/setup", status_code=302)

        # Step 2: No admin account yet
        try:
            maker = database.get_session_maker()
            async with maker() as db:
                from app.models.user import User
                count = await db.scalar(
                    select(func.count()).where(User.is_admin == True)  # noqa: E712
                )
            if not count:
                return RedirectResponse(url="/auth/setup", status_code=302)
        except Exception:
            return RedirectResponse(url="/auth/setup", status_code=302)

        return await call_next(request)


app.add_middleware(SetupGuardMiddleware)


class SessionRefreshMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        refresh = getattr(request.state, "session_refresh", None)
        if refresh:
            response.set_cookie(
                key=settings.SESSION_COOKIE_NAME,
                value=refresh,
                httponly=True,
                samesite="lax",
                max_age=request.state.session_max_age,
                secure=getattr(request.state, "session_secure", False),
            )
        return response


app.add_middleware(SessionRefreshMiddleware)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# ---------------------------------------------------------------------------
# Static files & templates
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/assets", StaticFiles(directory="app/static/uploads"), name="assets")
from app.templates_setup import templates  # noqa: E402

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

from app.routers import auth, dashboard, spaces, docker_hosts, containers, notifications, admin, account  # noqa: E402
from app.models import audit_log as _audit_log_model  # noqa: F401 — ensure table registered

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(spaces.router)
app.include_router(docker_hosts.router)
app.include_router(containers.router)
app.include_router(notifications.router)
app.include_router(admin.router)
app.include_router(account.router)


@app.get("/health", include_in_schema=False)
async def health_check():
    """Lightweight health probe — use this in docker-compose healthcheck instead of /auth/setup."""
    return {"status": "ok"}


@app.get("/brand/favicon", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon_endpoint():
    import mimetypes
    from fastapi.responses import Response, RedirectResponse
    from fastapi import HTTPException
    if not database.is_initialized():
        raise HTTPException(404)
    from app.services.settings_service import SettingsService, BRAND_FAVICON_URL_KEY
    maker = database.get_session_maker()
    async with maker() as db:
        url = await SettingsService.get(db, BRAND_FAVICON_URL_KEY)
    if not url:
        raise HTTPException(404)
    url = url.strip()
    if url.startswith("/static/uploads/"):
        url = "/assets/" + url[len("/static/uploads/"):]
    # Serve local uploads directly (avoids redirect — browsers handle favicon redirects unreliably)
    if url.startswith("/assets/"):
        filename = url[len("/assets/"):]
        file_path = f"app/static/uploads/{filename}"
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            media_type = mimetypes.guess_type(filename)[0] or "image/x-icon"
            return Response(content=content, media_type=media_type,
                            headers={"Cache-Control": "max-age=3600, must-revalidate"})
        except FileNotFoundError:
            raise HTTPException(404)
    return RedirectResponse(url=url, status_code=302)

# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


@app.exception_handler(302)
async def redirect_handler(request: Request, exc):
    return RedirectResponse(url=exc.headers.get("Location", "/auth/login"), status_code=302)


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return templates.TemplateResponse(
        request,
        "base.html", {"error": "Page not found (404)"}, status_code=404
    )


@app.exception_handler(403)
async def forbidden_handler(request: Request, exc):
    return templates.TemplateResponse(
        request,
        "base.html", {"error": "Access denied (403)"}, status_code=403
    )


@app.exception_handler(500)
async def server_error_handler(request: Request, exc):
    logger.exception(f"Internal server error: {exc}")
    return templates.TemplateResponse(
        request,
        "base.html", {"error": "Internal server error (500)"}, status_code=500
    )
