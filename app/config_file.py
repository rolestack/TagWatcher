"""
Manages the persistent config file at /app/data/tagwatcher.json.

This file stores settings that cannot be kept in the database (like the
database URL itself), and is initialized through the web setup wizard.
The directory must be mounted as a Docker volume to survive container restarts.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Override via TAGWATCHER_CONFIG_FILE env var for testing / custom paths
_DEFAULT_CONFIG_PATH = "/app/data/tagwatcher.json"
_ENV_KEY = "TAGWATCHER_CONFIG_FILE"


def get_config_path() -> Path:
    return Path(os.environ.get(_ENV_KEY, _DEFAULT_CONFIG_PATH))


def _load() -> dict:
    path = get_config_path()
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as exc:
        logger.warning(f"Could not read config file {path}: {exc}")
        return {}


def _save(data: dict) -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Config saved to {path}")


def get_database_url() -> Optional[str]:
    """
    Returns the database URL in priority order:
      1. Config file  (/app/data/tagwatcher.json)
      2. DATABASE_URL environment variable
    Returns None if neither is set (triggers setup wizard DB step).
    """
    url = _load().get("database_url")
    if url:
        return url
    return os.environ.get("DATABASE_URL") or None


def save_database_url(url: str) -> None:
    data = _load()
    data["database_url"] = url
    _save(data)


def is_db_configured() -> bool:
    return get_database_url() is not None
