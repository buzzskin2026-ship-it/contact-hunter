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
    search_result_limit: int = Field(default=160, ge=1, le=500)
    public_search_fallback_enabled: bool = True
    public_search_max_queries: int = Field(default=60, ge=1, le=200)
    public_search_delay_seconds: float = Field(default=0.8, ge=0.0, le=10.0)

    google_places_api_key: str | None = None
    google_places_enabled: bool = True
    google_places_max_queries: int = Field(default=700, ge=1, le=2_000)
    google_places_max_pages_per_query: int = Field(default=3, ge=1, le=3)
    google_places_max_places: int = Field(default=20_000, ge=1, le=100_000)
    google_places_concurrency: int = Field(default=5, ge=1, le=20)
    google_places_delay_seconds: float = Field(default=0.15, ge=0.0, le=5.0)

    osm_max_records_per_country: int = Field(default=5_000, ge=100, le=50_000)
    osm_timeout_seconds: float = Field(default=120, ge=30, le=300)

    common_crawl_enabled: bool = True
    common_crawl_max_domains: int = Field(default=200, ge=0, le=2_000)
    common_crawl_urls_per_domain: int = Field(default=10, ge=1, le=50)
    common_crawl_delay_seconds: float = Field(default=0.15, ge=0.0, le=5.0)

    crawler_user_agent: str = "ContactHunter/0.6 (+https://contact-hunter.onrender.com)"
    crawler_contact_email: str | None = None
    crawler_delay_seconds: float = Field(default=1.0, ge=0.2, le=30)
    crawler_timeout_seconds: float = Field(default=30, ge=5, le=120)
    crawler_max_pages_per_domain: int = Field(default=16, ge=1, le=60)
    crawler_max_response_bytes: int = Field(default=12_000_000, ge=100_000, le=30_000_000)
    crawler_concurrency: int = Field(default=5, ge=1, le=24)
    sitemap_enabled: bool = True
    sitemap_max_urls_per_domain: int = Field(default=25, ge=0, le=200)
    pdf_enabled: bool = True
    pdf_max_pages: int = Field(default=100, ge=1, le=500)
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
