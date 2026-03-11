"""PostgreSQL-backed settings store with env fallback."""
import os
import logging

logger = logging.getLogger(__name__)


class SettingsStore:
    def __init__(self, db_url: str | None = None):
        self._conn = None
        if db_url is None:
            db_url = os.environ.get("DATABASE_URL", "")
            # asyncpg DSN → psycopg2 DSN
            if db_url.startswith("postgresql+asyncpg://"):
                db_url = db_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        if db_url:
            try:
                import psycopg2
                self._conn = psycopg2.connect(db_url)
                self._conn.autocommit = True
                logger.info("SettingsStore: connected to PostgreSQL")
            except Exception as e:
                logger.warning(f"SettingsStore: DB connect failed ({e}), falling back to env")
                self._conn = None

    @property
    def available(self) -> bool:
        if self._conn is None:
            return False
        try:
            self._conn.cursor().execute("SELECT 1")
            return True
        except Exception:
            return False

    def get(self, key: str, default: str = "") -> str:
        if self.available:
            try:
                cur = self._conn.cursor()
                cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
                row = cur.fetchone()
                if row is not None:
                    return row[0] or default
            except Exception as e:
                logger.warning(f"SettingsStore.get({key}): {e}")
        return os.environ.get(key, default)

    def get_all(self) -> dict[str, str]:
        if self.available:
            try:
                cur = self._conn.cursor()
                cur.execute("SELECT key, value FROM app_settings")
                return {row[0]: row[1] or "" for row in cur.fetchall()}
            except Exception as e:
                logger.warning(f"SettingsStore.get_all: {e}")
        # Fallback: return known keys from env
        keys = [
            "TELEGRAM_TOKEN", "TELEGRAM_CHAT_IMPORTANT", "TELEGRAM_CHAT_NONIMPORTANT",
            "RTSP_URL", "CAMERA_MAC", "CAMERA_IP_SUBNET", "OCR_SOURCE", "LINE_Y_RATIO",
            "CAMERA_2_URL", "CAMERA_3_URL", "CAMERA_4_URL",
            "CAMERA_UI_USER", "CAMERA_UI_PASS",
            "IMOU_OPEN_APP_ID", "IMOU_OPEN_APP_SECRET", "IMOU_OPEN_DEVICE_ID",
        ]
        return {k: os.environ.get(k, "") for k in keys}

    def set_many(self, data: dict[str, str]) -> bool:
        """Update settings. Skips keys with empty values."""
        filtered = {k: v for k, v in data.items() if v}
        if not filtered:
            return True
        if not self.available:
            logger.warning("SettingsStore.set_many: DB not available")
            return False
        try:
            cur = self._conn.cursor()
            for key, value in filtered.items():
                cur.execute(
                    """INSERT INTO app_settings (key, value, updated_at)
                       VALUES (%s, %s, NOW())
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                    (key, value),
                )
            return True
        except Exception as e:
            logger.error(f"SettingsStore.set_many: {e}")
            return False
