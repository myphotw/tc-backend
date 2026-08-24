from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    VERSION: str = "1.0.0"

    POSTGRES_HOST: str
    POSTGRES_PORT: int
    POSTGRES_DB: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    MASTER_KEY: str
    TC_BACKEND_AUTH_TOKEN: str | None = None

    PHOTO_PLATFORM_ROOT: str = "./PhotoPlatform"
    INCOMING_DIR: str | None = None
    ORIGINAL_DIR: str | None = None
    PREVIEW_DIR: str | None = None
    THUMB_DIR: str | None = None
    EXPORT_DIR: str | None = None
    CACHE_DIR: str | None = None
    TEMP_DIR: str | None = None

    GOOGLE_API_KEY: str | None = None
    # Compatibility alias used by some docs / MemoryKeeper naming.
    GOOGLE_MAP_API_KEY: str | None = None
    GOOGLE_VISION_CREDENTIAL: str | None = None
    WEATHER_API_KEY: str | None = None
    ASTROMETRY_API_KEY: str | None = None
    API_CLIENT_TIMEOUT: int = 30
    API_CLIENT_RETRY_COUNT: int = 3

    VISION_MONTHLY_LIMIT: int = 900
    GEOCODING_MONTHLY_LIMIT: int = 100000
    WEATHER_MONTHLY_LIMIT: int = 100000
    PLATESOLVE_MONTHLY_LIMIT: int = 100000

    @model_validator(mode="after")
    def resolve_storage_dirs(self) -> "Settings":
        """PHOTO_PLATFORM_ROOT 기준으로 Storage 하위 경로를 채운다."""
        if (
            self.TC_BACKEND_AUTH_TOKEN is not None
            and not self.TC_BACKEND_AUTH_TOKEN.strip()
        ):
            self.TC_BACKEND_AUTH_TOKEN = None
        if not self.GOOGLE_API_KEY and self.GOOGLE_MAP_API_KEY:
            self.GOOGLE_API_KEY = self.GOOGLE_MAP_API_KEY

        root = self.PHOTO_PLATFORM_ROOT.rstrip("/\\")
        defaults = {
            "INCOMING_DIR": f"{root}/incoming",
            "ORIGINAL_DIR": f"{root}/original",
            "PREVIEW_DIR": f"{root}/preview",
            "THUMB_DIR": f"{root}/thumb",
            "EXPORT_DIR": f"{root}/export",
            "CACHE_DIR": f"{root}/cache",
            "TEMP_DIR": f"{root}/temp",
        }
        for field_name, default_value in defaults.items():
            current = getattr(self, field_name)
            if not current:
                setattr(self, field_name, default_value)
            else:
                setattr(
                    self,
                    field_name,
                    str(current).replace("${PHOTO_PLATFORM_ROOT}", root),
                )
        return self

    @property
    def photo_platform_root_path(self) -> Path:
        return Path(self.PHOTO_PLATFORM_ROOT)

    @property
    def incoming_dir_path(self) -> Path:
        return Path(self.INCOMING_DIR)

    @property
    def original_dir_path(self) -> Path:
        return Path(self.ORIGINAL_DIR)

    @property
    def preview_dir_path(self) -> Path:
        return Path(self.PREVIEW_DIR)

    @property
    def thumb_dir_path(self) -> Path:
        return Path(self.THUMB_DIR)

    @property
    def export_dir_path(self) -> Path:
        return Path(self.EXPORT_DIR)

    @property
    def cache_dir_path(self) -> Path:
        return Path(self.CACHE_DIR)

    @property
    def temp_dir_path(self) -> Path:
        return Path(self.TEMP_DIR)


settings = Settings()
