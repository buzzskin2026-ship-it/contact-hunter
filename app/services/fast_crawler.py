from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from urllib.parse import urljoin

from app.services.crawler import ContactCrawler, OrganizationCandidate, PageResult
from app.services.extractor import ExtractedPage, extract_page
from app.services.normalizer import (
    canonical_url,
    domain_of,
    is_same_or_subdomain,
    root_url,
)

_DOCUMENT_ACTIONS = {"pdf", "csv", "spreadsheet", "json", "xml"}
_FAST_PATHS = (
    "contatti",
    "contact",
    "contact-us",
    "contatto",
    "kontakt",
    "impressum",
    "sedi",
    "locations",
)


class FastContactCrawler(ContactCrawler):
    """Quick-first crawler designed for large discovery queues.

    The original crawler could spend many minutes on a single broken domain because
    every failed path could trigger a second Chromium request. This implementation
    gives every page and every domain a strict time budget, uses Playwright at most
    once per domain, and stops after the first useful public email page. That lets a
    national campaign touch all discovered domains before deep crawling any one site.
    """

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._playwright_domains: set[str] = set()
        self._playwright_lock = asyncio.Lock()

    async def fetch(self, url: str) -> PageResult:
        result = await self._fetch_http(url)
        if result.action in _DOCUMENT_ACTIONS:
            return result
        if result.html and len(result.html) >= 500:
            return result

        # Never launch a browser for network errors, 4xx/5xx responses, blocked URLs,
        # empty responses or unsupported documents. Chromium is only useful for a
        # successful but very small HTML shell that is probably rendered by JavaScript.
        if (
            result.action in {"blocked", "robots_blocked", "ignored", "error"}
            or result.status_code is None
            or result.status_code >= 400
            or not result.html
        ):
            return result

        domain = domain_of(result.url).removeprefix("www.")
        if not domain or domain in self._playwright_domains:
            return result
        self._playwright_domains.add(domain)

        try:
            async with self._playwright_lock:
                fallback = await asyncio.wait_for(
                    self._fetch_playwright(result.url),
                    timeout=self.settings.crawler_playwright_timeout_seconds,
                )
        except TimeoutError:
            return PageResult(
                result.url,
                result.status_code,
                result.html,
                "fetched",
                "Playwright interrotto dal timeout rapido",
            )
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
        logs: list[PageResult] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.crawler_domain_timeout_seconds

        sitemap_urls: list[str] = []
        try:
            sitemap_urls = await asyncio.wait_for(
                self._sitemap_urls(base_root, parent_domain),
                timeout=min(
                    self.settings.crawler_sitemap_timeout_seconds,
                    self.settings.crawler_domain_timeout_seconds,
                ),
            )
        except TimeoutError:
            logs.append(
                PageResult(
                    base_root,
                    None,
                    None,
                    "sitemap_timeout",
                    "Sitemap saltata dopo il timeout rapido",
                )
            )
        except Exception as exc:
            logs.append(PageResult(base_root, None, None, "sitemap_error", str(exc)))

        seeds = [normalized_start, base_root]
        seeds.extend(urljoin(f"{base_root}/", path) for path in _FAST_PATHS)
        seeds.extend(sitemap_urls)
        queue: deque[str] = deque(dict.fromkeys(seeds))
        visited: set[str] = set()
        pages: list[tuple[str, ExtractedPage]] = []
        quick_pages = self.settings.crawler_quick_pages_per_domain
        attempt_limit = min(max(quick_pages * 3, 12), 36)

        while queue and len(pages) < quick_pages and len(visited) < attempt_limit:
            remaining = deadline - loop.time()
            if remaining <= 0:
                logs.append(
                    PageResult(
                        base_root,
                        None,
                        None,
                        "domain_timeout",
                        f"Dominio interrotto dopo {self.settings.crawler_domain_timeout_seconds:.0f}s",
                    )
                )
                break

            url = queue.popleft()
            canonical = canonical_url(url)
            if not canonical or canonical in visited:
                continue
            current_domain = domain_of(canonical).removeprefix("www.")
            if not (
                is_same_or_subdomain(current_domain, parent_domain)
                or is_same_or_subdomain(parent_domain, current_domain)
            ):
                continue
            visited.add(canonical)

            try:
                result = await asyncio.wait_for(
                    self.fetch(canonical),
                    timeout=min(self.settings.crawler_page_timeout_seconds, remaining),
                )
            except TimeoutError:
                logs.append(
                    PageResult(
                        canonical,
                        None,
                        None,
                        "page_timeout",
                        f"Pagina saltata dopo {self.settings.crawler_page_timeout_seconds:.0f}s",
                    )
                )
                continue
            except Exception as exc:
                logs.append(PageResult(canonical, None, None, "error", str(exc)))
                continue

            logs.append(result)
            if not result.html:
                continue

            extracted = extract_page(
                result.html,
                result.url,
                country=country,
                keywords=keywords,
            )
            pages.append((result.url, extracted))

            # Contact links found in the current page have priority over generic seeds.
            new_links: list[str] = []
            for href in extracted.relevant_links:
                target = canonical_url(href, result.url)
                if not target or target in visited:
                    continue
                target_domain = domain_of(target).removeprefix("www.")
                if (
                    is_same_or_subdomain(target_domain, parent_domain)
                    or is_same_or_subdomain(parent_domain, target_domain)
                ):
                    new_links.append(target)
            for target in reversed(new_links[:8]):
                queue.appendleft(target)

            if self.settings.keep_page_snapshots:
                safe_name = parent_domain.replace(".", "_") + f"_{len(pages)}.html"
                path = Path(self.settings.snapshot_directory) / safe_name
                path.write_text(result.html, encoding="utf-8")

            # A public document is parsed in full. For websites, the first page with
            # an email is enough for the fast national pass; other domains must not wait.
            if extracted.emails:
                break

        organization = next((page.organization for _, page in pages if page.organization), None)
        emails = sorted({value for _, page in pages for value in page.emails})
        phones = sorted({value for _, page in pages for value in page.phones})
        whatsapp = sorted({value for _, page in pages for value in page.whatsapp})
        address = next((page.address for _, page in pages if page.address), None)
        city = next((page.city for _, page in pages if page.city), None)
        specialties = sorted({value for _, page in pages for value in page.specialties})
        source_url = next(
            (url for url, page in pages if page.emails or page.phones or page.whatsapp),
            normalized_start,
        )

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
