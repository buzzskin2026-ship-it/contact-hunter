from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import httpx

from app.config import Settings
from app.services.extractor import ExtractedPage, extract_page
from app.services.normalizer import canonical_url, domain_of, is_same_or_subdomain, root_url, url_is_allowed
from app.services.robots import RobotsCache


_CONTACT_PATHS = (
    "contact",
    "contacts",
    "contact-us",
    "contatti",
    "kontakt",
    "kontakte",
    "contacto",
    "contactos",
    "impressum",
    "legal-notice",
    "about",
    "chi-siamo",
)


@dataclass
class PageResult:
    url: str
    status_code: int | None
    html: str | None
    action: str
    detail: str | None = None


@dataclass
class OrganizationCandidate:
    organization: str | None
    website: str
    domain: str
    source_url: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    whatsapp: list[str] = field(default_factory=list)
    address: str | None = None
    city: str | None = None
    specialties: list[str] = field(default_factory=list)
    pages_crawled: int = 0
    logs: list[PageResult] = field(default_factory=list)


class DomainRateLimiter:
    def __init__(self, default_delay: float) -> None:
        self.default_delay = default_delay
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request: dict[str, float] = {}

    async def wait(self, domain: str, requested_delay: float | None = None) -> None:
        delay = max(self.default_delay, requested_delay or 0)
        async with self._locks[domain]:
            elapsed = time.monotonic() - self._last_request.get(domain, 0.0)
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
            self._last_request[domain] = time.monotonic()


class ContactCrawler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        headers = {
            "User-Agent": settings.crawler_user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "en,it;q=0.8,*;q=0.5",
        }
        self.client = httpx.AsyncClient(
            headers=headers,
            timeout=settings.crawler_timeout_seconds,
            follow_redirects=False,
        )
        self.robots = RobotsCache(self.client, settings.crawler_user_agent, settings.respect_robots_txt)
        self.limiter = DomainRateLimiter(settings.crawler_delay_seconds)

    async def close(self) -> None:
        await self.client.aclose()

    async def _safe(self, url: str) -> tuple[bool, str | None]:
        return await asyncio.to_thread(url_is_allowed, url, self.settings.blocked_domain_set)

    async def _fetch_http(self, url: str) -> PageResult:
        current = url
        for _ in range(6):
            allowed, reason = await self._safe(current)
            if not allowed:
                return PageResult(current, None, None, "blocked", reason)
            robots = await self.robots.can_fetch(current)
            if not robots.allowed:
                return PageResult(current, None, None, "robots_blocked", "robots.txt non consente la scansione")
            await self.limiter.wait(domain_of(current), robots.crawl_delay)
            try:
                response = await self.client.get(current)
            except httpx.HTTPError as exc:
                return PageResult(current, None, None, "error", str(exc))

            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    return PageResult(current, response.status_code, None, "error", "redirect senza Location")
                redirected = canonical_url(location, current)
                if not redirected:
                    return PageResult(current, response.status_code, None, "blocked", "redirect non valido")
                current = redirected
                continue

            content_type = response.headers.get("content-type", "").lower()
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                return PageResult(current, response.status_code, None, "ignored", f"content-type {content_type or 'sconosciuto'}")
            content = response.content
            if len(content) > self.settings.crawler_max_response_bytes:
                return PageResult(current, response.status_code, None, "ignored", "pagina oltre il limite di dimensione")
            encoding = response.encoding or "utf-8"
            try:
                html = content.decode(encoding, errors="replace")
            except LookupError:
                html = content.decode("utf-8", errors="replace")
            return PageResult(current, response.status_code, html, "fetched")
        return PageResult(current, None, None, "error", "troppi redirect")

    async def _fetch_playwright(self, url: str) -> PageResult:
        if not self.settings.playwright_enabled:
            return PageResult(url, None, None, "ignored", "fallback Playwright disabilitato")
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return PageResult(url, None, None, "error", "Playwright non installato")
        allowed, reason = await self._safe(url)
        if not allowed:
            return PageResult(url, None, None, "blocked", reason)
        robots = await self.robots.can_fetch(url)
        if not robots.allowed:
            return PageResult(url, None, None, "robots_blocked", "robots.txt non consente la scansione")
        await self.limiter.wait(domain_of(url), robots.crawl_delay)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context(user_agent=self.settings.crawler_user_agent)

                async def guard_request(route):
                    request_url = route.request.url
                    if request_url.startswith(("data:", "blob:")):
                        await route.continue_()
                        return
                    request_allowed, _ = await self._safe(request_url)
                    if request_allowed:
                        await route.continue_()
                    else:
                        await route.abort()

                await context.route("**/*", guard_request)
                page = await context.new_page()
                response = await page.goto(url, wait_until="domcontentloaded", timeout=self.settings.crawler_timeout_seconds * 1000)
                await page.wait_for_timeout(500)
                final_url = canonical_url(page.url)
                if not final_url:
                    await browser.close()
                    return PageResult(url, None, None, "error", "URL finale non valida")
                final_allowed, final_reason = await self._safe(final_url)
                if not final_allowed:
                    await browser.close()
                    return PageResult(final_url, None, None, "blocked", final_reason)
                html = await page.content()
                status_code = response.status if response else None
                await browser.close()
                if len(html.encode("utf-8")) > self.settings.crawler_max_response_bytes:
                    return PageResult(final_url, status_code, None, "ignored", "pagina oltre il limite di dimensione")
                return PageResult(final_url, status_code, html, "playwright")
        except Exception as exc:
            return PageResult(url, None, None, "error", f"Playwright: {exc}")

    async def fetch(self, url: str) -> PageResult:
        result = await self._fetch_http(url)
        if result.html and len(result.html) >= 1_000:
            return result
        if result.action in {"blocked", "robots_blocked", "ignored"}:
            return result
        fallback = await self._fetch_playwright(result.url)
        return fallback if fallback.html else result

    async def crawl_domain(
        self,
        start_url: str,
        country: str | None = None,
        keywords: list[str] | None = None,
    ) -> OrganizationCandidate:
        normalized_start = canonical_url(start_url)
        if not normalized_start:
            raise ValueError("URL iniziale non valida")
        parent_domain = domain_of(normalized_start).removeprefix("www.")
        base_root = root_url(normalized_start)
        seeds = [normalized_start, base_root]
        seeds.extend(urljoin(f"{base_root}/", path) for path in _CONTACT_PATHS)
        queue: deque[str] = deque(dict.fromkeys(seeds))
        visited: set[str] = set()
        pages: list[tuple[str, ExtractedPage]] = []
        logs: list[PageResult] = []
        attempt_limit = max(self.settings.crawler_max_pages_per_domain * 4, 16)

        while (
            queue
            and len(pages) < self.settings.crawler_max_pages_per_domain
            and len(visited) < attempt_limit
        ):
            url = queue.popleft()
            canonical = canonical_url(url)
            if not canonical or canonical in visited:
                continue
            current_domain = domain_of(canonical).removeprefix("www.")
            if not is_same_or_subdomain(current_domain, parent_domain) and not is_same_or_subdomain(parent_domain, current_domain):
                continue
            visited.add(canonical)
            result = await self.fetch(canonical)
            logs.append(result)
            if not result.html:
                continue
            extracted = extract_page(result.html, result.url, country=country, keywords=keywords)
            pages.append((result.url, extracted))

            for href in extracted.relevant_links:
                target = canonical_url(href, result.url)
                if not target or target in visited:
                    continue
                target_domain = domain_of(target).removeprefix("www.")
                if is_same_or_subdomain(target_domain, parent_domain) or is_same_or_subdomain(parent_domain, target_domain):
                    queue.append(target)

            if self.settings.keep_page_snapshots:
                safe_name = parent_domain.replace(".", "_") + f"_{len(pages)}.html"
                path = Path(self.settings.snapshot_directory) / safe_name
                path.write_text(result.html, encoding="utf-8")

        organization = next((page.organization for _, page in pages if page.organization), None)
        emails = sorted({value for _, page in pages for value in page.emails})
        phones = sorted({value for _, page in pages for value in page.phones})
        whatsapp = sorted({value for _, page in pages for value in page.whatsapp})
        address = next((page.address for _, page in pages if page.address), None)
        city = next((page.city for _, page in pages if page.city), None)
        specialties = sorted({value for _, page in pages for value in page.specialties})
        source_url = next((url for url, page in pages if page.emails or page.phones), normalized_start)

        return OrganizationCandidate(
            organization=organization,
            website=root_url(source_url),
            domain=parent_domain,
            source_url=source_url,
            emails=emails,
            phones=phones,
            whatsapp=whatsapp,
            address=address,
            city=city,
            specialties=specialties,
            pages_crawled=len(pages),
            logs=logs,
        )
