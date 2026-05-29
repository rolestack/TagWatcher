import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.settings import SystemSetting
from app.config import settings as env_settings

logger = logging.getLogger(__name__)

OIDC_ENABLED_KEY = "oidc_enabled"
OIDC_PROVIDER_URL_KEY = "oidc_provider_url"
OIDC_CLIENT_ID_KEY = "oidc_client_id"
OIDC_CLIENT_SECRET_KEY = "oidc_client_secret"
OIDC_SCOPES_KEY = "oidc_scopes"
LOCAL_LOGIN_ENABLED_KEY = "local_login_enabled"
BRAND_SSO_BUTTON_LABEL_KEY = "brand_sso_button_label"
BRAND_LOGIN_BG_URL_KEY = "brand_login_bg_url"
BRAND_FAVICON_URL_KEY = "brand_favicon_url"
SESSION_MAX_AGE_KEY = "session_max_age"
CUSTOM_HEAD_HTML_KEY = "custom_head_html"
FOOTER_TEXT_KEY = "footer_text"
CUSTOM_FOOTER_HTML_KEY = "custom_footer_html"
NOTIFICATION_RETENTION_DAYS_KEY = "notification_retention_days"

NOTIFICATION_RETENTION_DEFAULT = 90

class SettingsService:
    @staticmethod
    async def get(db: AsyncSession, key: str) -> Optional[str]:
        result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        row = result.scalar_one_or_none()
        return row.value if row else None

    @staticmethod
    async def set(db: AsyncSession, key: str, value: Optional[str]) -> None:
        result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        row = result.scalar_one_or_none()
        if row:
            row.value = value
        else:
            db.add(SystemSetting(key=key, value=value))
        await db.commit()

    @staticmethod
    async def set_many(db: AsyncSession, data: dict[str, Optional[str]]) -> None:
        for key, value in data.items():
            result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
            row = result.scalar_one_or_none()
            if row:
                row.value = value
            else:
                db.add(SystemSetting(key=key, value=value))
        await db.commit()

    @staticmethod
    async def get_oidc_config(db: AsyncSession) -> Optional[dict]:
        """
        Returns OIDC config dict if fully configured, else None.
        DB values take precedence over .env values.
        """
        provider_url = await SettingsService.get(db, OIDC_PROVIDER_URL_KEY) or env_settings.OIDC_PROVIDER_URL
        client_id = await SettingsService.get(db, OIDC_CLIENT_ID_KEY) or env_settings.OIDC_CLIENT_ID
        client_secret = await SettingsService.get(db, OIDC_CLIENT_SECRET_KEY) or env_settings.OIDC_CLIENT_SECRET
        scopes = await SettingsService.get(db, OIDC_SCOPES_KEY) or env_settings.OIDC_SCOPES
        enabled_raw = await SettingsService.get(db, OIDC_ENABLED_KEY)

        if not all([provider_url, client_id, client_secret]):
            return None

        enabled = enabled_raw != "false" if enabled_raw is not None else True

        return {
            "provider_url": provider_url.rstrip("/"),
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": scopes or "openid email profile",
            "enabled": enabled,
        }

    @staticmethod
    async def is_oidc_configured(db: AsyncSession) -> bool:
        cfg = await SettingsService.get_oidc_config(db)
        return cfg is not None and cfg.get("enabled", False)

    @staticmethod
    async def is_local_login_enabled(db: AsyncSession) -> bool:
        """Local login enabled state.
        env LOCAL_LOGIN_ENABLED=false acts as a hard override (beats DB).
        When env is true (default), the DB setting takes precedence."""
        if not env_settings.LOCAL_LOGIN_ENABLED:
            return False
        val = await SettingsService.get(db, LOCAL_LOGIN_ENABLED_KEY)
        if val is not None:
            return val != "false"
        return True

    @staticmethod
    async def get_session_max_age(db: AsyncSession) -> int:
        val = await SettingsService.get(db, SESSION_MAX_AGE_KEY)
        if val:
            try:
                return int(val)
            except ValueError:
                pass
        return env_settings.SESSION_MAX_AGE

    @staticmethod
    async def get_notification_retention_days(db: AsyncSession) -> int:
        val = await SettingsService.get(db, NOTIFICATION_RETENTION_DAYS_KEY)
        if val:
            try:
                days = int(val)
                if days > 0:
                    return days
            except ValueError:
                pass
        return NOTIFICATION_RETENTION_DEFAULT
