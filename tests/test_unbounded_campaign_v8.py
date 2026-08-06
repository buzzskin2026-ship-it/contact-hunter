from __future__ import annotations

import uuid

from sqlalchemy import delete, select

from app.api.routes import _clone_search
from app.config import Settings
from app.db import SessionLocal, init_db
from app.models import CrawlTarget, SearchJob, TargetStatus
from app.schemas import SearchCreate
from app.services.campaign_engine import (
    TargetSpec,
    _enqueue_targets,
    _has_capacity,
    _reset_interrupted_targets,
)
from app.services.search_open_data import EuropeanOpenDataProvider
from app.services.search_osm import OpenStreetMapDentalProvider


def test_zero_means_no_global_contact_ceiling():
    payload = SearchCreate(sector="studi dentistici", max_results=0)
    assert payload.max_results == 0

    job = SearchJob(sector="studi dentistici", max_results=0, contacts_found=999_999)
    assert _has_capacity(job)


def test_broad_retry_switches_to_unbounded_mode():
    original = SearchJob(
        sector="studi dentistici",
        max_results=100,
        requested_fields=["email"],
        official_sources_only=True,
        exclude_free_email_providers=True,
    )
    cloned = _clone_search(original, broad=True)
    assert cloned.max_results == 0
    assert cloned.official_sources_only is False
    assert cloned.exclude_free_email_providers is False
    assert {"email", "phone", "whatsapp", "address"}.issubset(
        cloned.requested_fields
    )


def test_osm_zero_omits_the_output_record_limit():
    query = OpenStreetMapDentalProvider._query("IT", None, timeout=180)
    assert "out tags center;" in query
    assert "out tags center 5000;" not in query


def test_open_data_zero_does_not_report_capacity_reached():
    provider = EuropeanOpenDataProvider(
        Settings(
            open_data_max_queries=0,
            open_data_max_resources=0,
            playwright_enabled=False,
        )
    )
    assert not provider._resource_capacity_reached(10_000_000)


def test_persistent_queue_deduplicates_and_recovers_running_targets():
    init_db()
    db = SessionLocal()
    job_id = str(uuid.uuid4())
    try:
        job = SearchJob(
            id=job_id,
            sector="studi dentistici",
            max_results=0,
        )
        db.add(job)
        db.commit()

        specs = [
            TargetSpec("crawl", "https://example.test/contact", "example.test"),
            TargetSpec("crawl", "https://example.test/contact", "example.test"),
            TargetSpec(
                "resolve",
                "resolve://test-business",
                "",
                {"organization": "Studio Test", "country": "Italia"},
            ),
        ]
        assert _enqueue_targets(db, job_id, specs) == 2
        assert _enqueue_targets(db, job_id, specs) == 0

        targets = list(
            db.scalars(
                select(CrawlTarget)
                .where(CrawlTarget.job_id == job_id)
                .order_by(CrawlTarget.id)
            )
        )
        assert len(targets) == 2
        targets[0].status = TargetStatus.running.value
        db.commit()

        _reset_interrupted_targets(db, job_id)
        db.refresh(targets[0])
        assert targets[0].status == TargetStatus.queued.value
        assert targets[0].last_error
    finally:
        db.execute(delete(CrawlTarget).where(CrawlTarget.job_id == job_id))
        db.execute(delete(SearchJob).where(SearchJob.id == job_id))
        db.commit()
        db.close()
