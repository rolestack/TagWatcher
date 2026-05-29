import re
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

from app.database import Base
from app.models import (  # noqa: F401
    User,
    user_groups,
    Group,
    group_spaces,
    Space,
    DockerHost,
    TrackedContainer,
    NotificationChannel,
    NotificationLog,
    SystemSetting,
)
from app.config_file import get_database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_sync_url() -> str:
    url = get_database_url()
    if not url:
        raise RuntimeError(
            "No database URL found. Either complete the setup wizard or set DATABASE_URL."
        )
    url = url.replace("+asyncpg", "+psycopg2")
    # asyncpg uses ?ssl=...; psycopg2 doesn't accept it — strip it
    url = re.sub(r"[?&]ssl=[^&]*", "", url).rstrip("?")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_get_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _get_sync_url()

    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
