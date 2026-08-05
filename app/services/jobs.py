from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import SessionLocal
from app.models import Contact, CrawlLog, JobStatus, SearchJob
from app.services.commoncrawl import CommonCrawlUrlProvider
from app.services.crawler import ContactCrawler
from app.services.extractor import reliability_for
from app.services.normalizer import (
    canonical_url,
    contact_fingerprint,
    domain_of,
    is_free_email,
)
from app.services.search import BraveSearchProvider, PublicSearchProvider, SearchHit, build_queries
from app.services.search_bing import BingRssSearchProvider
from app.services.search_osm import OpenStreetMapDentalProvider, OsmContactRecord

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="contact-hunter-job")

_DISCOVERY_BLOCKLIST = {
    "bing.com",
    "duckduckgo.com",
    "google.com",
    "openstreetmap.org",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "tiktok.com",
    "x.com",
    "twitter.com",
    "wikipedia.org",
    "yelp.com",
    "tripadvisor.com",
}
_GENERIC_TOKENS = {
    "dental",
    "dentist",
    "dentiste",
    "dentisti",
    "dentistry",
    "clinic",
    "clinica",
    "clinique",
    "studio",
    "cabinet",
    "centre",
    "center",
    "centro",
    "praxis",
    "zahnarzt",
    "zahnarztpraxis",
    "odontoiatrico",
    "odontoiatrica",
    "stomatologiczna",
    "stomatologia",
    "laboratorio",
    "laboratory",
}


def submit_job(job_id: str) -> None:
    _executor.submit(_run_job_sync, job_id)


def _run_job_sync(job_id: str) -> None:
    asyncio.run(_run_job(job_id))


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def _email_groups(emails: list[str], *, broad: bool) -> list[list[str]]:
    unique = list(dict.fromkeys(email.strip().lower() for email in emails if email.strip()))
    if not unique:
        return [[]]
    if broad:
        return [[email] for email in unique]
    return [unique]


def _distinctive_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", _fold(value))
        if len(token) >= 4 and token not in _GENERIC_TOKENS
    }


def _usable_business_hit(hit: SearchHit, record: OsmContactRecord) -> bool:
    host = domain_of(hit.url).removeprefix("www.")
    if not host or any(host == blocked or host.endswith(f".{blocked}") for blocked in _DISCOVERY_BLOCKLIST):
        return False
    if hit.url.lower().endswith((".doc", ".docx", ".xls", ".xlsx")):
        return False
    tokens = _distinctive_tokens(record.organization)
    if not tokens:
        return True
    haystack = _fold(" ".join((host, hit.title or "", hit.description or "")))
    return any(token in haystack for token in tokens)


def _round_robin_records(records: list[OsmContactRecord], limit: int) -> list[OsmContactRecord]:
    by_country: dict[str, deque[OsmContactRecord]] = defaultdict(deque)
    for record in records:
        by_country[record.country].append(record)
    selected: list[OsmContactRecord] = []
    country_queue = deque(by_country)
    while country_queue and len(selected) < limit:
        country = country_queue.popleft()
        queue = by_country[country]
        if queue:
            selected.append(queue.popleft())
        if queue:
            country_queue.append(country)
    return selected


async def _provider_hits(
    query: str,
    brave: BraveSearchProvider,
    bing: BingRssSearchProvider,
    public: PublicSearchProvider,
    diagnostics: list[str],
    count: int = 8,
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    if brave.configured:
        try:
            hits = await brave.search(query, count=count)
        except Exception as exc:
            diagnostics.append(f"Brave exact={type(exc).__name__}")
    if not hits and bing.configured:
        try:
            hits = await bing.search(query, count=count)
        except Exception as exc:
            diagnostics.append(f"BingRSS exact={type(exc).__name__}")
    if not hits and public.configured:
        try:
            hits = await public.search(query, count=count)
        except Exception as exc:
            diagnostics.append(f"DuckDuckGo exact={type(exc).__name__}")
    return hits


async def _resolve_named_records(
    records: list[OsmContactRecord],
    brave: BraveSearchProvider,
    bing: BingRssSearchProvider,
    public: PublicSearchProvider,
    diagnostics: list[str],
    max_results: int,
) -> list[OsmContactRecord]:
    unresolved = [record for record in records if not record.website]
    if not unresolved:
        return []

    lookup_limit = min(max_results * 2, 300 if brave.configured else 120)
    selected = _round_robin_records(unresolved, lookup_limit)
    semaphore = asyncio.Semaphore(6 if brave.configured else 3)

    async def resolve(record: OsmContactRecord) -> OsmContactRecord | None:
        location = " ".join(part for part in (record.city, record.country) if part)
        query = f'"{record.organization}" {location} sito ufficiale contatti email'
        async with semaphore:
            hits = await _provider_hits(query, brave, bing, public, diagnostics)
        for hit in hits:
            if not _usable_business_hit(hit, record):
                continue
            record.website = canonical_url(hit.url)
            return record if record.website else None
        return None

    results = await asyncio.gather(
        *(resolve(record) for record in selected),
        return_exceptions=True,
    )
    resolved = [result for result in results if isinstance(result, OsmContactRecord)]
    diagnostics.append(
        f"Risoluzione per nome: {len(resolved)} siti trovati su {len(selected)} strutture tentate"
    )
    return resolved


async def _expand_with_common_crawl(
    urls: list[str],
    diagnostics: list[str],
) -> list[str]:
    settings = get_settings()
    provider = CommonCrawlUrlProvider(settings)
    if not provider.configured or settings.common_crawl_max_domains <= 0:
        return urls

    expanded: list[str] = []
    replaced = 0
    attempted = 0
    for original in urls:
        if attempted >= settings.common_crawl_max_domains:
            expanded.append(original)
            continue
        host = domain_of(original).removeprefix("www.")
        if not host:
            expanded.append(original)
            continue
        attempted += 1
        try:
            archived_urls, _ = await provider.discover(host)
        except Exception as exc:
            if len(diagnostics) < 100:
                diagnostics.append(f"Common Crawl {host}={type(exc).__name__}")
            expanded.append(original)
            continue
        if archived_urls:
            expanded.append(archived_urls[0].url)
            replaced += 1
        else:
            expanded.append(original)
    diagnostics.append(
        f"Common Crawl: {replaced} domini avviati da una pagina contatti/PDF su {attempted} interrogati"
    )
    return expanded


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
            300,
            max(80, (job.max_results * 3 // country_count) + 40),
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

    brave = BraveSearchProvider(settings)
    bing = BingRssSearchProvider(settings)
    public = PublicSearchProvider(settings)

    resolved_records = await _resolve_named_records(
        osm_records,
        brave,
        bing,
        public,
        diagnostics,
        job.max_results,
    )
    for record in osm_records:
        if not record.website:
            continue
        urls.append(record.website)
        host = domain_of(record.website).removeprefix("www.")
        if host and host not in domain_hints:
            domain_hints[host] = record
    if resolved_records:
        diagnostics.append(f"Siti ufficiali aggiunti da nomi OSM: {len(resolved_records)}")

    queries = build_queries(job.sector, countries, job.cities or [], job.keywords or [])
    minimum_country_queries = min(60, max(len(countries) * 4, 1))
    query_limit = len(queries) if brave.configured else max(
        settings.public_search_max_queries,
        minimum_country_queries,
    )
    selected_queries = queries[: min(query_limit, len(queries))]
    per_query = min(20, max(5, settings.search_result_limit // max(len(selected_queries), 1)))

    for query in selected_queries:
        hits = await _provider_hits(
            query,
            brave,
            bing,
            public,
            diagnostics,
            count=min(per_query, 15),
        )
        urls.extend(hit.url for hit in hits)

    unique: list[str] = []
    seen_domains: set[str] = set()
    max_domains = min(600, max(120, job.max_results * 2))
    for url in urls:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if not host or host in seen_domains:
            continue
        if any(host == blocked or host.endswith(f".{blocked}") for blocked in _DISCOVERY_BLOCKLIST):
            continue
        seen_domains.add(host)
        unique.append(url)
        if len(unique) >= max_domains:
            break

    unique = await _expand_with_common_crawl(unique, diagnostics)
    diagnostics.append(f"Domini unici pronti per il crawler: {len(unique)}")
    return unique, osm_records, domain_hints, diagnostics


def _save_osm_contacts(
    job: SearchJob,
    records: list[OsmContactRecord],
    db,
) -> SearchJob:
    """Save public directory contacts when broad collection is selected."""
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
        for email_group in _email_groups(selected_emails, broad=True):
            if job.contacts_found >= job.max_results:
                break
            fingerprint = contact_fingerprint(domain, email_group, selected_phones)
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
                    emails=email_group,
                    phones=selected_phones,
                    whatsapp=[],
                    specialties=[],
                    source_url=record.source_url,
                    source_type="openstreetmap",
                    reliability="medium",
                    status="public_directory",
                    notes=(
                        "Contatto professionale pubblico scoperto tramite OpenStreetMap. "
                        "Ricontrollare sul sito ufficiale prima di campagne massive. "
                        "Dati OSM © OpenStreetMap contributors."
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
        for detail in diagnostics[-80:]:
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
                "Per la massima copertura configura BRAVE_SEARCH_API_KEY oppure usa la raccolta ampia."
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

            organization = (
                candidate.organization
                or (hint.organization if hint else None)
                or candidate.domain.split(".")[0].replace("-", " ").title()
            )
            source_is_pdf = candidate.source_url.casefold().split("?", 1)[0].endswith(".pdf")
            source_type = "public_pdf" if source_is_pdf else "official_website"
            note = (
                "Contatto estratto da un PDF pubblicamente accessibile. Verificare attualità e fonte "
                "prima dell'uso commerciale."
                if source_is_pdf
                else "Contatto estratto da una pagina pubblica del sito. Verificare finalità e base "
                "giuridica prima dell'uso commerciale."
            )

            for email_group in _email_groups(
                selected_emails,
                broad=not job.official_sources_only,
            ):
                if job.contacts_found >= job.max_results:
                    break
                fingerprint = contact_fingerprint(
                    candidate.domain,
                    email_group,
                    selected_phones or selected_whatsapp,
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
                    emails=email_group,
                    phones=selected_phones,
                    whatsapp=selected_whatsapp,
                    specialties=candidate.specialties,
                    source_url=candidate.source_url,
                    source_type=source_type,
                    reliability=reliability_for(email_group, candidate.website),
                    status="verified_public",
                    notes=note,
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
