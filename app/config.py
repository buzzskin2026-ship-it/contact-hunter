from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Contact Hunter"
    environment: str = "development"
    secret_key: str = "change-me-in-production"
    admin_username: str = "admin"
    admin_password: str = "change-me"
    auth_enabled: bool = True

    database_url: str = "sqlite:///./data/contact_hunter.db"

    # Persistent campaign engine. Batch sizes control memory and database pressure;
    # they never cap the total number of contacts or URLs in a campaign.
    campaign_queue_batch_size: int = Field(default=100, ge=10, le=2_000)
    campaign_max_attempts: int = Field(default=2, ge=1, le=10)
    campaign_recovery_jobs: int = Field(default=25, ge=1, le=500)
    campaign_discovery_batch_size: int = Field(default=500, ge=50, le=10_000)

    brave_search_api_key: str | None = None
    search_result_limit: int = Field(default=500, ge=1, le=500)
    public_search_fallback_enabled: bool = True
    # Zero means all generated queries; it is not a total campaign limit.
    public_search_max_queries: int = Field(default=0, ge=0, le=10_000)
    public_search_delay_seconds: float = Field(default=0.8, ge=0.0, le=10.0)

    open_data_enabled: bool = True
    # Zero means all generated queries/resources available from the provider.
    open_data_max_queries: int = Field(default=0, ge=0, le=10_000)
    open_data_datasets_per_query: int = Field(default=1_000, ge=1, le=10_000)
    open_data_max_resources: int = Field(default=0, ge=0, le=1_000_000)
    open_data_delay_seconds: float = Field(default=0.1, ge=0.0, le=5.0)

    # Zero omits the Overpass output limit. External servers may still enforce
    # their own time and resource policies.
    osm_max_records_per_country: int = Field(default=0, ge=0, le=1_000_000)
    osm_timeout_seconds: float = Field(default=180, ge=30, le=600)

    common_crawl_enabled: bool = True
    # Zero means every discovered domain. URLs per domain remain bounded because
    # a single site must never monopolise a worldwide campaign.
    common_crawl_max_domains: int = Field(default=0, ge=0, le=1_000_000)
    common_crawl_urls_per_domain: int = Field(default=25, ge=1, le=200)
    common_crawl_delay_seconds: float = Field(default=0.15, ge=0.0, le=5.0)

    crawler_user_agent: str = "ContactHunter/0.8 (+https://contact-hunter.onrender.com)"
    crawler_contact_email: str | None = None
    crawler_delay_seconds: float = Field(default=0.35, ge=0.2, le=30)
    crawler_timeout_seconds: float = Field(default=15, ge=5, le=120)
    crawler_page_timeout_seconds: float = Field(default=12, ge=3, le=90)
    crawler_domain_timeout_seconds: float = Field(default=45, ge=10, le=300)
    crawler_sitemap_timeout_seconds: float = Field(default=6, ge=1, le=60)
    crawler_playwright_timeout_seconds: float = Field(default=12, ge=3, le=90)
    crawler_quick_pages_per_domain: int = Field(default=6, ge=1, le=20)
    crawler_max_pages_per_domain: int = Field(default=16, ge=1, le=60)
    crawler_max_response_bytes: int = Field(
        default=20_000_000,
        ge=100_000,
        le=50_000_000,
    )
    crawler_concurrency: int = Field(default=8, ge=1, le=24)
    sitemap_enabled: bool = True
    sitemap_max_urls_per_domain: int = Field(default=25, ge=0, le=200)
    pdf_enabled: bool = True
    pdf_max_pages: int = Field(default=100, ge=1, le=500)
    structured_documents_enabled: bool = True
    structured_document_max_rows: int = Field(
        default=100_000,
        ge=100,
        le=1_000_000,
    )
    respect_robots_txt: bool = True
    playwright_enabled: bool = True
    keep_page_snapshots: bool = False

    blocked_domains: str = (
        "facebook.com,instagram.com,linkedin.com,youtube.com,tiktok.com,"
        "x.com,twitter.com"
    )
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
        return ",".join(
            sorted({part.strip().lower() for part in value.split(",") if part.strip()})
        )

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
