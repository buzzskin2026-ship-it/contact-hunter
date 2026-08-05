from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl, field_validator


class SearchCreate(BaseModel):
    sector: str = Field(min_length=2, max_length=160)
    countries: list[str] = Field(default_factory=list, max_length=30)
    cities: list[str] = Field(default_factory=list, max_length=100)
    keywords: list[str] = Field(default_factory=list, max_length=30)
    seed_urls: list[HttpUrl] = Field(default_factory=list, max_length=500)
    requested_fields: list[str] = Field(default_factory=lambda: ["email", "phone"])
    max_results: int = Field(default=100, ge=1, le=10_000)
    official_sources_only: bool = True
    exclude_free_email_providers: bool = False

    @field_validator("sector")
    @classmethod
    def clean_sector(cls, value: str) -> str:
        return " ".join(value.split())


class SearchRead(BaseModel):
    id: str
    sector: str
    countries: list[str]
    cities: list[str]
    requested_fields: list[str]
    max_results: int
    status: str
    discovered_urls: int
    crawled_pages: int
    contacts_found: int
    duplicates_skipped: int
    blocked_urls: int
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class ContactRead(BaseModel):
    id: int
    organization: str
    category: str | None
    country: str | None
    city: str | None
    address: str | None
    website: str
    domain: str
    emails: list[str]
    phones: list[str]
    whatsapp: list[str]
    specialties: list[str]
    source_url: str
    reliability: str
    verified_at: datetime

    model_config = {"from_attributes": True}
