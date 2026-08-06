from __future__ import annotations

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import JobStatus, SearchJob
from app.services.campaign_engine import submit_job


def resume_pending_jobs(limit: int | None = None) -> int:
    """Requeue incomplete campaigns after a process restart.

    Crawl targets are stored in PostgreSQL, so the campaign engine resumes queued
    work instead of rebuilding one giant in-memory task list.
    """
    settings = get_settings()
    effective_limit = limit or settings.campaign_recovery_jobs
    db = SessionLocal()
    try:
        jobs = list(
            db.scalars(
                select(SearchJob)
                .where(
                    SearchJob.status.in_(
                        (
                            JobStatus.queued.value,
                            JobStatus.running.value,
                        )
                    )
                )
                .order_by(SearchJob.created_at)
                .limit(effective_limit)
            )
        )
        ids = [job.id for job in jobs]
        for job in jobs:
            job.status = JobStatus.queued.value
            job.error_message = None
        db.commit()
    finally:
        db.close()

    for job_id in ids:
        submit_job(job_id)
    return len(ids)
