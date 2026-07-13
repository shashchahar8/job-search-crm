from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "local"
    database_url: str = "sqlite:///data/job_search_crm.sqlite3"
    data_dir: Path = Path("data")
    playwright_profile_dir: Path = Path("data/browser-profiles/seek")
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


@lru_cache
def get_settings() -> Settings:
    return Settings()

