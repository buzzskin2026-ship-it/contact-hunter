from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class TargetStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class SearchJob(Base):
    __tablename__ = "search_jobs"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    sector: Mapped[str] = mapped_column(String(160), index=True)
    countries: Mapped[list[str]] = mapped_column(JSON, default=list)
    cities: Mapped[list[str]] = mapped_column(JSON, default=list)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    seed_urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    requested_fields: Mapped[list[str]] = mapped_column(
        JSON,
        default=lambda: ["email", "phone"],
    )
    # Zero means that the campaign has no global contact ceiling.
    max_results: Mapped[int] = mapped_column(Integer, default=0)
    official_sources_only: Mapped[bool] = mapped_column(Boolean, default=True)
    exclude_free_email_providers: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[str] = mapped_column(
        String(20),
        default=JobStatus.queued.value,
        index=True,
    )
    discovered_urls: Mapped[int] = mapped_column(Integer, default=0)
    crawled_pages: Mapped[int] = mapped_column(Integer, default=0)
    contacts_found: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_skipped: Mapped[int] = mapped_column(Integer, default=0)
    blocked_urls: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )
    logs: Mapped[list["CrawlLog"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )
    targets: Mapped[list["CrawlTarget"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_contacts_fingerprint"),
        Index("ix_contacts_country_city", "country", "city"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("search_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    organization: Mapped[str] = mapped_column(String(300), index=True)
    category: Mapped[str | None] = mapped_column(String(200), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    city: Mapped[str | None] = mapped_column(String(150), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    emails: Mapped[list[str]] = mapped_column(JSON, default=list)
    phones: Mapped[list[str]] = mapped_column(JSON, default=list)
    whatsapp: Mapped[list[str]] = mapped_column(JSON, default=list)
    specialties: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_url: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(
        String(80),
        default="official_website",
    )
    reliability: Mapped[str] = mapped_column(String(20), default="medium", index=True)
    status: Mapped[str] = mapped_column(String(30), default="verified_public")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )

    job: Mapped[SearchJob | None] = relationship(back_populates="contacts")


class CrawlTarget(Base):
    __tablename__ = "crawl_targets"
    __table_args__ = (
        UniqueConstraint("job_id", "url_hash", name="uq_crawl_targets_job_url"),
        Index("ix_crawl_targets_job_status", "job_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("search_jobs.id", ondelete="CASCADE"),
        index=True,
    )
    # crawl = fetch a public URL; resolve = search the public web by business name.
    kind: Mapped[str] = mapped_column(String(20), default="crawl", index=True)
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64))
    domain: Mapped[str] = mapped_column(String(255), index=True)
    hint: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(
        String(20),
        default=TargetStatus.queued.value,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    job: Mapped[SearchJob] = relationship(back_populates="targets")


class CrawlLog(Base):
    __tablename__ = "crawl_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("search_jobs.id", ondelete="CASCADE"),
        index=True,
    )
    url: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[SearchJob] = relationship(back_populates="logs")
