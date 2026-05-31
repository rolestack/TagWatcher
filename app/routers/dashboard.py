import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func

from sqlalchemy.orm import selectinload

from app.deps import DB, CurrentUser
from app.models.space import Space
from app.models.docker_host import DockerHost
from app.models.container import TrackedContainer
from app.models.notification import NotificationLog
from app.models.group import group_spaces

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])
from app.templates_setup import templates


def _period_start(period: str) -> datetime:
    try:
        tz = ZoneInfo(os.environ.get("TZ", "UTC"))
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    now_local = datetime.now(tz)
    if period == "week":
        week_start = (now_local - timedelta(days=now_local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return week_start.astimezone(timezone.utc).replace(tzinfo=None)
    if period == "month":
        return now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
    return now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: CurrentUser,
    db: DB,
    period: str = Query(default="today"),
):
    """Main dashboard: summary stats, accessible spaces, recent notifications."""

    if period not in ("today", "week", "month"):
        period = "today"

    # Get spaces accessible by the user
    if user.is_admin:
        result = await db.execute(
            select(Space).options(selectinload(Space.docker_hosts))
        )
        accessible_spaces = result.scalars().all()
    else:
        user_group_ids = [g.id for g in user.groups]
        if user_group_ids:
            result = await db.execute(
                select(Space)
                .options(selectinload(Space.docker_hosts))
                .join(group_spaces, group_spaces.c.space_id == Space.id)
                .where(group_spaces.c.group_id.in_(user_group_ids))
                .distinct()
            )
            accessible_spaces = result.scalars().all()
        else:
            accessible_spaces = []

    space_ids = [s.id for s in accessible_spaces]

    if space_ids:
        host_result = await db.execute(
            select(func.count(DockerHost.id)).where(DockerHost.space_id.in_(space_ids))
        )
        total_hosts = host_result.scalar() or 0

        host_ids_result = await db.execute(
            select(DockerHost.id).where(DockerHost.space_id.in_(space_ids))
        )
        host_ids = [r[0] for r in host_ids_result.all()]

        if host_ids:
            container_result = await db.execute(
                select(func.count(TrackedContainer.id)).where(
                    TrackedContainer.docker_host_id.in_(host_ids),
                    TrackedContainer.status != "removed",
                )
            )
            total_containers = container_result.scalar() or 0

            updates_result = await db.execute(
                select(func.count(TrackedContainer.id)).where(
                    TrackedContainer.docker_host_id.in_(host_ids),
                    TrackedContainer.status != "removed",
                    TrackedContainer.has_update == True,  # noqa: E712
                )
            )
            pending_updates = updates_result.scalar() or 0

            # Check if any notifications exist (regardless of period) for empty-state messaging
            any_count = await db.scalar(
                select(func.count(NotificationLog.id))
                .join(TrackedContainer, TrackedContainer.id == NotificationLog.container_id)
                .where(TrackedContainer.docker_host_id.in_(host_ids))
            )
            has_notifications = (any_count or 0) > 0

            notif_result = await db.execute(
                select(NotificationLog)
                .join(TrackedContainer, TrackedContainer.id == NotificationLog.container_id)
                .where(
                    TrackedContainer.docker_host_id.in_(host_ids),
                    NotificationLog.sent_at >= _period_start(period),
                )
                .options(
                    selectinload(NotificationLog.container)
                    .selectinload(TrackedContainer.docker_host)
                    .selectinload(DockerHost.space)
                )
                .order_by(NotificationLog.sent_at.desc())
                .limit(200)
            )
            recent_notifications = notif_result.scalars().all()
        else:
            total_containers = 0
            pending_updates = 0
            has_notifications = False
            recent_notifications = []
    else:
        total_hosts = 0
        total_containers = 0
        pending_updates = 0
        has_notifications = False
        recent_notifications = []

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "spaces": accessible_spaces,
            "total_hosts": total_hosts,
            "total_containers": total_containers,
            "pending_updates": pending_updates,
            "recent_notifications": recent_notifications,
            "has_notifications": has_notifications,
            "period": period,
        },
    )
