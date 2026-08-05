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
from app.services.normalizer import (
    canonical_url,
    contact_fingerprint,
    domain_of,
    is_free_email,
)
from app.services.search import BraveSearchProvider, PublicSearchProvider, build_queries
from app.services.search_bing import BingRssSearchProvider
from app.services.search_osm import OpenStreetMapDentalProvider, OsmContactRecord

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="contact-hunter-job")


def submit_job(job_id: str) -> None:
    _executor.submit(_run_job_sync, job_id)


def _run_job_sync(job_id: str) -> None:
    asyncio.run(_run_job(job_id))


async def _discover(
    job: SearchJob,
) -> tuple[list[str], list[OsmContactRecord], dict[str, OsmContactRecord], list[str]]:
    settings = get_settings()
    urls: list[str] = []
    diagnostics: list[str] = []
    osm_records: list[OsmContactRecord] = []
    domain_hints: dict[str, OsmContactRecord] = {}

    for raw in job.seed_urls or []:
        normalized = canonical_url(str(raw))
        if normalized:
            urls.append(normalized)

    countries = list(job.countries or [])
    osm = OpenStreetMapDentalProvider(settings)
    if osm.configured_for(job.sector, countries):
        country_count = max(len(countries), 1)
        per_country_limit = min(
            120,
            max(35, (job.max_results * 2 // country_count) + 20),
        )
        semaphore = asyncio.Semaphore(2)

        async def discover_country(country: str):
            async with semaphore:
                return await osm.search_country(country, limit=per_country_limit)

        osm_results = await asyncio.gather(
            *(discover_country(country) for country in countries),
            return_exceptions=True,
        )
        for country, result in zip(countries, osm_results, strict=False):
            if isinstance(result, Exception):
                diagnostics.append(f"OSM {country}={type(result).__name__}")
                continue
            records, detail = result
            diagnostics.append(detail)
            osm_records.extend(records)
            for record in records:
                if not record.website:
                    continue
                urls.append(record.website)
                host = domain_of(record.website).removeprefix("www.")
                if host and host not in domain_hints:
                    domain_hints[host] = record

    brave = BraveSearchProvider(settings)
    bing = BingRssSearchProvider(settings)
    public = PublicSearchProvider(settings)
    queries = build_queries(job.sector, countries, job.cities or [], job.keywords or [])
    # Guarantee at least two queries per requested country even when the environment
    # still contains an older conservative PUBLIC_SEARCH_MAX_QUERIES value.
    minimum_country_queries = min(30, max(len(countries) * 2, 1))
    query_limit = len(queries) if brave.configured else max(
        settings.public_search_max_queries,
        minimum_country_queries,
    )
    selected_queries = queries[: min(query_limit, len(queries))]
    per_query = min(20, max(5, settings.search_result_limit // max(len(selected_queries), 1)))

    for query in selected_queries:
        hits = []

        if brave.configured:
            try:
                hits = await brave.search(query, count=per_query)
                diagnostics.append(f"Brave={len(hits)}")
            except Exception as exc:
                diagnostics.append(f"Brave={type(exc).__name__}")

        if not hits and bing.configured:
            try:
                hits = await bing.search(query, count=min(per_query, 12))
                diagnostics.append(f"BingRSS={len(hits)}")
            except Exception as exc:
                diagnostics.append(f"BingRSS={type(exc).__name__}")

        if not hits and public.configured:
            try:
                hits = await public.search(query, count=min(per_query, 12))
                diagnostics.append(f"DuckDuckGo={len(hits)}")
            except Exception as exc:
                diagnostics.append(f"DuckDuckGo={type(exc).__name__}")

        urls.extend(hit.url for hit in hits)

    unique: list[str] = []
    seen_domains: set[str] = set()
    max_domains = max(60, job.max_results * 2)
    for url in urls:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if not host or host in seen_domains:
            continue
        seen_domains.add(host)
        unique.append(url)
        if len(unique) >= max_domains:
            break
    return unique, osm_records, domain_hints, diagnostics


def _save_osm_contacts(
    job: SearchJob,
    records: list[OsmContactRecord],
    db,
) -> SearchJob:
    """Save structured public contacts only when non-official sources are allowed."""
    if job.official_sources_only:
        return job

    requested = set(job.requested_fields or [])
    for record in records:
        if job.contacts_found >= job.max_results:
            break
        selected_emails = record.emails if "email" in requested else []
        selected_phones = record.phones if "phone" in requested else []
        selected_address = record.address if "address" in requested else None
        if not selected_emails and not selected_phones:
            continue
        if job.exclude_free_email_providers and selected_emails and all(
            is_free_email(email) for email in selected_emails
        ):
            continue

        website = record.website or record.source_url
        domain = domain_of(website) or "openstreetmap.org"
        fingerprint = contact_fingerprint(domain, selected_emails, selected_phones)
        db.add(
            Contact(
                job_id=job.id,
                organization=record.organization,
                category=record.category,
                country=record.country,
                city=record.city,
                address=selected_address,
                website=website,
                domain=domain,
                emails=selected_emails,
                phones=selected_phones,
                whatsapp=[],
                specialties=[],
                source_url=record.source_url,
                source_type="openstreetmap",
                reliability="medium",
                status="public_directory",
                notes=(
                    "Contatto pubblico scoperto tramite OpenStreetMap; verificare sul sito ufficiale "
                    "prima dell'uso commerciale. Dati OSM © OpenStreetMap contributors."
                ),
                fingerprint=fingerprint,
            )
        )
        try:
            db.flush()
            job.contacts_found += 1
            db.commit()
        except IntegrityError:
            db.rollback()
            job = db.get(SearchJob, job.id)
            if job:
                job.duplicates_skipped += 1
                db.commit()
    return db.get(SearchJob, job.id) or job


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

        urls, osm_records, domain_hints, diagnostics = await _discover(job)
        job.discovered_urls = len(urls)
        for detail in diagnostics[-30:]:
            db.add(CrawlLog(job_id=job.id, url="", action="discovery", detail=detail))
        db.commit()

        job = _save_osm_contacts(job, osm_records, db)
        if not urls:
            if job.contacts_found:
                job.status = JobStatus.completed.value
                job.completed_at = datetime.now(timezone.utc)
                db.commit()
                return
            details = "; ".join(diagnostics[-10:]) or "nessun provider eseguito"
            raise RuntimeError(
                "Nessun sito trovato dai motori disponibili. "
                f"Diagnostica: {details}. "
                "Per una copertura più ampia configura BRAVE_SEARCH_API_KEY oppure inserisci URL iniziali."
            )

        country_hint = job.countries[0] if len(job.countries or []) == 1 else None
        city_hint = job.cities[0] if len(job.cities or []) == 1 else None
        semaphore = asyncio.Semaphore(settings.crawler_concurrency)

        async def crawl_one(url: str):
            host = domain_of(url).removeprefix("www.")
            hint = domain_hints.get(host)
            async with semaphore:
                candidate = await crawler.crawl_domain(
                    url,
                    country=hint.country if hint else country_hint,
                    keywords=job.keywords,
                )
                return candidate, hint

        tasks = [asyncio.create_task(crawl_one(url)) for url in urls]
        for future in asyncio.as_completed(tasks):
            if job.contacts_found >= job.max_results:
                for pending in tasks:
                    if not pending.done():
                        pending.cancel()
                break
            try:
                candidate, hint = await future
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
            selected_address = None
            if "address" in requested:
                selected_address = candidate.address or (hint.address if hint else None)
            if not selected_emails and not selected_phones and not selected_whatsapp:
                db.commit()
                continue
            if job.exclude_free_email_providers and selected_emails and all(
                is_free_email(email) for email in selected_emails
            ):
                db.commit()
                continue

            fingerprint = contact_fingerprint(
                candidate.domain,
                selected_emails,
                selected_phones or selected_whatsapp,
            )
            organization = (
                candidate.organization
                or (hint.organization if hint else None)
                or candidate.domain.split(".")[0].replace("-", " ").title()
            )
            contact = Contact(
                job_id=job.id,
                organization=organization[:300],
                category=(hint.category if hint else job.sector)[:200],
                country=hint.country if hint else country_hint,
                city=candidate.city or (hint.city if hint else None) or city_hint,
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
                notes=(
                    "Contatto estratto da una pagina pubblica del sito ufficiale. Verificare finalità "
                    "e base giuridica prima dell'uso commerciale."
                ),
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
