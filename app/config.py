from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Application
    APP_NAME: str = "TagWatcher"
    APP_URL: str = "http://localhost:8000"
    SECRET_KEY: str = "change-me-in-production-use-long-random-string"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # Database — optional here; configured through the web setup wizard and
    # stored in /app/data/tagwatcher.json. Setting this env var skips the
    # database step in the setup wizard (useful for scripted deployments).
    DATABASE_URL: Optional[str] = None

    # OIDC / SSO — optional here; overridden by DB settings configured in the admin panel
    OIDC_PROVIDER_URL: Optional[str] = None
    OIDC_CLIENT_ID: Optional[str] = None
    OIDC_CLIENT_SECRET: Optional[str] = None
    OIDC_SCOPES: str = "openid email profile"

    # Auth — can be overridden by DB settings, but env acts as a hard fallback
    # Set LOCAL_LOGIN_ENABLED=false in .env to force-disable local login at the infrastructure level
    LOCAL_LOGIN_ENABLED: bool = True

    # Scheduler
    CHECK_INTERVAL_MINUTES: int = 60

    # Proxy
    BEHIND_PROXY: bool = False

    # Session cookie settings
    SESSION_COOKIE_NAME: str = "tagwatcher_session"
    SESSION_MAX_AGE: int = 3600 * 8  # 8 hours

    # Workers (for gunicorn)
    WORKERS: int = 2
    WORKER_CLASS: str = "uvicorn.workers.UvicornWorker"
    BIND: str = "0.0.0.0:8000"
    TIMEOUT: int = 120
    KEEPALIVE: int = 5

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_database_url(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection string")
        return v


settings = Settings()
