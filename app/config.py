from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Contact Hunter"
    environment: str = "development"
    secret_key: str = "change-me-in-production"
    admin_username: str = "admin"
    admin_password: str = "change-me"
    auth_enabled: bool = True

    database_url: str = "sqlite:///./data/contact_hunter.db"
    brave_search_api_key: str | None = None
    search_result_limit: int = Field(default=40, ge=1, le=200)
    public_search_fallback_enabled: bool = True
    public_search_max_queries: int = Field(default=12, ge=1, le=30)
    public_search_delay_seconds: float = Field(default=1.0, ge=0.0, le=10.0)

    crawler_user_agent: str = "ContactHunter/0.1 (+https://example.com/bot)"
    crawler_contact_email: str | None = None
    crawler_delay_seconds: float = Field(default=1.2, ge=0.2, le=30)
    crawler_timeout_seconds: float = Field(default=20, ge=5, le=120)
    crawler_max_pages_per_domain: int = Field(default=6, ge=1, le=30)
    crawler_max_response_bytes: int = Field(default=2_000_000, ge=100_000, le=10_000_000)
    crawler_concurrency: int = Field(default=4, ge=1, le=16)
    respect_robots_txt: bool = True
    playwright_enabled: bool = True
    keep_page_snapshots: bool = False

    blocked_domains: str = "facebook.com,instagram.com,linkedin.com,youtube.com,tiktok.com,x.com,twitter.com"
    data_directory: Path = Path("data")
    export_directory: Path = Path("data/exports")
    snapshot_directory: Path = Path("data/snapshots")

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        if isinstance(value, str) and value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value

    @field_validator("blocked_domains")
    @classmethod
    def normalize_domains(cls, value: str) -> str:
        return ",".join(sorted({part.strip().lower() for part in value.split(",") if part.strip()}))

    @property
    def blocked_domain_set(self) -> set[str]:
        return {part for part in self.blocked_domains.split(",") if part}

    def ensure_directories(self) -> None:
        self.data_directory.mkdir(parents=True, exist_ok=True)
        self.export_directory.mkdir(parents=True, exist_ok=True)
        self.snapshot_directory.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
