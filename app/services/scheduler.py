import logging
import os
import uuid as _uuid

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from app.config import settings

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")

_PG_LOCK_BASE = 7_749_283_641


def _host_lock_id(host_id: str) -> int:
    """Deterministic per-host advisory lock ID, stays within pg bigint range."""
    return (_PG_LOCK_BASE + int(_uuid.UUID(host_id)) % 10_000_000) % (2 ** 62)


async def _run_check_host(host_id: str) -> None:
    """Check a specific host for image updates.

    Uses a per-host PG advisory lock so only one gunicorn worker runs
    the check at a time when multiple workers share the same DB.
    """
    from app.database import get_session_maker
    from app.services.checker_service import CheckerService
    from app.models.docker_host import DockerHost
    from sqlalchemy import select, text

    checker = CheckerService()
    maker = get_session_maker()
    lock_id = _host_lock_id(host_id)

    async with maker() as db:
        locked = await db.scalar(text(f"SELECT pg_try_advisory_lock({lock_id})"))
        if not locked:
            logger.debug(f"Scheduler: another worker is already checking host {host_id}, skipping.")
            return
        try:
            result = await db.execute(
                select(DockerHost).where(DockerHost.id == _uuid.UUID(host_id))
            )
            host = result.scalar_one_or_none()
            if host and host.is_active and host.auto_check_updates:
                await checker.check_host(db, host, aggregate_notify=True)
        except Exception as e:
            logger.error(f"Scheduled check failed for host {host_id}: {e}")
        finally:
            await db.execute(text(f"SELECT pg_advisory_unlock({lock_id})"))


def _job_id(host_id: str, idx: int = 0) -> str:
    return f"host_{host_id}" if idx == 0 else f"host_{host_id}_{idx}"


def _system_tz() -> str:
    return os.environ.get("TZ", "UTC")


def _register_schedule_jobs(host_id: str, host, times: list[str], tz: str) -> None:
    registered = []
    for i, time_str in enumerate(times):
        try:
            hh, mm = time_str.split(":")
            trigger = CronTrigger(hour=int(hh), minute=int(mm), timezone=tz)
            scheduler.add_job(
                _run_check_host,
                trigger=trigger,
                args=[host_id],
                id=_job_id(host_id, i),
                name=f"Check {host.name} at {time_str}",
                replace_existing=True,
                misfire_grace_time=300,
            )
            registered.append(time_str)
        except Exception as e:
            logger.warning(f"Scheduler: invalid schedule time '{time_str}' for host '{host.name}': {e}")
    if registered:
        logger.info(f"Scheduler: '{host.name}' → daily at {', '.join(registered)} ({tz})")


def register_host_job(host) -> None:
    """Create or replace APScheduler job(s) for a host based on its schedule config.

    Called at startup for each host, and from routers whenever a host is
    added or its settings change.
    """
    host_id = str(host.id)

    # Remove all existing jobs for this host before re-registering.
    for job in list(scheduler.get_jobs()):
        if job.id == _job_id(host_id) or job.id.startswith(f"host_{host_id}_"):
            scheduler.remove_job(job.id)

    if not host.is_active or not host.auto_check_updates:
        logger.info(f"Scheduler: no job registered for host '{host.name}' (disabled or auto-check off)")
        return

    tz = _system_tz()

    if host.check_schedule_time:
        times = [t.strip() for t in host.check_schedule_time.split(",") if t.strip()]
        _register_schedule_jobs(host_id, host, times, tz)
    else:
        interval = host.check_interval_minutes or settings.CHECK_INTERVAL_MINUTES
        scheduler.add_job(
            _run_check_host,
            trigger=IntervalTrigger(minutes=interval),
            args=[host_id],
            id=_job_id(host_id),
            name=f"Check {host.name} every {interval}m",
            replace_existing=True,
            misfire_grace_time=60,
        )
        logger.info(f"Scheduler: '{host.name}' → every {interval} min")


def unregister_host_job(host_id: str) -> None:
    """Remove all APScheduler jobs for a host (called on host deletion)."""
    removed = 0
    for job in list(scheduler.get_jobs()):
        if job.id == _job_id(host_id) or job.id.startswith(f"host_{host_id}_"):
            scheduler.remove_job(job.id)
            removed += 1
    if removed:
        logger.info(f"Scheduler: removed {removed} job(s) for host {host_id}")
