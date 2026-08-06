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
from app.services.search_open_data import EuropeanOpenDataProvider
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
_DOCUMENT_EXTENSIONS = (".pdf", ".csv", ".tsv", ".xlsx", ".xls", ".json", ".xml")
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
    if hit.url.lower().endswith((".doc", ".docx")):
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


def _dedupe_directory_records(records: list[OsmContactRecord]) -> list[OsmContactRecord]:
    unique: list[OsmContactRecord] = []
    seen: set[str] = set()
    for record in records:
        key = record.external_id or "|".join(
            (
                record.organization.casefold(),
                (record.city or "").casefold(),
                (record.address or "").casefold(),
                record.website or "",
            )
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


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

    lookup_limit = min(max_results * 2, 2_500 if brave.configured else 300)
    selected = _round_robin_records(unresolved, lookup_limit)
    semaphore = asyncio.Semaphore(8 if brave.configured else 3)

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
    attempted_hosts: set[str] = set()
    for original in urls:
        host = domain_of(original).removeprefix("www.")
        if not host or host in attempted_hosts or attempted >= settings.common_crawl_max_domains:
            expanded.append(original)
            continue
        attempted_hosts.add(host)
        attempted += 1
        try:
            archived_urls, _ = await provider.discover(host)
        except Exception as exc:
            if len(diagnostics) < 150:
                diagnostics.append(f"Common Crawl {host}={type(exc).__name__}")
            expanded.append(original)
            continue
        if archived_urls:
            expanded.append(archived_urls[0].url)
            expanded.append(original)
            replaced += 1
        else:
            expanded.append(original)
    diagnostics.append(
        f"Common Crawl: {replaced} domini arricchiti su {attempted} interrogati"
    )
    return list(dict.fromkeys(expanded))


async def _discover_web_urls(
    queries: list[str],
    brave: BraveSearchProvider,
    bing: BingRssSearchProvider,
    public: PublicSearchProvider,
    diagnostics: list[str],
    limit: int,
) -> list[str]:
    selected = queries[:limit]
    semaphore = asyncio.Semaphore(6 if brave.configured else 3)

    async def run(query: str) -> list[SearchHit]:
        async with semaphore:
            return await _provider_hits(
                query,
                brave,
                bing,
                public,
                diagnostics,
                count=15,
            )

    results = await asyncio.gather(*(run(query) for query in selected), return_exceptions=True)
    urls: list[str] = []
    successful = 0
    for result in results:
        if isinstance(result, Exception):
            continue
        successful += 1
        urls.extend(hit.url for hit in result)
    diagnostics.append(f"Ricerca web/PDF: {successful} query completate su {len(selected)}")
    return urls


def _select_urls(urls: list[str], max_urls: int) -> list[str]:
    unique: list[str] = []
    seen_urls: set[str] = set()
    domain_counts: defaultdict[str, int] = defaultdict(int)

    for raw in urls:
        url = canonical_url(raw)
        if not url or url in seen_urls:
            continue
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if not host:
            continue
        if any(host == blocked or host.endswith(f".{blocked}") for blocked in _DISCOVERY_BLOCKLIST):
            continue
        path = parsed.path.casefold()
        is_document = path.endswith(_DOCUMENT_EXTENSIONS)
        per_domain_limit = 10 if is_document else 2
        if domain_counts[host] >= per_domain_limit:
            continue
        seen_urls.add(url)
        domain_counts[host] += 1
        unique.append(url)
        if len(unique) >= max_urls:
            break
    return unique


async def _discover(
    job: SearchJob,
) -> tuple[list[str], list[OsmContactRecord], dict[str, OsmContactRecord], list[str]]:
    settings = get_settings()
    urls: list[str] = []
    diagnostics: list[str] = []
    directory_records: list[OsmContactRecord] = []
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
            settings.osm_max_records_per_country,
            max(1_000, (job.max_results * 2 // country_count) + 500),
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
            directory_records.extend(records)

    directory_records = _dedupe_directory_records(directory_records)
    diagnostics.append(f"Directory OSM: {len(directory_records)} attività uniche")

    open_data = EuropeanOpenDataProvider(settings)
    if open_data.configured:
        try:
            resources, detail = await open_data.search(
                job.sector,
                countries,
                list(job.keywords or []),
            )
            urls.extend(resource.url for resource in resources)
            diagnostics.append(detail)
        except Exception as exc:
            diagnostics.append(f"Open data UE={type(exc).__name__}: {exc}")

    brave = BraveSearchProvider(settings)
    bing = BingRssSearchProvider(settings)
    public = PublicSearchProvider(settings)

    resolved_records = await _resolve_named_records(
        directory_records,
        brave,
        bing,
        public,
        diagnostics,
        job.max_results,
    )
    for record in directory_records:
        if not record.website:
            continue
        urls.append(record.website)
        host = domain_of(record.website).removeprefix("www.")
        if host and host not in domain_hints:
            domain_hints[host] = record
    if resolved_records:
        diagnostics.append(f"Siti ufficiali aggiunti da nomi OSM: {len(resolved_records)}")

    queries = build_queries(job.sector, countries, list(job.cities or []), list(job.keywords or []))
    web_query_limit = min(
        len(queries),
        settings.public_search_max_queries * (3 if brave.configured else 1),
    )
    urls.extend(
        await _discover_web_urls(
            queries,
            brave,
            bing,
            public,
            diagnostics,
            limit=web_query_limit,
        )
    )

    max_urls = min(20_000, max(2_000, job.max_results * 4))
    selected = _select_urls(urls, max_urls=max_urls)
    selected = await _expand_with_common_crawl(selected, diagnostics)
    diagnostics.append(f"URL e documenti pronti per il crawler: {len(selected)}")
    return selected, directory_records, domain_hints, diagnostics


def _directory_note(_: OsmContactRecord) -> str:
    return (
        "Contatto professionale pubblico scoperto tramite OpenStreetMap. "
        "Dati OSM © OpenStreetMap contributors."
    )


def _save_directory_contacts(
    job: SearchJob,
    records: list[OsmContactRecord],
    db,
    *,
    defer_website_only: bool,
) -> SearchJob:
    if job.official_sources_only:
        return job

    requested = set(job.requested_fields or [])
    for record in records:
        if job.contacts_found >= job.max_results:
            break
        selected_emails = record.emails if "email" in requested else []
        selected_phones = record.phones if "phone" in requested else []
        selected_address = record.address if "address" in requested else None

        if defer_website_only and record.website and not selected_emails:
            continue
        if not selected_emails and not selected_phones:
            continue
        if job.exclude_free_email_providers and selected_emails and all(
            is_free_email(email) for email in selected_emails
        ):
            continue

        website = record.website or record.source_url
        domain = domain_of(website) or record.source_type
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
                    source_type=record.source_type,
                    reliability="medium",
                    status="public_directory",
                    notes=_directory_note(record),
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


def _source_type(url: str) -> tuple[str, str]:
    path = urlparse(url).path.casefold()
    if path.endswith(".pdf"):
        return "public_pdf", "PDF pubblicamente accessibile"
    if path.endswith((".csv", ".tsv")):
        return "public_csv", "dataset CSV/TSV pubblicamente accessibile"
    if path.endswith((".xlsx", ".xls")):
        return "public_spreadsheet", "foglio elettronico pubblicamente accessibile"
    if path.endswith((".json", ".xml")):
        return "public_dataset", "dataset strutturato pubblicamente accessibile"
    return "official_website", "pagina pubblicamente accessibile del sito"


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

        urls, directory_records, domain_hints, diagnostics = await _discover(job)
        job.discovered_urls = len(urls)
        for detail in diagnostics[-150:]:
            db.add(CrawlLog(job_id=job.id, url="", action="discovery", detail=detail))
        db.commit()

        job = _save_directory_contacts(
            job,
            directory_records,
            db,
            defer_website_only=True,
        )
        if not urls:
            job = _save_directory_contacts(
                job,
                directory_records,
                db,
                defer_website_only=False,
            )
            if job.contacts_found:
                job.status = JobStatus.completed.value
                job.completed_at = datetime.now(timezone.utc)
                db.commit()
                return
            details = "; ".join(diagnostics[-10:]) or "nessun provider eseguito"
            raise RuntimeError(
                "Nessun sito o documento trovato dai provider disponibili. "
                f"Diagnostica: {details}. "
                "Configura BRAVE_SEARCH_API_KEY e aggiungi URL di albi/associazioni per ampliare la copertura."
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
            source_type, source_label = _source_type(candidate.source_url)
            note = (
                f"Contatto estratto da {source_label}. Verificare attualità, finalità e base giuridica "
                "prima dell'uso commerciale."
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
            job = _save_directory_contacts(
                job,
                directory_records,
                db,
                defer_website_only=False,
            )
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
