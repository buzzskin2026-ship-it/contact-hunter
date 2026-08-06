from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Contact, CrawlLog, CrawlTarget, SearchJob
from app.schemas import ContactRead, SearchCreate, SearchRead
from app.security import require_admin
from app.services.campaign_engine import submit_job
from app.services.exporter import export_csv, export_xlsx
from app.services.importer import import_xlsx

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _csv(value: str) -> list[str]:
    return [
        item.strip()
        for item in value.replace("\n", ",").split(",")
        if item.strip()
    ]


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
def dashboard(request: Request, db: Session = Depends(get_db)):
    jobs = list(
        db.scalars(
            select(SearchJob).order_by(SearchJob.created_at.desc()).limit(30)
        )
    )
    total_contacts = db.scalar(select(func.count(Contact.id))) or 0
    countries = db.scalar(select(func.count(func.distinct(Contact.country)))) or 0
    high_quality = db.scalar(
        select(func.count(Contact.id)).where(Contact.reliability == "high")
    ) or 0
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "jobs": jobs,
            "total_contacts": total_contacts,
            "countries": countries,
            "high_quality": high_quality,
        },
    )


@router.get(
    "/searches/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
def new_search(request: Request):
    return templates.TemplateResponse(request, "search_form.html", {})


@router.post("/searches", dependencies=[Depends(require_admin)])
def create_search(
    sector: str = Form(...),
    countries: str = Form(""),
    cities: str = Form(""),
    keywords: str = Form(""),
    seed_urls: str = Form(""),
    requested_fields: list[str] = Form(default=["email", "phone"]),
    max_results: int = Form(0),
    official_sources_only: bool = Form(False),
    exclude_free_email_providers: bool = Form(False),
    db: Session = Depends(get_db),
):
    payload = SearchCreate(
        sector=sector,
        countries=_csv(countries),
        cities=_csv(cities),
        keywords=_csv(keywords),
        seed_urls=_csv(seed_urls),
        requested_fields=requested_fields,
        max_results=max_results,
        official_sources_only=official_sources_only,
        exclude_free_email_providers=exclude_free_email_providers,
    )
    job = SearchJob(
        sector=payload.sector,
        countries=payload.countries,
        cities=payload.cities,
        keywords=payload.keywords,
        seed_urls=[str(url) for url in payload.seed_urls],
        requested_fields=payload.requested_fields,
        max_results=payload.max_results,
        official_sources_only=payload.official_sources_only,
        exclude_free_email_providers=payload.exclude_free_email_providers,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    submit_job(job.id)
    return RedirectResponse(f"/searches/{job.id}", status_code=303)


@router.get(
    "/searches/{job_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
def search_detail(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    job = db.get(SearchJob, job_id)
    if not job:
        raise HTTPException(404, "Ricerca non trovata")
    contacts = list(
        db.scalars(
            select(Contact)
            .where(Contact.job_id == job_id)
            .order_by(Contact.id.desc())
            .limit(500)
        )
    )
    logs = list(
        db.scalars(
            select(CrawlLog)
            .where(CrawlLog.job_id == job_id)
            .order_by(CrawlLog.id.desc())
            .limit(250)
        )
    )
    queue_stats = {
        status: count
        for status, count in db.execute(
            select(CrawlTarget.status, func.count(CrawlTarget.id))
            .where(CrawlTarget.job_id == job_id)
            .group_by(CrawlTarget.status)
        ).all()
    }
    return templates.TemplateResponse(
        request,
        "search_detail.html",
        {
            "job": job,
            "contacts": contacts,
            "logs": logs,
            "queue_stats": queue_stats,
        },
    )


def _clone_search(original: SearchJob, *, broad: bool = False) -> SearchJob:
    fields = list(original.requested_fields or ["email", "phone"])
    if broad:
        fields = list(
            dict.fromkeys([*fields, "email", "phone", "whatsapp", "address"])
        )
    return SearchJob(
        sector=original.sector,
        countries=list(original.countries or []),
        cities=list(original.cities or []),
        keywords=list(original.keywords or []),
        seed_urls=list(original.seed_urls or []),
        requested_fields=fields,
        max_results=0 if broad else original.max_results,
        official_sources_only=False if broad else original.official_sources_only,
        exclude_free_email_providers=(
            False if broad else original.exclude_free_email_providers
        ),
    )


@router.post(
    "/searches/{job_id}/retry",
    dependencies=[Depends(require_admin)],
)
def retry_search(job_id: str, db: Session = Depends(get_db)):
    original = db.get(SearchJob, job_id)
    if not original:
        raise HTTPException(404, "Ricerca non trovata")
    job = _clone_search(original)
    db.add(job)
    db.commit()
    db.refresh(job)
    submit_job(job.id)
    return RedirectResponse(f"/searches/{job.id}", status_code=303)


@router.post(
    "/searches/{job_id}/broad-retry",
    dependencies=[Depends(require_admin)],
)
def broad_retry_search(job_id: str, db: Session = Depends(get_db)):
    original = db.get(SearchJob, job_id)
    if not original:
        raise HTTPException(404, "Ricerca non trovata")
    job = _clone_search(original, broad=True)
    db.add(job)
    db.commit()
    db.refresh(job)
    submit_job(job.id)
    return RedirectResponse(f"/searches/{job.id}", status_code=303)


@router.get(
    "/contacts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
def contacts_page(
    request: Request,
    q: str = Query(""),
    country: str = Query(""),
    reliability: str = Query(""),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    page_size = 100
    statement = select(Contact)
    count_statement = select(func.count(Contact.id))
    conditions = []
    if q:
        conditions.append(
            or_(
                Contact.organization.ilike(f"%{q}%"),
                Contact.domain.ilike(f"%{q}%"),
            )
        )
    if country:
        conditions.append(Contact.country == country)
    if reliability:
        conditions.append(Contact.reliability == reliability)
    if conditions:
        statement = statement.where(*conditions)
        count_statement = count_statement.where(*conditions)
    total = db.scalar(count_statement) or 0
    contacts = list(
        db.scalars(
            statement.order_by(Contact.verified_at.desc(), Contact.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    countries = [
        value
        for value in db.scalars(
            select(Contact.country).distinct().order_by(Contact.country)
        )
        if value
    ]
    return templates.TemplateResponse(
        request,
        "contacts.html",
        {
            "contacts": contacts,
            "countries": countries,
            "total": total,
            "page": page,
            "page_size": page_size,
            "q": q,
            "country": country,
            "reliability": reliability,
        },
    )


@router.post("/imports/xlsx", dependencies=[Depends(require_admin)])
def upload_xlsx(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    filename = file.filename or "import.xlsx"
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "È accettato solo il formato XLSX")
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as handle:
        shutil.copyfileobj(file.file, handle)
        temp_path = Path(handle.name)
    if temp_path.stat().st_size > 20_000_000:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(413, "File oltre il limite di 20 MB")
    try:
        imported, duplicates = import_xlsx(db, temp_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return RedirectResponse(
        f"/?imported={imported}&duplicates={duplicates}",
        status_code=303,
    )


@router.get("/exports/{format}", dependencies=[Depends(require_admin)])
def download_export(
    format: str,
    job_id: str | None = None,
    db: Session = Depends(get_db),
):
    if format == "csv":
        path = export_csv(db, job_id)
        media_type = "text/csv"
    elif format == "xlsx":
        path = export_xlsx(db, job_id)
        media_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        raise HTTPException(400, "Formato non supportato")
    return FileResponse(path, filename=path.name, media_type=media_type)


@router.get(
    "/api/searches/{job_id}",
    response_model=SearchRead,
    dependencies=[Depends(require_admin)],
)
def search_api(job_id: str, db: Session = Depends(get_db)):
    job = db.get(SearchJob, job_id)
    if not job:
        raise HTTPException(404, "Ricerca non trovata")
    return job


@router.get(
    "/api/contacts",
    response_model=list[ContactRead],
    dependencies=[Depends(require_admin)],
)
def contacts_api(
    limit: int = Query(100, ge=1, le=100_000),
    db: Session = Depends(get_db),
):
    return list(
        db.scalars(select(Contact).order_by(Contact.id.desc()).limit(limit))
    )
