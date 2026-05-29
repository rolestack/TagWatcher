import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import Request

logger = logging.getLogger(__name__)

# All known action values — used to populate the dropdown in the UI
KNOWN_ACTIONS = [
    "user.login",
    "user.login_failed",
    "user.logout",
    "user.create",
    "user.update",
    "user.delete",
    "account.update_profile",
    "account.update_timezone",
    "account.change_password",
    "group.create",
    "group.delete",
    "group.add_user",
    "group.remove_user",
    "group.add_space",
    "group.remove_space",
    "space.create",
    "space.delete",
    "host.create",
    "host.update",
    "host.delete",
    "host.sync",
    "host.check",
    "host.check_all",
    "container.strategy_update",
    "container.check",
    "notification_channel.create",
    "notification_channel.update",
    "notification_channel.delete",
    "notification_channel.test",
    "settings.update",
]


def _get_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _write_audit_file_log(
    action: str, user_email, user_name, resource_type, resource_id,
    resource_name, details, resolved_ip
) -> None:
    try:
        from app.logging_config import audit_logger
        who = f"{user_name} <{user_email}>" if user_email else (user_name or "system")
        res = "/".join(filter(None, [resource_type, resource_name or (str(resource_id) if resource_id else None)]))
        detail_str = json.dumps(details, default=str) if details else ""
        parts = [f"[{action}]", f"by {who}", res or "-", resolved_ip or "-"]
        if detail_str:
            parts.append(detail_str)
        audit_logger.info(" | ".join(parts))
    except Exception:
        pass


async def audit(
    _db_unused,  # kept for call-site compatibility but NOT used
    action: str,
    *,
    user=None,
    resource_type: str | None = None,
    resource_id=None,
    resource_name: str | None = None,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
    ip: str | None = None,
) -> None:
    """Write a single audit log entry using its own session. Never raises."""
    from app.models.audit_log import AuditLog
    from app import database

    if not database.is_initialized():
        return

    resolved_ip = ip or _get_ip(request)
    user_email = getattr(user, "email", None) or getattr(user, "username", None)
    user_name = getattr(user, "name", None) or getattr(user, "display_name", None)

    try:
        maker = database.get_session_maker()
        async with maker() as session:
            entry = AuditLog(
                user_id=user.id if user else None,
                user_email=user_email,
                user_name=user_name,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id is not None else None,
                resource_name=resource_name,
                details=json.dumps(details, default=str) if details else None,
                ip_address=resolved_ip,
                created_at=datetime.now(timezone.utc),
            )
            session.add(entry)
            await session.commit()
    except Exception as e:
        logger.warning(f"audit({action!r}) failed: {e}")

    _write_audit_file_log(action, user_email, user_name, resource_type, resource_id,
                          resource_name, details, resolved_ip)
