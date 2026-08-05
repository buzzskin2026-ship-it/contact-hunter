from __future__ import annotations

from app.db import Base, SessionLocal, engine
from app.models import SearchJob
from app.services.jobs import submit_job


Base.metadata.create_all(bind=engine)
with SessionLocal() as db:
    job = SearchJob(
        sector="studi dentistici",
        countries=["Italy"],
        cities=["Rome"],
        seed_urls=["https://example.org/"],
        max_results=10,
    )
    db.add(job)
    db.commit()
    print(f"Ricerca creata: {job.id}")
    submit_job(job.id)
