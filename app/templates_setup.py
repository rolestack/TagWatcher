import os
from datetime import datetime
from fastapi.templating import Jinja2Templates

_SYSTEM_TZ = os.environ.get("TZ", "UTC")


def _localtime(dt, user=None):
    """Jinja2 filter: convert UTC datetime to user's local timezone."""
    if dt is None:
        return "—"
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return dt.strftime("%Y-%m-%d %H:%M")

    tz_name = None
    if user is not None and hasattr(user, "timezone") and user.timezone:
        tz_name = user.timezone
    if not tz_name:
        tz_name = _SYSTEM_TZ

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        from zoneinfo import ZoneInfo as ZI
        tz = ZI("UTC")

    if dt.tzinfo is None:
        from zoneinfo import ZoneInfo as ZI
        dt = dt.replace(tzinfo=ZI("UTC"))

    local_dt = dt.astimezone(tz)
    tz_abbr = local_dt.strftime("%Z")  # e.g. KST, UTC, EST, EDT, GMT, BST
    return local_dt.strftime(f"%Y-%m-%d %H:%M {tz_abbr}")


def _format_audit_value(v) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def _fmt_audit_details(details_json: str | None, max_len: int = 80) -> str:
    """Jinja2 filter: render a JSON details string as readable key: value · … pairs."""
    if not details_json:
        return ""
    try:
        import json
        data = json.loads(details_json)
        if not isinstance(data, dict):
            return str(data)[:max_len]
        parts = [f"{k}: {_format_audit_value(v)}" for k, v in data.items()]
        result = " · ".join(parts)
        if len(result) > max_len:
            result = result[:max_len - 1] + "…"
        return result
    except Exception:
        return details_json[:max_len] if details_json else ""


templates = Jinja2Templates(directory="app/templates")
templates.env.filters["localtime"] = _localtime
templates.env.filters["fmt_audit_details"] = _fmt_audit_details
templates.env.globals["system_tz"] = _SYSTEM_TZ

# In-memory cache for site customization (populated at startup and on save).
# Single-process safe; multi-worker would see stale values until next save.
site_customization: dict[str, str] = {
    "custom_head_html": "",
    "footer_text": "",
    "custom_footer_html": "",
}
templates.env.globals["site"] = site_customization
