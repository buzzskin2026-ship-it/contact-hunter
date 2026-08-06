from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import (
    Contact,
    CrawlLog,
    CrawlTarget,
    JobStatus,
    SearchJob,
    TargetStatus,
)
from app.services import jobs as legacy
from app.services.commoncrawl import CommonCrawlUrlProvider
from app.services.extractor import reliability_for
from app.services.fast_crawler import FastContactCrawler
from app.services.normalizer import (
    canonical_url,
    contact_fingerprint,
    domain_of,
    is_free_email,
)
from app.services.search import BraveSearchProvider, PublicSearchProvider, build_queries
from app.services.search_bing import BingRssSearchProvider
from app.services.search_open_data import EuropeanOpenDataProvider
from app.services.search_osm import OpenStreetMapDentalProvider, OsmContactRecord

_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="contact-hunter-campaign",
)


@dataclass(frozen=True)
class TargetSpec:
    kind: str
    url: str
    domain: str
    hint: dict = field(default_factory=dict)


@dataclass
class TargetOutcome:
    target_id: int
    candidate: object | None = None
    new_targets: list[TargetSpec] = field(default_factory=list)
    logs: list = field(default_factory=list)
    error: str | None = None


def submit_job(job_id: str) -> None:
    _executor.submit(_run_job_sync, job_id)


def _run_job_sync(job_id: str) -> None:
    asyncio.run(_run_job(job_id))


def _has_capacity(job: SearchJob) -> bool:
    return job.max_results == 0 or job.contacts_found < job.max_results


def _target_hash(kind: str, url: str) -> str:
    return hashlib.sha256(f"{kind}\n{url}".encode()).hexdigest()


def _record_hint(record: OsmContactRecord) -> dict:
    return {
        "organization": record.organization,
        "category": record.category,
        "country": record.country,
        "city": record.city,
        "address": record.address,
        "website": record.website,
        "source_url": record.source_url,
        "source_type": record.source_type,
        "emails": list(record.emails),
        "phones": list(record.phones),
        "external_id": record.external_id,
    }


def _hint_record(hint: dict) -> OsmContactRecord:
    return OsmContactRecord(
        source_url=str(hint.get("source_url") or ""),
        organization=str(hint.get("organization") or "Struttura professionale"),
        category=str(hint.get("category") or "Struttura professionale"),
        country=str(hint.get("country") or ""),
        city=hint.get("city"),
        address=hint.get("address"),
        website=hint.get("website"),
        emails=list(hint.get("emails") or []),
        phones=list(hint.get("phones") or []),
        source_type=str(hint.get("source_type") or "openstreetmap"),
        external_id=hint.get("external_id"),
    )


def _crawl_spec(url: str, hint: dict | None = None) -> TargetSpec | None:
    normalized = canonical_url(url)
    if not normalized:
        return None
    host = domain_of(normalized).removeprefix("www.")
    if not host:
        return None
    if any(
        host == blocked or host.endswith(f".{blocked}")
        for blocked in legacy._DISCOVERY_BLOCKLIST
    ):
        return None
    return TargetSpec("crawl", normalized, host, hint or {})


def _resolve_spec(record: OsmContactRecord) -> TargetSpec:
    hint = _record_hint(record)
    identity = record.external_id or "|".join(
        (
            record.organization.casefold(),
            (record.city or "").casefold(),
            record.country.casefold(),
        )
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return TargetSpec("resolve", f"resolve://{digest}", "", hint)


def _chunks(values: list, size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _enqueue_targets(db, job_id: str, specs: list[TargetSpec]) -> int:
    inserted = 0
    unique: dict[str, TargetSpec] = {}
    for spec in specs:
        digest = _target_hash(spec.kind, spec.url)
        unique.setdefault(digest, spec)

    for chunk in _chunks(list(unique.items()), 500):
        hashes = [digest for digest, _ in chunk]
        existing = set(
            db.scalars(
                select(CrawlTarget.url_hash).where(
                    CrawlTarget.job_id == job_id,
                    CrawlTarget.url_hash.in_(hashes),
                )
            )
        )
        for digest, spec in chunk:
            if digest in existing:
                continue
            db.add(
                CrawlTarget(
                    job_id=job_id,
                    kind=spec.kind,
                    url=spec.url,
                    url_hash=digest,
                    domain=spec.domain,
                    hint=spec.hint,
                )
            )
            inserted += 1
        db.commit()

    total = db.scalar(
        select(func.count(CrawlTarget.id)).where(CrawlTarget.job_id == job_id)
    ) or 0
    job = db.get(SearchJob, job_id)
    if job:
        job.discovered_urls = total
        db.commit()
    return inserted


def _reset_interrupted_targets(db, job_id: str) -> None:
    db.execute(
        update(CrawlTarget)
        .where(
            CrawlTarget.job_id == job_id,
            CrawlTarget.status == TargetStatus.running.value,
        )
        .values(
            status=TargetStatus.queued.value,
            started_at=None,
            last_error="Ripreso dopo riavvio del servizio",
        )
    )
    db.commit()


def _claim_batch(db, job_id: str, settings: Settings) -> list[CrawlTarget]:
    targets = list(
        db.scalars(
            select(CrawlTarget)
            .where(
                CrawlTarget.job_id == job_id,
                CrawlTarget.status == TargetStatus.queued.value,
                CrawlTarget.attempts < settings.campaign_max_attempts,
            )
            .order_by(CrawlTarget.id)
            .limit(settings.campaign_queue_batch_size)
        )
    )
    now = datetime.now(timezone.utc)
    for target in targets:
        target.status = TargetStatus.running.value
        target.started_at = now
        target.attempts += 1
        target.last_error = None
    db.commit()
    return targets


def _directory_note(record: OsmContactRecord) -> str:
    return (
        f"Contatto professionale pubblico scoperto tramite {record.source_type}. "
        "Conservata la fonte pubblica per verifica e aggiornamento."
    )


def _save_directory_contacts(
    job: SearchJob,
    records: list[OsmContactRecord],
    db,
    *,
    include_phone_only: bool,
) -> SearchJob:
    if job.official_sources_only:
        return job
    requested = set(job.requested_fields or [])
    for record in records:
        if not _has_capacity(job):
            break
        emails = record.emails if "email" in requested else []
        phones = record.phones if "phone" in requested else []
        address = record.address if "address" in requested else None
        if not emails and (not include_phone_only or not phones):
            continue
        if job.exclude_free_email_providers and emails and all(
            is_free_email(email) for email in emails
        ):
            continue

        website = record.website or record.source_url
        domain = domain_of(website) or record.source_type
        for email_group in legacy._email_groups(emails, broad=True):
            if not _has_capacity(job):
                break
            fingerprint = contact_fingerprint(domain, email_group, phones)
            db.add(
                Contact(
                    job_id=job.id,
                    organization=record.organization,
                    category=record.category,
                    country=record.country,
                    city=record.city,
                    address=address,
                    website=website,
                    domain=domain,
                    emails=email_group,
                    phones=phones,
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


async def _discover_campaign(
    job: SearchJob,
    settings: Settings,
) -> tuple[list[TargetSpec], list[OsmContactRecord], list[str]]:
    specs: list[TargetSpec] = []
    records: list[OsmContactRecord] = []
    diagnostics: list[str] = []

    for raw in job.seed_urls or []:
        spec = _crawl_spec(str(raw))
        if spec:
            specs.append(spec)

    countries = list(job.countries or [])
    osm = OpenStreetMapDentalProvider(settings)
    if osm.configured_for(job.sector, countries):
        semaphore = asyncio.Semaphore(2)

        async def discover_country(country: str):
            async with semaphore:
                return await osm.search_country(
                    country,
                    limit=settings.osm_max_records_per_country,
                )

        results = await asyncio.gather(
            *(discover_country(country) for country in countries),
            return_exceptions=True,
        )
        for country, result in zip(countries, results, strict=False):
            if isinstance(result, Exception):
                diagnostics.append(f"OSM {country}={type(result).__name__}")
                continue
            found, detail = result
            records.extend(found)
            diagnostics.append(detail)

    records = legacy._dedupe_directory_records(records)
    diagnostics.append(f"Directory pubbliche: {len(records)} attività uniche")
    for record in records:
        hint = _record_hint(record)
        if record.website:
            spec = _crawl_spec(record.website, hint)
            if spec:
                specs.append(spec)
        else:
            specs.append(_resolve_spec(record))

    open_data = EuropeanOpenDataProvider(settings)
    if open_data.configured:
        try:
            resources, detail = await open_data.search(
                job.sector,
                countries,
                list(job.keywords or []),
            )
            diagnostics.append(detail)
            for resource in resources:
                spec = _crawl_spec(resource.url)
                if spec:
                    specs.append(spec)
        except Exception as exc:
            diagnostics.append(f"Open data UE={type(exc).__name__}: {exc}")

    brave = BraveSearchProvider(settings)
    bing = BingRssSearchProvider(settings)
    public = PublicSearchProvider(settings)
    queries = build_queries(
        job.sector,
        countries,
        list(job.cities or []),
        list(job.keywords or []),
    )
    if settings.public_search_max_queries > 0:
        queries = queries[: settings.public_search_max_queries]

    web_urls: list[str] = []
    for query_batch in _chunks(queries, settings.campaign_discovery_batch_size):
        web_urls.extend(
            await legacy._discover_web_urls(
                query_batch,
                brave,
                bing,
                public,
                diagnostics,
                limit=len(query_batch),
            )
        )
    for url in web_urls:
        spec = _crawl_spec(url)
        if spec:
            specs.append(spec)

    diagnostics.append(
        f"Coda iniziale: {len(specs)} piste prima della deduplica persistente"
    )
    return specs, records, diagnostics


async def _resolve_business(
    target: CrawlTarget,
    brave: BraveSearchProvider,
    bing: BingRssSearchProvider,
    public: PublicSearchProvider,
) -> TargetOutcome:
    record = _hint_record(target.hint or {})
    location = " ".join(part for part in (record.city, record.country) if part)
    query = f'"{record.organization}" {location} sito ufficiale contatti email'
    diagnostics: list[str] = []
    try:
        hits = await legacy._provider_hits(
            query,
            brave,
            bing,
            public,
            diagnostics,
            count=20,
        )
    except Exception as exc:
        return TargetOutcome(target.id, error=str(exc))

    specs: list[TargetSpec] = []
    for hit in hits:
        if not legacy._usable_business_hit(hit, record):
            continue
        spec = _crawl_spec(hit.url, target.hint)
        if spec:
            specs.append(spec)
    detail = (
        f"Risoluzione nome: {len(specs)} siti candidati per {record.organization}"
    )
    return TargetOutcome(
        target.id,
        new_targets=specs,
        logs=[(target.url, None, "resolve", detail)],
    )


async def _expand_common_crawl(
    target: CrawlTarget,
    provider: CommonCrawlUrlProvider,
) -> tuple[list[TargetSpec], str | None]:
    if not provider.configured or not target.domain:
        return [], None
    try:
        urls, detail = await provider.discover(target.domain)
    except Exception as exc:
        return [], f"Common Crawl {target.domain}: {type(exc).__name__}"
    specs = [
        spec
        for item in urls
        if (spec := _crawl_spec(item.url, target.hint or {})) is not None
    ]
    return specs, detail


async def _crawl_target(
    target: CrawlTarget,
    crawler: FastContactCrawler,
    common_crawl: CommonCrawlUrlProvider,
    keywords: list[str],
    *,
    expand_common_crawl: bool,
) -> TargetOutcome:
    hint = target.hint or {}
    try:
        common_specs: list[TargetSpec] = []
        common_detail: str | None = None
        if expand_common_crawl:
            common_specs, common_detail = await _expand_common_crawl(
                target,
                common_crawl,
            )
        candidate = await crawler.crawl_domain(
            target.url,
            country=hint.get("country"),
            keywords=keywords,
        )
        logs = list(candidate.logs)
        if common_detail:
            logs.append((target.url, None, "common_crawl", common_detail))
        return TargetOutcome(
            target.id,
            candidate=candidate,
            new_targets=common_specs,
            logs=logs,
        )
    except Exception as exc:
        return TargetOutcome(target.id, error=str(exc))


def _save_candidate_contacts(
    job: SearchJob,
    target: CrawlTarget,
    candidate,
    db,
) -> SearchJob:
    requested = set(job.requested_fields or [])
    emails = candidate.emails if "email" in requested else []
    phones = candidate.phones if "phone" in requested else []
    whatsapp = candidate.whatsapp if "whatsapp" in requested else []
    hint = target.hint or {}
    address = None
    if "address" in requested:
        address = candidate.address or hint.get("address")
    if not emails and not phones and not whatsapp:
        return job
    if job.exclude_free_email_providers and emails and all(
        is_free_email(email) for email in emails
    ):
        return job

    organization = (
        candidate.organization
        or hint.get("organization")
        or candidate.domain.split(".")[0].replace("-", " ").title()
    )
    source_type, source_label = legacy._source_type(candidate.source_url)
    note = (
        f"Contatto estratto da {source_label}. Conservata la fonte pubblica "
        "per verifica, aggiornamento e gestione delle opposizioni."
    )
    for email_group in legacy._email_groups(
        emails,
        broad=not job.official_sources_only,
    ):
        if not _has_capacity(job):
            break
        fingerprint = contact_fingerprint(
            candidate.domain,
            email_group,
            phones or whatsapp,
        )
        db.add(
            Contact(
                job_id=job.id,
                organization=str(organization)[:300],
                category=str(hint.get("category") or job.sector)[:200],
                country=hint.get("country"),
                city=candidate.city or hint.get("city"),
                address=address,
                website=candidate.website,
                domain=candidate.domain,
                emails=email_group,
                phones=phones,
                whatsapp=whatsapp,
                specialties=candidate.specialties,
                source_url=candidate.source_url,
                source_type=source_type,
                reliability=reliability_for(email_group, candidate.website),
                status="verified_public",
                notes=note,
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


def _persist_logs(job: SearchJob, logs: list, db) -> None:
    for log in logs:
        if isinstance(log, tuple):
            url, status_code, action, detail = log
        else:
            url = log.url
            status_code = log.status_code
            action = log.action
            detail = log.detail
        db.add(
            CrawlLog(
                job_id=job.id,
                url=url,
                status_code=status_code,
                action=action,
                detail=detail,
            )
        )
        if action in {"blocked", "robots_blocked"}:
            job.blocked_urls += 1


def _first_domain_target(db, target: CrawlTarget) -> bool:
    earlier = db.scalar(
        select(func.count(CrawlTarget.id)).where(
            CrawlTarget.job_id == target.job_id,
            CrawlTarget.kind == "crawl",
            CrawlTarget.domain == target.domain,
            CrawlTarget.id < target.id,
        )
    ) or 0
    return earlier == 0


async def _process_queue(
    job_id: str,
    directory_records: list[OsmContactRecord],
    settings: Settings,
) -> None:
    db = SessionLocal()
    crawler = FastContactCrawler(settings)
    common_crawl = CommonCrawlUrlProvider(settings)
    brave = BraveSearchProvider(settings)
    bing = BingRssSearchProvider(settings)
    public = PublicSearchProvider(settings)
    semaphore = asyncio.Semaphore(settings.crawler_concurrency)
    try:
        _reset_interrupted_targets(db, job_id)
        while True:
            job = db.get(SearchJob, job_id)
            if not job or not _has_capacity(job):
                break
            batch = _claim_batch(db, job_id, settings)
            if not batch:
                break

            async def run_target(target: CrawlTarget) -> TargetOutcome:
                async with semaphore:
                    if target.kind == "resolve":
                        return await _resolve_business(
                            target,
                            brave,
                            bing,
                            public,
                        )
                    return await _crawl_target(
                        target,
                        crawler,
                        common_crawl,
                        list(job.keywords or []),
                        expand_common_crawl=_first_domain_target(db, target),
                    )

            outcomes = await asyncio.gather(*(run_target(target) for target in batch))

            for outcome in outcomes:
                target = db.get(CrawlTarget, outcome.target_id)
                job = db.get(SearchJob, job_id)
                if not target or not job:
                    continue
                if outcome.new_targets:
                    _enqueue_targets(db, job_id, outcome.new_targets)
                _persist_logs(job, outcome.logs, db)
                if outcome.error:
                    target.last_error = outcome.error
                    if target.attempts < settings.campaign_max_attempts:
                        target.status = TargetStatus.queued.value
                        target.started_at = None
                    else:
                        target.status = TargetStatus.failed.value
                        target.completed_at = datetime.now(timezone.utc)
                    db.add(
                        CrawlLog(
                            job_id=job.id,
                            url=target.url,
                            action="target_error",
                            detail=outcome.error,
                        )
                    )
                else:
                    target.status = TargetStatus.completed.value
                    target.completed_at = datetime.now(timezone.utc)
                    if outcome.candidate is not None:
                        job.crawled_pages += outcome.candidate.pages_crawled
                        job = _save_candidate_contacts(
                            job,
                            target,
                            outcome.candidate,
                            db,
                        )
                db.commit()

        job = db.get(SearchJob, job_id)
        if job and _has_capacity(job):
            _save_directory_contacts(
                job,
                directory_records,
                db,
                include_phone_only=True,
            )
    finally:
        await crawler.close()
        db.close()


async def _run_job(job_id: str) -> None:
    settings = get_settings()
    db = SessionLocal()
    directory_records: list[OsmContactRecord] = []
    try:
        job = db.get(SearchJob, job_id)
        if not job:
            return
        job.status = JobStatus.running.value
        if not job.started_at:
            job.started_at = datetime.now(timezone.utc)
        job.completed_at = None
        job.error_message = None
        db.commit()

        specs, directory_records, diagnostics = await _discover_campaign(job, settings)
        inserted = _enqueue_targets(db, job.id, specs)
        for detail in diagnostics[-250:]:
            db.add(
                CrawlLog(
                    job_id=job.id,
                    url="",
                    action="discovery",
                    detail=detail,
                )
            )
        db.add(
            CrawlLog(
                job_id=job.id,
                url="",
                action="queue",
                detail=f"{inserted} nuove piste inserite nella coda persistente",
            )
        )
        db.commit()

        job = _save_directory_contacts(
            job,
            directory_records,
            db,
            include_phone_only=False,
        )
        db.close()
        await _process_queue(job_id, directory_records, settings)
        db = SessionLocal()

        job = db.get(SearchJob, job_id)
        if job:
            queued = db.scalar(
                select(func.count(CrawlTarget.id)).where(
                    CrawlTarget.job_id == job_id,
                    CrawlTarget.status == TargetStatus.queued.value,
                )
            ) or 0
            if queued == 0 or not _has_capacity(job):
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
        db.close()
