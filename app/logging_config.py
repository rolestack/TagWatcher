"""
File logging configuration for TagWatcher.

Reads logging.properties, sets up three rotating file handlers:
  - tagwatcher.log  — application logs (app.* loggers)
  - access.log      — HTTP access logs (uvicorn.access logger)
  - audit.log       — audit trail (tagwatcher.audit logger)

Call setup_file_logging() once at startup (and again after settings change).
"""

import re
import os
import logging
import logging.handlers
from pathlib import Path

_logger = logging.getLogger(__name__)

PROPS_PATH = os.environ.get("LOGGING_PROPERTIES_PATH", "logging.properties")

# Marker attribute: identifies handlers managed by this module so they can be
# cleanly removed on reload without disturbing any other handlers.
_TW_HANDLER = "_tw_managed"
_AUDIT_LOGGER_NAME = "tagwatcher.audit"

DEFAULTS: dict[str, str] = {
    "log.dir": "logs",
    "log.rotation.strategy": "date",
    "log.rotation.max_size_mb": "10",
    "log.rotation.backup_count": "7",
    "log.rotation.when": "midnight",
    "log.rotation.date_suffix": "%Y%m%d",
    "log.level.app": "INFO",
    "log.level.access": "INFO",
    "log.level.audit": "INFO",
    "log.file.app": "tagwatcher.log",
    "log.file.access": "access.log",
    "log.file.audit": "audit.log",
}

# ---------------------------------------------------------------------------
# Properties file I/O
# ---------------------------------------------------------------------------

def _write_defaults(path: str) -> None:
    """Write a default logging.properties file."""
    lines = [
        "# TagWatcher Logging Configuration",
        "# Mount this file as a volume to persist settings across container restarts.",
        "# Changes take effect on next container start.",
        "",
        "# --- General ---",
        f"log.dir={DEFAULTS['log.dir']}",
        "",
        "# --- Rotation ---",
        f"log.rotation.strategy={DEFAULTS['log.rotation.strategy']}",
        f"log.rotation.when={DEFAULTS['log.rotation.when']}",
        f"log.rotation.date_suffix={DEFAULTS['log.rotation.date_suffix']}",
        f"log.rotation.backup_count={DEFAULTS['log.rotation.backup_count']}",
        f"log.rotation.max_size_mb={DEFAULTS['log.rotation.max_size_mb']}",
        "",
        "# --- Log Levels ---",
        f"log.level.app={DEFAULTS['log.level.app']}",
        f"log.level.access={DEFAULTS['log.level.access']}",
        f"log.level.audit={DEFAULTS['log.level.audit']}",
        "",
        "# --- File Names ---",
        f"log.file.app={DEFAULTS['log.file.app']}",
        f"log.file.access={DEFAULTS['log.file.access']}",
        f"log.file.audit={DEFAULTS['log.file.audit']}",
        "",
    ]
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        _logger.info(f"Created default logging.properties at {path}")
    except Exception as exc:
        _logger.warning(f"Could not create logging.properties at {path}: {exc}")


def load_properties(path: str | None = None) -> dict[str, str]:
    """Parse logging.properties, returning a dict with defaults filled in.
    Creates the file with defaults if it does not exist."""
    props = dict(DEFAULTS)
    p = path or PROPS_PATH
    try:
        with open(p, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith("!"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    props[k.strip()] = v.strip()
    except FileNotFoundError:
        _write_defaults(p)
    return props



# ---------------------------------------------------------------------------
# Handler construction
# ---------------------------------------------------------------------------

def _remove_managed(log_obj: logging.Logger) -> None:
    for h in list(log_obj.handlers):
        if getattr(h, _TW_HANDLER, False):
            try:
                h.close()
            except Exception:
                pass
            log_obj.removeHandler(h)


def _make_handler(filepath: Path, props: dict[str, str]) -> logging.Handler:
    strategy = props.get("log.rotation.strategy", "date").lower()
    backup_count = int(props.get("log.rotation.backup_count", "7"))

    if strategy == "size":
        max_bytes = int(float(props.get("log.rotation.max_size_mb", "10")) * 1024 * 1024)
        h: logging.Handler = logging.handlers.RotatingFileHandler(
            filepath,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    else:  # date (default)
        when = props.get("log.rotation.when", "midnight")
        date_suffix = props.get("log.rotation.date_suffix", "%Y%m%d")
        th = logging.handlers.TimedRotatingFileHandler(
            filepath,
            when=when,
            backupCount=backup_count,
            encoding="utf-8",
        )
        th.suffix = date_suffix
        # Build a matching extMatch regex so Python's cleanup finds rotated files
        raw = re.sub(r"%[YmdHMSj]", r"\\d+", re.escape(date_suffix))
        try:
            th.extMatch = re.compile(r"^\." + raw + r"(\.\w+)?$")
        except Exception:
            pass
        h = th

    setattr(h, _TW_HANDLER, True)
    return h


# ---------------------------------------------------------------------------
# Public setup
# ---------------------------------------------------------------------------

def setup_file_logging(props: dict[str, str] | None = None) -> None:
    """Configure (or reconfigure) file logging. Safe to call multiple times."""
    if props is None:
        props = load_properties()

    # Always remove old managed handlers first so reload is clean
    for name in ("app", "uvicorn.access", _AUDIT_LOGGER_NAME):
        _remove_managed(logging.getLogger(name))

    log_dir = Path(props.get("log.dir", "logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        _logger.error(f"Cannot create log directory {log_dir}: {exc}")
        return

    def _attach(log_name: str, filename_key: str, level_key: str,
                fmt: logging.Formatter, propagate: bool = True) -> None:
        log_obj = logging.getLogger(log_name)
        log_obj.propagate = propagate
        filepath = log_dir / props.get(filename_key, filename_key)
        try:
            h = _make_handler(filepath, props)
            h.setFormatter(fmt)
            h.setLevel(getattr(logging, props.get(level_key, "INFO").upper(), logging.INFO))
            log_obj.addHandler(h)
        except Exception as exc:
            _logger.error(f"Cannot create log handler for {log_name} → {filepath}: {exc}")

    std_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    access_fmt = logging.Formatter(
        "%(asctime)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    audit_fmt = logging.Formatter(
        "%(asctime)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # tagwatcher.log — all app.* logs (propagate=True so console still works)
    _attach("app", "log.file.app", "log.level.app", std_fmt, propagate=True)

    # access.log — uvicorn HTTP access logs
    # propagate=False: uvicorn already has its own stdout handler on this logger;
    # overriding to True causes access records to also hit the root handler and
    # appear in tagwatcher.log via the root → app propagation chain.
    _attach("uvicorn.access", "log.file.access", "log.level.access", access_fmt, propagate=False)

    # audit.log — structured audit trail; propagate=False (not in tagwatcher.log)
    audit_log_obj = logging.getLogger(_AUDIT_LOGGER_NAME)
    audit_log_obj.setLevel(logging.INFO)
    _attach(_AUDIT_LOGGER_NAME, "log.file.audit", "log.level.audit", audit_fmt, propagate=False)


# ---------------------------------------------------------------------------
# Convenience reference used by audit_service
# ---------------------------------------------------------------------------
audit_logger = logging.getLogger(_AUDIT_LOGGER_NAME)
