import re
import uuid
import secrets
import logging
from datetime import datetime, timedelta, timezone

_ERR_HOST_NOT_FOUND = "Host not found"
from typing import Optional
from fastapi import APIRouter, Depends, Request, HTTPException, status, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.deps import get_current_active_user, get_db, CurrentUser, DB, get_space_access
from app.models.docker_host import DockerHost
from app.models.space import Space
from app.services.docker_service import DockerService
from app.services.checker_service import CheckerService
from app.security import validate_docker_host_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/spaces/{space_id}/hosts", tags=["docker-hosts"])
from app.templates_setup import templates

docker_service = DockerService()
checker_service = CheckerService()


def _parse_schedule_fields(
    check_schedule_time: str, check_interval_minutes: str
) -> tuple[str | None, int | None]:
    """Parse schedule form fields into (schedule_time_str, interval_minutes)."""
    raw_times = check_schedule_time.strip() if check_schedule_time else ""
    valid_times = sorted({
        t.strip() for t in raw_times.split(",")
        if re.match(r'^\d{2}:\d{2}$', t.strip())
    })
    if valid_times:
        return ",".join(valid_times), None
    try:
        interval = int(check_interval_minutes) if check_interval_minutes.strip() else None
    except ValueError:
        interval = None
    return None, interval


@router.get("", response_class=HTMLResponse)
async def list_hosts(
    space_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    space = await get_space_access(space_id, user, db)
    return templates.TemplateResponse(
        request,
        "hosts/list.html",
        {
            "user": user,
            "space": space,
            "hosts": space.docker_hosts,
        },
    )


@router.get("/{host_id}/edit", response_class=HTMLResponse)
async def edit_host_page(
    space_id: uuid.UUID,
    host_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    space = await get_space_access(space_id, user, db)

    result = await db.execute(
        select(DockerHost).where(DockerHost.id == host_id, DockerHost.space_id == space_id)
    )
    host = result.scalar_one_or_none()
    if not host:
        raise HTTPException(status_code=404, detail=_ERR_HOST_NOT_FOUND)

    from app.config import settings as _settings
    return templates.TemplateResponse(
        request,
        "hosts/edit.html",
        {"user": user, "space": space, "host": host, "settings": _settings},
    )


@router.get("/{host_id}", response_class=HTMLResponse)
async def host_detail(
    space_id: uuid.UUID,
    host_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    space = await get_space_access(space_id, user, db)

    result = await db.execute(
        select(DockerHost).where(DockerHost.id == host_id, DockerHost.space_id == space_id)
    )
    host = result.scalar_one_or_none()
    if not host:
        raise HTTPException(status_code=404, detail=_ERR_HOST_NOT_FOUND)

    import fnmatch as _fnmatch
    active_containers = [c for c in host.tracked_containers if c.status != "removed"]
    patterns = [p.strip() for p in (host.exclude_patterns or "").splitlines() if p.strip()]
    excluded_ids = {
        c.id for c in active_containers
        if any(
            _fnmatch.fnmatch(f"{c.image}:{c.tag}", p) or _fnmatch.fnmatch(c.image, p)
            for p in patterns
        )
    }
    return templates.TemplateResponse(
        request,
        "hosts/detail.html",
        {
            "user": user,
            "space": space,
            "host": host,
            "containers": active_containers,
            "excluded_ids": excluded_ids,
        },
    )


@router.post("/check-all")
async def check_all_hosts(
    space_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    """Manually trigger an update check for all hosts in this space."""
    from sqlalchemy import select as sa_select
    space = await get_space_access(space_id, user, db)

    result = await db.execute(
        sa_select(DockerHost).where(
            DockerHost.space_id == space_id,
            DockerHost.is_active == True,  # noqa: E712
        )
    )
    hosts = result.scalars().all()

    for host in hosts:
        try:
            await checker_service.check_host(db, host)
        except Exception as e:
            logger.error(f"Check failed for host {host.name}: {e}")

    from app.services.audit_service import audit as _audit
    await _audit(None, "host.check_all", user=user, resource_type="space",
                 resource_name=space.name, details={"hosts_checked": len(hosts)}, request=request)
    logger.info(f"Manual check-all triggered for space {space.name} by {user.email}")
    return {"message": f"Check completed for {len(hosts)} host(s)"}


@router.post("", response_class=RedirectResponse)
async def add_host(
    space_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
    name: str = Form(...),
    host_type: str = Form("tcp"),
    host_url: str = Form(""),
    use_tls: bool = Form(False),
    tls_ca: str = Form(""),
    tls_cert: str = Form(""),
    tls_key: str = Form(""),
    auto_check_updates: str = Form("on"),
):
    space = await get_space_access(space_id, user, db)

    if host_type not in ("tcp", "unix", "agent"):
        host_type = "tcp"

    reg_token: str | None = None
    reg_expires: datetime | None = None

    if host_type == "agent":
        reg_token = secrets.token_urlsafe(32)
        reg_expires = datetime.now(timezone.utc) + timedelta(hours=24)
        host_url = ""
    else:
        url_error = validate_docker_host_url(host_url)
        if url_error:
            raise HTTPException(status_code=400, detail=url_error)

    host = DockerHost(
        space_id=space_id,
        name=name,
        host_type=host_type,
        host_url=host_url,
        use_tls=use_tls,
        tls_ca=tls_ca or None,
        tls_cert=tls_cert or None,
        tls_key=tls_key or None,
        is_active=True,
        auto_check_updates=auto_check_updates == "on",
        agent_registration_token=reg_token,
        agent_registration_token_expires_at=reg_expires,
    )
    db.add(host)
    await db.commit()
    await db.refresh(host)
    from app.services.audit_service import audit as _audit
    await _audit(db, "host.create", user=user, resource_type="host",
                 resource_id=host.id, resource_name=host.name,
                 details={"space": space.name, "type": host_type, "url": host_url}, request=request)
    from app.services.scheduler import register_host_job as _reg_job
    _reg_job(host)
    logger.info(f"Added host: {host.name} (type={host_type}) to space {space.name} by {user.email}")
    return RedirectResponse(url=f"/spaces/{space_id}/hosts/{host.id}", status_code=302)


@router.post("/{host_id}/generate-token", response_class=JSONResponse)
async def generate_agent_token(
    space_id: uuid.UUID,
    host_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
):
    """Generate a new one-time registration token for an agent-type host."""
    await get_space_access(space_id, user, db)
    result = await db.execute(
        select(DockerHost).where(DockerHost.id == host_id, DockerHost.space_id == space_id)
    )
    host = result.scalar_one_or_none()
    if not host or host.host_type != "agent":
        raise HTTPException(status_code=404, detail="Agent host not found")

    token = secrets.token_urlsafe(32)
    host.agent_registration_token = token
    host.agent_registration_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    host.agent_secret = None
    host.host_url = ""
    await db.commit()
    logger.info(f"Generated new registration token for agent host '{host.name}' by {user.email}")
    return {"token": token}


@router.post("/{host_id}")
async def update_host(
    space_id: uuid.UUID,
    host_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
    name: str = Form(...),
    host_url: str = Form(""),
    is_active: str = Form("off"),
    auto_check_updates: str = Form("off"),
    check_interval_minutes: str = Form(""),
    check_schedule_time: str = Form(""),
    version_strategy: str = Form("auto"),
    version_pattern: str = Form(""),
    exclude_patterns: str = Form(""),
    notification_snooze_hours: str = Form("24"),
    use_tls: str = Form("off"),
    tls_ca: str = Form(""),
    tls_cert: str = Form(""),
    tls_key: str = Form(""),
    agent_allowed_cidrs: str = Form(""),
):
    space = await get_space_access(space_id, user, db)

    result = await db.execute(
        select(DockerHost).where(DockerHost.id == host_id, DockerHost.space_id == space_id)
    )
    host = result.scalar_one_or_none()
    if not host:
        raise HTTPException(status_code=404, detail=_ERR_HOST_NOT_FOUND)

    host.name = name.strip()
    if host.host_type != "agent":
        url_error = validate_docker_host_url(host_url.strip())
        if url_error:
            raise HTTPException(status_code=400, detail=url_error)
        host.host_url = host_url.strip()
    host.is_active = is_active == "on"
    host.auto_check_updates = auto_check_updates == "on"
    _valid = {"auto", "major", "minor", "patch", "custom"}
    host.version_strategy = version_strategy if version_strategy in _valid else "auto"
    host.version_pattern = version_pattern.strip() or None if version_strategy == "custom" else None
    host.exclude_patterns = exclude_patterns.strip() or None
    try:
        host.notification_snooze_hours = max(1, int(notification_snooze_hours))
    except (ValueError, TypeError):
        host.notification_snooze_hours = 24
    host.use_tls = use_tls == "on"
    host.tls_ca = tls_ca.strip() or None
    host.tls_cert = tls_cert.strip() or None
    host.tls_key = tls_key.strip() or None
    if host.host_type == "agent":
        host.agent_allowed_cidrs = agent_allowed_cidrs.strip() or None
    host.check_schedule_time, host.check_interval_minutes = _parse_schedule_fields(
        check_schedule_time, check_interval_minutes
    )

    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(db, "host.update", user=user, resource_type="host",
                 resource_id=host.id, resource_name=host.name,
                 details={"url": host.host_url, "active": host.is_active}, request=request)
    from app.services.scheduler import register_host_job as _reg_job
    _reg_job(host)
    logger.info(f"Updated docker host: {host.name} by {user.email}")
    return RedirectResponse(url=f"/spaces/{space_id}/hosts/{host_id}", status_code=303)


@router.delete("/{host_id}")
async def remove_host(
    space_id: uuid.UUID,
    host_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    space = await get_space_access(space_id, user, db)

    result = await db.execute(
        select(DockerHost).where(DockerHost.id == host_id, DockerHost.space_id == space_id)
    )
    host = result.scalar_one_or_none()
    if not host:
        raise HTTPException(status_code=404, detail=_ERR_HOST_NOT_FOUND)

    hname = host.name
    from app.services.scheduler import unregister_host_job as _unreg_job
    _unreg_job(str(host_id))
    await db.delete(host)
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(db, "host.delete", user=user, resource_type="host",
                 resource_id=str(host_id), resource_name=hname, request=request)
    await db.commit()
    logger.info(f"Removed docker host: {hname} by {user.email}")
    return {"message": "Host removed"}


@router.post("/{host_id}/sync")
async def sync_host(
    space_id: uuid.UUID,
    host_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    """Manually trigger a container sync for this host."""
    space = await get_space_access(space_id, user, db)

    result = await db.execute(
        select(DockerHost).where(DockerHost.id == host_id, DockerHost.space_id == space_id)
    )
    host = result.scalar_one_or_none()
    if not host:
        raise HTTPException(status_code=404, detail=_ERR_HOST_NOT_FOUND)

    try:
        await docker_service.sync_containers(db, host)
        from app.services.audit_service import audit as _audit
        await _audit(db, "host.sync", user=user, resource_type="host",
                     resource_id=host.id, resource_name=host.name, request=request)
        logger.info(f"Synced containers for host {host.name} by {user.email}")
        return {"message": "Sync completed"}
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Sync failed for host {host.name}: {e}")
        try:
            host.last_sync_error = error_msg
            await db.commit()
        except Exception:
            await db.rollback()
        raise HTTPException(status_code=500, detail="Sync failed. Check server logs for details.")


@router.get("/{host_id}/check-progress")
async def check_progress(
    space_id: uuid.UUID,
    host_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
):
    """Return current progress of an in-progress update check for this host."""
    from app.services.checker_service import get_check_progress
    return get_check_progress(str(host_id))


@router.post("/{host_id}/check")
async def check_host(
    space_id: uuid.UUID,
    host_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    """Manually trigger an update check for a single host."""
    space = await get_space_access(space_id, user, db)

    result = await db.execute(
        select(DockerHost).where(DockerHost.id == host_id, DockerHost.space_id == space_id)
    )
    host = result.scalar_one_or_none()
    if not host:
        raise HTTPException(status_code=404, detail=_ERR_HOST_NOT_FOUND)

    try:
        await checker_service.check_host(db, host, force_notify=True, aggregate_notify=True)
        from app.services.audit_service import audit as _audit
        await _audit(db, "host.check", user=user, resource_type="host",
                     resource_id=host.id, resource_name=host.name, request=request)
        logger.info(f"Manual update check for host {host.name} by {user.email}")
        return {"message": "Update check completed"}
    except Exception as e:
        logger.error(f"Update check failed for host {host.name}: {e}")
        raise HTTPException(status_code=500, detail="Update check failed. Check server logs for details.")
