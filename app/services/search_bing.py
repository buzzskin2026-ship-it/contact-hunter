from __future__ import annotations

import asyncio
from xml.etree import ElementTree

import httpx

from app.config import Settings
from app.services.normalizer import canonical_url, domain_of
from app.services.search import SearchHit


class BingRssSearchProvider:
    """Low-volume discovery fallback based on Bing's public RSS search output."""

    endpoint = "https://www.bing.com/search"

    def __init__(self, settings: Settings) -> None:
        self.timeout = settings.crawler_timeout_seconds
        self.user_agent = settings.crawler_user_agent
        self.enabled = settings.public_search_fallback_enabled
        self.delay = settings.public_search_delay_seconds

    @property
    def configured(self) -> bool:
        return self.enabled

    async def search(
        self,
        query: str,
        count: int = 10,
        country_code: str | None = None,
    ) -> list[SearchHit]:
        if not self.enabled:
            return []

        params: dict[str, str] = {
            "q": query,
            "format": "rss",
            "setlang": "it",
        }
        if country_code:
            params["cc"] = country_code.lower()

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.5",
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:
            response = await client.get(self.endpoint, params=params)
            response.raise_for_status()

        root = ElementTree.fromstring(response.content)
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for item in root.findall(".//item"):
            raw_url = (item.findtext("link") or "").strip()
            url = canonical_url(raw_url)
            if not url:
                continue
            host = domain_of(url).removeprefix("www.")
            if host in {"bing.com", "microsoft.com"} or host.endswith(".bing.com"):
                continue
            if url in seen:
                continue
            seen.add(url)
            hits.append(
                SearchHit(
                    url=url,
                    title=(item.findtext("title") or "").strip() or None,
                    description=(item.findtext("description") or "").strip() or None,
                )
            )
            if len(hits) >= min(max(count, 1), 15):
                break

        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return hits
