from __future__ import annotations

import pytest

from app.config import Settings
from app.services.crawler import PageResult
from app.services.fast_crawler import FastContactCrawler


@pytest.mark.asyncio
async def test_http_error_never_launches_playwright(monkeypatch):
    crawler = FastContactCrawler(Settings(playwright_enabled=True))
    calls = {"playwright": 0}

    async def fake_http(url: str):
        return PageResult(url, 500, None, "error", "HTTP 500")

    async def fake_playwright(url: str):
        calls["playwright"] += 1
        return PageResult(url, 200, "<html></html>", "playwright")

    monkeypatch.setattr(crawler, "_fetch_http", fake_http)
    monkeypatch.setattr(crawler, "_fetch_playwright", fake_playwright)
    try:
        result = await crawler.fetch("https://example.test/contact")
        assert result.status_code == 500
        assert calls["playwright"] == 0
    finally:
        await crawler.close()


@pytest.mark.asyncio
async def test_quick_pass_stops_after_first_public_email(monkeypatch):
    settings = Settings(
        playwright_enabled=False,
        crawler_quick_pages_per_domain=6,
        crawler_page_timeout_seconds=3,
        crawler_domain_timeout_seconds=10,
        crawler_sitemap_timeout_seconds=1,
    )
    crawler = FastContactCrawler(settings)
    calls: list[str] = []

    async def no_sitemap(base_root: str, parent_domain: str):
        return []

    async def fake_fetch(url: str):
        calls.append(url)
        return PageResult(
            url,
            200,
            "<html><head><title>Studio Test</title></head>"
            "<body>Contatti: info@studio-test.it</body></html>",
            "fetched",
        )

    monkeypatch.setattr(crawler, "_sitemap_urls", no_sitemap)
    monkeypatch.setattr(crawler, "fetch", fake_fetch)
    try:
        candidate = await crawler.crawl_domain("https://studio-test.it")
        assert candidate.emails == ["info@studio-test.it"]
        assert candidate.pages_crawled == 1
        assert len(calls) == 1
    finally:
        await crawler.close()


def test_quick_crawler_defaults_are_bounded_and_parallel():
    settings = Settings()
    assert settings.crawler_page_timeout_seconds < settings.crawler_domain_timeout_seconds
    assert settings.crawler_quick_pages_per_domain <= 6
    assert settings.crawler_concurrency >= 8
    assert settings.crawler_playwright_timeout_seconds <= settings.crawler_page_timeout_seconds
