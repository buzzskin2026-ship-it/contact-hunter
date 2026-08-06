from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.models import JobStatus, SearchJob
from app.services.jobs import submit_job


def resume_pending_jobs(limit: int = 5) -> int:
    """Requeue jobs left incomplete by a process restart.

    The application currently runs one web instance, so startup is the safe point
    to recover queued/running campaigns. Database uniqueness keeps repeated
    contacts from being inserted if discovery has to start again.
    """
    db = SessionLocal()
    try:
        jobs = list(
            db.scalars(
                select(SearchJob)
                .where(SearchJob.status.in_((JobStatus.queued.value, JobStatus.running.value)))
                .order_by(SearchJob.created_at)
                .limit(limit)
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
