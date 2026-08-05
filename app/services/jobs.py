from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import SessionLocal
from app.models import Contact, CrawlLog, JobStatus, SearchJob
from app.services.crawler import ContactCrawler
from app.services.extractor import reliability_for
from app.services.normalizer import canonical_url, contact_fingerprint, is_free_email
from app.services.search import BraveSearchProvider, PublicSearchProvider, build_queries

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="contact-hunter-job")


def submit_job(job_id: str) -> None:
    _executor.submit(_run_job_sync, job_id)


def _run_job_sync(job_id: str) -> None:
    asyncio.run(_run_job(job_id))


async def _discover(job: SearchJob) -> list[str]:
    settings = get_settings()
    urls: list[str] = []
    for raw in job.seed_urls or []:
        normalized = canonical_url(str(raw))
        if normalized:
            urls.append(normalized)

    brave = BraveSearchProvider(settings)
    public = PublicSearchProvider(settings)
    queries = build_queries(job.sector, job.countries or [], job.cities or [], job.keywords or [])
    query_limit = len(queries) if brave.configured else settings.public_search_max_queries
    selected_queries = queries[:query_limit]
    per_query = min(20, max(5, settings.search_result_limit // max(len(selected_queries), 1)))

    for query in selected_queries:
        hits = []
        if brave.configured:
            try:
                hits = await brave.search(query, count=per_query)
            except Exception:
                hits = []
        if not hits and public.configured:
            try:
                hits = await public.search(query, count=min(per_query, 12))
            except Exception:
                hits = []
        urls.extend(hit.url for hit in hits)

    unique: list[str] = []
    seen_domains: set[str] = set()
    for url in urls:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if not host or host in seen_domains:
            continue
        seen_domains.add(host)
        unique.append(url)
        if len(unique) >= job.max_results * 3:
            break
    return unique


async def _run_job(job_id: str) -> None:
    settings = get_settings()
    db = SessionLocal()
    crawler = ContactCrawler(settings)
    try:
        job = db.get(SearchJob, job_id)
        if not job:
            return
        job.status = JobStatus.running.value
        job.started_at = datetime.now(timezone.utc)
        job.error_message = None
        db.commit()

        urls = await _discover(job)
        job.discovered_urls = len(urls)
        db.commit()
        if not urls:
            raise RuntimeError(
                "Nessun sito trovato. Il motore pubblico può essere temporaneamente limitato: "
                "riprova, configura BRAVE_SEARCH_API_KEY oppure inserisci alcuni URL iniziali."
            )

        country_hint = job.countries[0] if len(job.countries or []) == 1 else None
        city_hint = job.cities[0] if len(job.cities or []) == 1 else None
        semaphore = asyncio.Semaphore(settings.crawler_concurrency)

        async def crawl_one(url: str):
            async with semaphore:
                return await crawler.crawl_domain(url, country=country_hint, keywords=job.keywords)

        tasks = [asyncio.create_task(crawl_one(url)) for url in urls]
        for future in asyncio.as_completed(tasks):
            if job.contacts_found >= job.max_results:
                for pending in tasks:
                    if not pending.done():
                        pending.cancel()
                break
            try:
                candidate = await future
            except asyncio.CancelledError:
                continue
            except Exception as exc:
                db.add(CrawlLog(job_id=job.id, url="", action="error", detail=str(exc)))
                db.commit()
                continue

            job.crawled_pages += candidate.pages_crawled
            for log in candidate.logs:
                db.add(
                    CrawlLog(
                        job_id=job.id,
                        url=log.url,
                        status_code=log.status_code,
                        action=log.action,
                        detail=log.detail,
                    )
                )
                if log.action in {"blocked", "robots_blocked"}:
                    job.blocked_urls += 1

            requested = set(job.requested_fields or [])
            selected_emails = candidate.emails if "email" in requested else []
            selected_phones = candidate.phones if "phone" in requested else []
            selected_whatsapp = candidate.whatsapp if "whatsapp" in requested else []
            selected_address = candidate.address if "address" in requested else None
            if not selected_emails and not selected_phones and not selected_whatsapp:
                db.commit()
                continue
            if job.exclude_free_email_providers and selected_emails and all(is_free_email(e) for e in selected_emails):
                db.commit()
                continue

            fingerprint = contact_fingerprint(candidate.domain, selected_emails, selected_phones or selected_whatsapp)
            organization = candidate.organization or candidate.domain.split(".")[0].replace("-", " ").title()
            contact = Contact(
                job_id=job.id,
                organization=organization[:300],
                category=job.sector[:200],
                country=country_hint,
                city=candidate.city or city_hint,
                address=selected_address,
                website=candidate.website,
                domain=candidate.domain,
                emails=selected_emails,
                phones=selected_phones,
                whatsapp=selected_whatsapp,
                specialties=candidate.specialties,
                source_url=candidate.source_url,
                source_type="official_website",
                reliability=reliability_for(selected_emails, candidate.website),
                status="verified_public",
                notes="Contatto estratto da una pagina pubblica. Verificare finalità e base giuridica prima dell'uso commerciale.",
                fingerprint=fingerprint,
            )
            db.add(contact)
            try:
                db.flush()
                job.contacts_found += 1
                db.commit()
            except IntegrityError:
                db.rollback()
                job = db.get(SearchJob, job_id)
                if job:
                    job.duplicates_skipped += 1
                    db.commit()

        job = db.get(SearchJob, job_id)
        if job:
            job.status = JobStatus.completed.value
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as exc:
        db.rollback()
        job = db.get(SearchJob, job_id)
        if job:
            job.status = JobStatus.failed.value
            job.error_message = str(exc)
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        await crawler.close()
        db.close()
