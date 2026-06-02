import asyncio
import logging
from typing import Optional, AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import MetaData, text

logger = logging.getLogger(__name__)

convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=convention)


class Base(DeclarativeBase):
    metadata = metadata


# ---------------------------------------------------------------------------
# Lazy engine — initialized via initialize() after DB config is known
# ---------------------------------------------------------------------------

_engine: Optional[AsyncEngine] = None
_session_maker: Optional[async_sessionmaker] = None
_init_lock = asyncio.Lock()


def is_initialized() -> bool:
    # Check both in-memory engine AND config file for multi-worker safety
    if _engine is not None:
        return True
    # If engine is None, check if config file exists (another worker may have initialized)
    from app.config_file import get_database_url
    try:
        url = get_database_url()
        return url is not None
    except Exception:
        return False


def get_session_maker() -> async_sessionmaker:
    if _session_maker is None:
        raise RuntimeError(
            "Database is not initialized. Complete the setup wizard first."
        )
    return _session_maker


async def get_or_init_session_maker() -> async_sessionmaker:
    """Get session maker, initializing if needed (for multiworker scenarios)."""
    try:
        return get_session_maker()
    except RuntimeError:
        # Another worker configured DB but this worker hasn't loaded it yet
        from app.config_file import get_database_url
        url = get_database_url()
        await initialize(url)
        return get_session_maker()


async def initialize(database_url: str, debug: bool = False) -> None:
    """Create (or recreate) the async engine and session maker."""
    global _engine, _session_maker
    async with _init_lock:
        if _engine is not None:
            logger.info("Disposing existing database engine...")
            await _engine.dispose()
        _engine = create_async_engine(
            database_url,
            echo=debug,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
        )
        _session_maker = async_sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
        logger.info("Database engine initialized.")


async def test_connection(database_url: str) -> None:
    """Verify a DB URL is reachable without touching global state. Raises on failure."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def init_tables() -> None:
    """Create all tables that are not yet present. Used during initial setup."""
    if _engine is None:
        raise RuntimeError("Database not initialized.")
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured.")


async def run_migrations() -> None:
    """Apply incremental schema changes that create_all won't handle on existing tables."""
    if _engine is None:
        raise RuntimeError("Database not initialized.")
    async with _engine.begin() as conn:
        for stmt in [
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS last_sync_error TEXT",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS auto_check_updates BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS check_interval_minutes INTEGER",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS version_strategy VARCHAR(20) NOT NULL DEFAULT 'auto'",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS exclude_patterns TEXT",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS last_update_check_at TIMESTAMP WITH TIME ZONE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS timezone VARCHAR(64)",
            "ALTER TABLE tracked_containers ADD COLUMN IF NOT EXISTS version_strategy_override VARCHAR(20)",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS check_schedule_time TEXT",
            "ALTER TABLE docker_hosts ALTER COLUMN check_schedule_time TYPE TEXT",
            """CREATE TABLE IF NOT EXISTS audit_logs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                user_email VARCHAR(320),
                user_name VARCHAR(256),
                action VARCHAR(64) NOT NULL,
                resource_type VARCHAR(64),
                resource_id VARCHAR(128),
                resource_name VARCHAR(512),
                details TEXT,
                ip_address VARCHAR(64),
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
            )""",
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_user_id ON audit_logs (user_id)",
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_action ON audit_logs (action)",
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_resource_type ON audit_logs (resource_type)",
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at)",
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_user_created ON audit_logs (user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_resource ON audit_logs (resource_type, resource_id)",
            "CREATE INDEX IF NOT EXISTS ix_tracked_containers_host_name ON tracked_containers (docker_host_id, name)",
            "ALTER TABLE tracked_containers ADD COLUMN IF NOT EXISTS version_pattern VARCHAR(200)",
            "ALTER TABLE tracked_containers ALTER COLUMN version_strategy_override TYPE VARCHAR(32)",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS version_pattern VARCHAR(200)",
            "ALTER TABLE docker_hosts ALTER COLUMN version_strategy TYPE VARCHAR(32)",
            # Rename strategy values to SemVer terminology
            "UPDATE docker_hosts SET version_strategy = 'major' WHERE version_strategy = 'unrestricted'",
            "UPDATE docker_hosts SET version_strategy = 'minor' WHERE version_strategy = 'same_major'",
            "UPDATE docker_hosts SET version_strategy = 'patch' WHERE version_strategy = 'same_minor'",
            "UPDATE tracked_containers SET version_strategy_override = 'major' WHERE version_strategy_override = 'unrestricted'",
            "UPDATE tracked_containers SET version_strategy_override = 'minor' WHERE version_strategy_override = 'same_major'",
            "UPDATE tracked_containers SET version_strategy_override = 'patch' WHERE version_strategy_override = 'same_minor'",
            "ALTER TYPE channeltype ADD VALUE IF NOT EXISTS 'teams'",
            "ALTER TABLE spaces ADD COLUMN IF NOT EXISTS icon VARCHAR(16)",
            "ALTER TABLE tracked_containers ADD COLUMN IF NOT EXISTS snoozed_until TIMESTAMP WITH TIME ZONE",
            "ALTER TABLE notification_logs ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMP WITH TIME ZONE",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS notification_snooze_hours INTEGER NOT NULL DEFAULT 24",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS host_type VARCHAR(32) NOT NULL DEFAULT 'tcp'",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS agent_registration_token VARCHAR(128)",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS agent_registration_token_expires_at TIMESTAMP WITH TIME ZONE",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS agent_secret VARCHAR(128)",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS agent_allowed_cidrs TEXT",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS runtime_type VARCHAR(32)",
            "ALTER TABLE docker_hosts ADD COLUMN IF NOT EXISTS runtime_metadata TEXT",
            "UPDATE docker_hosts SET host_type = 'unix' WHERE host_url LIKE 'unix://%' AND host_type = 'tcp'",
        ]:
            await conn.execute(text(stmt))
    logger.info("Schema migrations applied.")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an AsyncSession."""
    maker = get_session_maker()
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
