import fnmatch
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.docker_host import DockerHost
from app.models.container import TrackedContainer
from app.models.notification import NotificationChannel, NotificationLog
from app.models.space import Space
from app.services.docker_service import DockerService
from app.services.registry_service import RegistryService
from app.services.notification_service import NotificationService
from app.config import settings

logger = logging.getLogger(__name__)

# Per-host check progress: {str(host_id): {"total": N, "done": N}}
_check_progress: dict[str, dict] = {}


def get_check_progress(host_id: str) -> dict:
    return _check_progress.get(host_id, {"total": 0, "done": 0, "running": False})


docker_service = DockerService()
registry_service = RegistryService()
notification_service = NotificationService()


class CheckerService:

    # ── Scheduler entry point ──────────────────────────────────────────────────

    async def check_all_hosts(self, db: AsyncSession):
        """Get all active docker hosts and check each one."""
        result = await db.execute(
            select(DockerHost).where(
                DockerHost.is_active == True,  # noqa: E712
                DockerHost.auto_check_updates == True,  # noqa: E712
            )
        )
        hosts = result.scalars().all()
        logger.debug(f"Scheduler poll: {len(hosts)} active host(s) eligible")

        now = datetime.now(timezone.utc)
        for host in hosts:
            if not self._should_check(host, now):
                continue
            try:
                await self.check_host(db, host, aggregate_notify=True)
            except Exception as e:
                logger.error(f"Error checking host {host.name}: {e}")

    # ── Schedule helpers ───────────────────────────────────────────────────────

    def _should_check(self, host: DockerHost, now: datetime) -> bool:
        """Return True if this host is due for an update check."""
        if host.check_schedule_time:
            return self._is_schedule_time_due(host, now)
        interval = host.check_interval_minutes or settings.CHECK_INTERVAL_MINUTES
        if host.last_update_check_at:
            elapsed = (now - host.last_update_check_at).total_seconds() / 60
            if elapsed < interval:
                logger.debug(f"Skipping {host.name}: {elapsed:.0f}m elapsed, interval {interval}m")
                return False
        return True

    def _is_schedule_time_due(self, host: DockerHost, now: datetime) -> bool:
        """Check whether any scheduled time slot is due and hasn't run yet."""
        try:
            tz = ZoneInfo(os.environ.get("TZ", "UTC"))
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        now_local = now.astimezone(tz)
        last_local = host.last_update_check_at.astimezone(tz) if host.last_update_check_at else None
        for time_str in host.check_schedule_time.split(","):
            time_str = time_str.strip()
            try:
                hh, mm = map(int, time_str.split(":"))
                scheduled = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if now_local < scheduled:
                    continue
                if last_local is None or last_local < scheduled:
                    return True
            except Exception:
                continue
        return False

    # ── Host-level check ───────────────────────────────────────────────────────

    async def _check_containers_for_host(
        self, db: AsyncSession, checkable: list, host: DockerHost,
        host_strategy: str, host_key: str, force_notify: bool, aggregate_notify: bool
    ) -> list:
        containers_to_notify: list[TrackedContainer] = []
        try:
            for container in checkable:
                strategy = container.version_strategy_override or host_strategy
                pattern = self._resolve_pattern(container, host, strategy)
                try:
                    should_notify = await self.check_container(
                        db, container,
                        strategy=strategy, custom_pattern=pattern,
                        force_notify=force_notify, skip_notify=aggregate_notify,
                    )
                    if aggregate_notify and should_notify:
                        containers_to_notify.append(container)
                except Exception as e:
                    logger.error(f"Error checking container {container.name}: {e}")
                    await self._safe_rollback(db)
                finally:
                    _check_progress[host_key]["done"] += 1
        finally:
            _check_progress.pop(host_key, None)
        return containers_to_notify

    async def check_host(self, db: AsyncSession, host: DockerHost,
                         force_notify: bool = False, aggregate_notify: bool = False):
        """Sync containers then check each for updates."""
        check_start_time = datetime.now(timezone.utc)
        logger.info(f"Checking host: {host.name} ({host.host_url})")
        try:
            await docker_service.sync_containers(db, host)
        except Exception as e:
            logger.error(f"Failed to sync containers for host {host.name}: {e}")
            return

        exclude_patterns = [
            p.strip()
            for p in (host.exclude_patterns or "").splitlines()
            if p.strip()
        ]

        result = await db.execute(
            select(TrackedContainer).where(
                TrackedContainer.docker_host_id == host.id,
                TrackedContainer.status != "removed",
            )
        )
        containers = result.scalars().all()

        host_strategy = host.version_strategy if hasattr(host, "version_strategy") and host.version_strategy else "auto"
        host_key = str(host.id)
        checkable = [
            c for c in containers
            if not any(
                fnmatch.fnmatch(f"{c.image}:{c.tag}", p) or fnmatch.fnmatch(c.image, p)
                for p in exclude_patterns
            )
        ]
        _check_progress[host_key] = {"total": len(checkable), "done": 0, "running": True}

        containers_to_notify = await self._check_containers_for_host(
            db, checkable, host, host_strategy, host_key, force_notify, aggregate_notify
        )

        if aggregate_notify and containers_to_notify:
            try:
                await self.send_aggregated_notifications_for_host(db, host, containers_to_notify, check_start_time)
            except Exception as e:
                logger.error(f"Failed to send aggregated notifications for host {host.name}: {e}")

        host.last_update_check_at = datetime.now(timezone.utc)
        await db.commit()

    @staticmethod
    def _resolve_pattern(container: TrackedContainer, host: DockerHost, strategy: str) -> str | None:
        if container.version_strategy_override == "custom":
            return container.version_pattern
        if strategy == "custom":
            return host.version_pattern
        return None

    # ── Container-level check ──────────────────────────────────────────────────

    async def check_container(self, db: AsyncSession, container: TrackedContainer,
                              strategy: str = "auto", custom_pattern: str | None = None,
                              force_notify: bool = False, skip_notify: bool = False) -> bool:
        """Check a single container for image updates. Returns True if a notification should be sent."""
        logger.debug(f"Checking container: {container.name} ({container.image}:{container.tag})")
        container.last_checked_at = datetime.now(timezone.utc)

        try:
            result = await registry_service.find_latest_version(
                container.image, container.tag, strategy=strategy, custom_pattern=custom_pattern
            )
        except Exception as e:
            logger.warning(f"Registry check failed for {container.name}: {e}")
            await db.commit()
            return False

        if result is None:
            await db.commit()
            return False

        latest_tag, latest_digest, release_date_str = result

        had_update_before = container.has_update
        old_latest_tag = container.latest_tag
        old_latest_digest = container.latest_digest
        had_snooze = container.snoozed_until is not None
        not_acked = had_update_before and container.snoozed_until is None

        has_update = self._detect_has_update(container, latest_tag, latest_digest)
        container.latest_tag = latest_tag
        container.latest_digest = latest_digest
        container.has_update = has_update
        self._set_release_date(container, release_date_str)
        await db.commit()

        now = datetime.now(timezone.utc)
        is_snoozed = container.snoozed_until is not None and container.snoozed_until > now

        tag_changed = has_update and latest_tag != old_latest_tag
        digest_changed = has_update and bool(latest_digest) and latest_digest != old_latest_digest
        snooze_expired = has_update and had_update_before and had_snooze and not is_snoozed
        should_notify = (
            has_update
            and (not had_update_before or tag_changed or digest_changed or snooze_expired or force_notify or not_acked)
            and not is_snoozed
        )
        logger.debug(
            f"[{container.name}] has_update={has_update} had_before={had_update_before} "
            f"tag_changed={tag_changed} digest_changed={digest_changed} "
            f"snooze_expired={snooze_expired} not_acked={not_acked} is_snoozed={is_snoozed} "
            f"→ should_notify={should_notify}"
        )

        if should_notify and not skip_notify:
            try:
                await self.send_notifications_for_container(db, container)
            except Exception as e:
                logger.error(f"Failed to send notifications for {container.name}: {e}")
                await self._safe_rollback(db)

        return should_notify

    @staticmethod
    def _detect_has_update(
        container: TrackedContainer, latest_tag: str, latest_digest: str | None
    ) -> bool:
        """Determine whether a newer image is available for this container."""
        tag_lower = container.tag.lower()
        is_rolling = tag_lower in {"latest", "stable", "edge", "nightly", "main", "master", "develop"}
        same_tag_returned = latest_tag == container.tag
        if is_rolling or same_tag_returned:
            if not latest_digest:
                return False
            if container.digest:
                return latest_digest != container.digest
            if container.latest_digest:
                return latest_digest != container.latest_digest
            return False
        return bool(latest_tag and latest_tag != container.tag)

    @staticmethod
    def _set_release_date(container: TrackedContainer, release_date_str) -> None:
        if not release_date_str:
            return
        try:
            if isinstance(release_date_str, str):
                container.release_date = datetime.fromisoformat(
                    release_date_str.replace("Z", "+00:00")
                )
            else:
                container.release_date = release_date_str
        except Exception:
            pass

    @staticmethod
    async def _safe_rollback(db: AsyncSession) -> None:
        try:
            await db.rollback()
        except Exception:
            pass

    # ── Notification dispatch ──────────────────────────────────────────────────

    async def send_notifications_for_container(
        self, db: AsyncSession, container: TrackedContainer
    ):
        """Get notification channels for the container's space and send to all active ones."""
        from app.models.docker_host import DockerHost
        host_result = await db.execute(
            select(DockerHost).where(DockerHost.id == container.docker_host_id)
        )
        host = host_result.scalar_one_or_none()
        if not host:
            return

        space_result = await db.execute(select(Space).where(Space.id == host.space_id))
        space = space_result.scalar_one_or_none()
        space_name = space.name if space else ""
        check_time = datetime.now(timezone.utc)

        channels_result = await db.execute(
            select(NotificationChannel).where(
                NotificationChannel.space_id == host.space_id,
                NotificationChannel.is_active == True,  # noqa: E712
            )
        )
        channels = channels_result.scalars().all()

        for channel in channels:
            log = NotificationLog(
                container_id=container.id,
                channel_id=channel.id,
                old_tag=container.tag,
                old_digest=container.digest,
                new_tag=container.latest_tag,
                new_digest=container.latest_digest,
                release_date=container.release_date,
                status="sent",
            )
            try:
                await notification_service.send_update_notification(
                    channel, container, settings.APP_URL,
                    host_name=host.name, space_name=space_name, check_time=check_time,
                )
                logger.info(
                    f"Sent notification for {container.name} via {channel.channel_type} ({channel.name})"
                )
            except Exception as e:
                log.status = "failed"
                log.error_message = str(e)
                logger.error(
                    f"Notification failed for {container.name} via {channel.channel_type}: {e}"
                )
            finally:
                db.add(log)

        await db.commit()

    async def send_aggregated_notifications_for_host(
        self, db: AsyncSession, host: DockerHost, containers: list[TrackedContainer],
        check_time: Optional[datetime] = None,
    ):
        """Send a single combined notification for all updated containers on a host."""
        space_result = await db.execute(select(Space).where(Space.id == host.space_id))
        space = space_result.scalar_one_or_none()
        space_name = space.name if space else ""

        channels_result = await db.execute(
            select(NotificationChannel).where(
                NotificationChannel.space_id == host.space_id,
                NotificationChannel.is_active == True,  # noqa: E712
            )
        )
        channels = channels_result.scalars().all()
        if not channels:
            return

        for channel in channels:
            try:
                await notification_service.send_aggregated_update_notification(
                    channel, containers, settings.APP_URL,
                    host_name=host.name, space_name=space_name, check_time=check_time,
                )
                log_status = "sent"
                log_error = None
                logger.info(
                    f"Sent aggregated notification ({len(containers)} containers) via {channel.channel_type}"
                )
            except Exception as e:
                log_status = "failed"
                log_error = str(e)
                logger.error(
                    f"Aggregated notification failed via {channel.channel_type}: {e}"
                )
            for container in containers:
                log = NotificationLog(
                    container_id=container.id,
                    channel_id=channel.id,
                    old_tag=container.tag,
                    old_digest=container.digest,
                    new_tag=container.latest_tag,
                    new_digest=container.latest_digest,
                    release_date=container.release_date,
                    status=log_status,
                    error_message=log_error,
                )
                db.add(log)

        await db.commit()
