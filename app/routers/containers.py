import uuid
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.deps import get_current_active_user, get_db, CurrentUser, DB, decode_session, get_space_access
from app.models.container import TrackedContainer
from app.models.docker_host import DockerHost
from app.models.user import User
from app.services.docker_service import DockerService
from app.services.checker_service import CheckerService
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/containers", tags=["containers"])
from app.templates_setup import templates

docker_service = DockerService()
checker_service = CheckerService()

_VALID_STRATEGIES = {"auto", "major", "minor", "patch", "custom", ""}


# ── Shared helpers ─────────────────────────────────────────────────────────────

async def _get_container_with_access(
    container_id: uuid.UUID,
    user,
    db,
) -> tuple[TrackedContainer, DockerHost]:
    """Load container + host, verify the user has access to the container's space."""
    result = await db.execute(
        select(TrackedContainer).where(TrackedContainer.id == container_id)
    )
    container = result.scalar_one_or_none()
    if not container:
        raise HTTPException(status_code=404, detail="Container not found")

    host_result = await db.execute(
        select(DockerHost).where(DockerHost.id == container.docker_host_id)
    )
    host = host_result.scalar_one_or_none()
    if not host:
        raise HTTPException(status_code=404, detail="Container host not found")

    await get_space_access(host.space_id, user, db)
    return container, host


async def _authenticate_websocket(websocket: WebSocket, db: AsyncSession) -> Optional[User]:
    """Return the authenticated User from a session cookie, or close the WS and return None."""
    cookie_header = websocket.cookies.get(settings.SESSION_COOKIE_NAME)
    session_data = decode_session(cookie_header) if cookie_header else None
    if not session_data or "user_id" not in session_data:
        await websocket.close(code=4001)
        return None
    try:
        uid = uuid.UUID(session_data["user_id"])
    except ValueError:
        await websocket.close(code=4001)
        return None
    result = await db.execute(select(User).where(User.id == uid))
    ws_user = result.scalar_one_or_none()
    if not ws_user or not ws_user.is_active:
        await websocket.close(code=4001)
        return None
    return ws_user


def _build_check_strategy(
    container: TrackedContainer, host: DockerHost, strategy_override: str
) -> tuple[str, Optional[str]]:
    """Resolve the effective strategy and custom pattern for an update check."""
    host_strategy = host.version_strategy if host and host.version_strategy else "auto"
    if strategy_override and strategy_override in _VALID_STRATEGIES:
        pattern = container.version_pattern if strategy_override == "custom" else None
        return strategy_override, pattern
    strategy = container.version_strategy_override or host_strategy
    if strategy != "custom":
        return strategy, None
    if container.version_strategy_override == "custom":
        return strategy, container.version_pattern
    return strategy, host.version_pattern if host else None


def _parse_image_ref(image_ref: str) -> tuple[str, str]:
    """Split 'image:tag' → (image, tag). Falls back to 'latest' for digest/untagged refs."""
    if ":" in image_ref and not image_ref.startswith("sha256:"):
        return image_ref.rsplit(":", 1)
    return image_ref, "latest"


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/{container_id}", response_class=HTMLResponse)
async def container_detail(
    container_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    container, host = await _get_container_with_access(container_id, user, db)

    return templates.TemplateResponse(
        request,
        "containers/detail.html",
        {
            "user": user,
            "container": container,
            "host": host,
            "app_url": settings.APP_URL,
            "now": datetime.now(timezone.utc),
        },
    )


@router.patch("/{container_id}/strategy")
async def update_container_strategy(
    container_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
    strategy: str,
    pattern: str = "",
):
    """Set or clear the per-container version strategy override."""
    if strategy not in _VALID_STRATEGIES:
        raise HTTPException(status_code=400, detail="Invalid strategy")
    container, _ = await _get_container_with_access(container_id, user, db)
    old_strategy = container.version_strategy_override
    container.version_strategy_override = strategy or None
    container.version_pattern = (pattern.strip() or None) if strategy == "custom" else None
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(None, "container.strategy_update", user=user, resource_type="container",
                 resource_id=container.id, resource_name=container.name,
                 details={"from": old_strategy, "to": container.version_strategy_override,
                          "pattern": container.version_pattern,
                          "image": f"{container.image}:{container.tag}"}, request=request)
    return {"strategy": container.version_strategy_override, "pattern": container.version_pattern}


@router.websocket("/{container_id}/logs")
async def container_logs_ws(
    container_id: uuid.UUID,
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
):
    """WebSocket endpoint for live log streaming from a container."""
    await websocket.accept()

    ws_user = await _authenticate_websocket(websocket, db)
    if not ws_user:
        return

    try:
        container, host = await _get_container_with_access(container_id, ws_user, db)
    except HTTPException:
        await websocket.close(code=4004)
        return

    await websocket.send_text(f"INFO: Connecting to {host.name}, streaming logs for {container.name}...\n")

    try:
        await docker_service.stream_logs(host, container.container_id, websocket)
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for container {container_id}")
    except Exception as e:
        logger.error(f"Log streaming error for container {container_id}: {e}")
        try:
            await websocket.send_text("ERROR: Log streaming failed.")
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@router.post("/{container_id}/check")
async def check_container(
    container_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
    strategy_override: str = "",
):
    """Manually trigger an update check for a container.

    strategy_override: if provided (from the UI dropdown), use it instead of the DB-saved value.
    """
    container, host = await _get_container_with_access(container_id, user, db)

    try:
        strategy, pattern = _build_check_strategy(container, host, strategy_override)
        await checker_service.check_container(
            db, container, strategy=strategy, custom_pattern=pattern, force_notify=True
        )
        await db.refresh(container)
        from app.services.audit_service import audit as _audit
        await _audit(None, "container.check", user=user, resource_type="container",
                     resource_id=container.id, resource_name=container.name,
                     details={"image": f"{container.image}:{container.tag}",
                              "has_update": container.has_update}, request=request)
        return {
            "has_update": container.has_update,
            "latest_tag": container.latest_tag,
            "latest_digest": container.latest_digest,
            "last_checked_at": container.last_checked_at.isoformat() if container.last_checked_at else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Check failed for container {container_id}: {e}")
        raise HTTPException(status_code=500, detail="Update check failed. Please try again.")


@router.post("/{container_id}/reload")
async def reload_container(
    container_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    """Re-sync this container's state from Docker (handles restarts/recreates)."""
    container, host = await _get_container_with_access(container_id, user, db)

    try:
        live_containers = await docker_service.list_containers(host)
    except Exception as e:
        logger.error(f"Reload failed for container {container_id}: {e}")
        raise HTTPException(status_code=502, detail="Could not connect to Docker host.")

    match = next((c for c in live_containers if c["name"] == container.name), None)
    if match:
        image_name, tag = _parse_image_ref(match["image"])
        container.container_id = match["container_id"]
        container.image = image_name
        container.tag = tag
        container.status = match["status"]
        container.digest = match.get("digest")
    else:
        container.status = "removed"

    await db.commit()
    return {"status": container.status, "tag": container.tag, "image": container.image}


@router.post("/{container_id}/update")
async def update_container_image(
    container_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    """Pull the latest image and recreate the container with the same configuration."""
    container, host = await _get_container_with_access(container_id, user, db)

    if not container.has_update:
        raise HTTPException(status_code=400, detail="No update available for this container.")

    latest_tag = container.latest_tag or container.tag
    new_image = f"{container.image}:{latest_tag}"

    try:
        result = await docker_service.pull_and_recreate(host, container.container_id, new_image)
    except Exception as e:
        logger.error(f"Image update failed for container {container.name}: {e}")
        raise HTTPException(status_code=500, detail="Container update failed. Check server logs.")

    from app.services.audit_service import audit as _audit

    try:
        live = await docker_service.list_containers(host)
        match = next((c for c in live if c["name"] == container.name), None)
        if match:
            image_name, tag = _parse_image_ref(match["image"])
            container.container_id = match["container_id"]
            container.image = image_name
            container.tag = tag
            container.status = match["status"]
            container.digest = match.get("digest")
    except Exception:
        pass

    container.has_update = False
    container.latest_tag = latest_tag
    container.snoozed_until = None
    await db.commit()

    await _audit(None, "container.update", user=user, resource_type="container",
                 resource_id=container.id, resource_name=container.name,
                 details={"image": new_image}, request=request)

    return {"status": "updated", "image": new_image, "container_status": result.get("status")}


@router.post("/{container_id}/notifications/{log_id}/acknowledge")
async def acknowledge_notification(
    container_id: uuid.UUID,
    log_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    """Acknowledge a specific notification log entry and snooze the container."""
    from app.models.notification import NotificationLog
    from app.templates_setup import _localtime

    container, host = await _get_container_with_access(container_id, user, db)

    log_result = await db.execute(
        select(NotificationLog).where(
            NotificationLog.id == log_id,
            NotificationLog.container_id == container_id,
        )
    )
    log = log_result.scalar_one_or_none()
    if not log:
        raise HTTPException(status_code=404, detail="Notification not found")

    now = datetime.now(timezone.utc)
    log.status = "ack"
    log.status_changed_at = now

    snooze_hours = host.notification_snooze_hours if host and host.notification_snooze_hours else 24
    container.snoozed_until = now + timedelta(hours=snooze_hours)
    snoozed_until = container.snoozed_until

    await db.commit()
    return {
        "status": "ack",
        "status_changed_at": now.isoformat(),
        "status_changed_at_display": _localtime(now, user),
        "snoozed_until": snoozed_until.isoformat() if snoozed_until else None,
        "snooze_until_display": _localtime(snoozed_until, user) if snoozed_until else "",
    }
