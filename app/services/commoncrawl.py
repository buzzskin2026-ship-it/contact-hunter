from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.services.normalizer import canonical_url, domain_of


_CONTACT_HINTS = (
    "contact",
    "contacts",
    "contact-us",
    "contatti",
    "contatto",
    "kontakt",
    "kontakte",
    "impressum",
    "legal-notice",
    "about",
    "chi-siamo",
    "sedi",
    "locations",
    "clinics",
    "offices",
    "team",
    "staff",
    "email",
)


@dataclass(frozen=True)
class ArchivedUrl:
    url: str
    timestamp: str | None = None
    mime: str | None = None
    status: str | None = None


class CommonCrawlUrlProvider:
    """Use Common Crawl's public CDX index to discover useful public URLs.

    Only URL metadata is queried. Contact Hunter then requests the current live page,
    respecting its normal safety, robots and rate-limit rules.
    """

    collection_list_url = "https://index.commoncrawl.org/collinfo.json"

    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.common_crawl_enabled
        self.timeout = max(settings.crawler_timeout_seconds, 30)
        self.delay = settings.common_crawl_delay_seconds
        self.max_urls_per_domain = settings.common_crawl_urls_per_domain
        self.user_agent = settings.crawler_user_agent
        self._collection_api: str | None = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self.enabled

    async def _latest_collection_api(self) -> str | None:
        if self._collection_api:
            return self._collection_api
        async with self._lock:
            if self._collection_api:
                return self._collection_api
            headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.get(self.collection_list_url)
                response.raise_for_status()
                collections = response.json()
            if not collections:
                return None
            latest = collections[0]
            api = latest.get("cdx-api") or latest.get("cdx_api")
            if isinstance(api, str):
                self._collection_api = api.replace("http://", "https://", 1)
            return self._collection_api

    @staticmethod
    def _score(url: str) -> tuple[int, int, str]:
        parsed = urlparse(url)
        lowered = parsed.path.casefold()
        score = 0
        for position, hint in enumerate(_CONTACT_HINTS):
            if hint in lowered:
                score += 100 - position
        if lowered.endswith(".pdf"):
            score += 18
        if parsed.query:
            score -= 8
        depth = lowered.count("/")
        return (-score, depth, url)

    async def discover(self, domain: str) -> tuple[list[ArchivedUrl], str]:
        if not self.enabled:
            return [], "Common Crawl disabilitato"
        api = await self._latest_collection_api()
        if not api:
            return [], "Common Crawl: nessuna collezione disponibile"

        normalized_domain = domain_of(f"https://{domain}").removeprefix("www.")
        if not normalized_domain:
            return [], "Common Crawl: dominio non valido"

        params = {
            "url": f"{normalized_domain}/*",
            "output": "json",
            "filter": ["status:200", "mime:(text/html|application/pdf)"],
            "collapse": "urlkey",
            "pageSize": "200",
        }
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/x-ndjson, application/json, text/plain",
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:
            response = await client.get(api, params=params)
            response.raise_for_status()

        records: list[ArchivedUrl] = []
        seen: set[str] = set()
        for line in response.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw_url = payload.get("url")
            url = canonical_url(str(raw_url)) if raw_url else None
            if not url or url in seen:
                continue
            host = domain_of(url).removeprefix("www.")
            if host != normalized_domain and not host.endswith(f".{normalized_domain}"):
                continue
            lowered = url.casefold()
            if not any(hint in lowered for hint in _CONTACT_HINTS) and not lowered.endswith(".pdf"):
                continue
            seen.add(url)
            records.append(
                ArchivedUrl(
                    url=url,
                    timestamp=str(payload.get("timestamp") or "") or None,
                    mime=str(payload.get("mime") or "") or None,
                    status=str(payload.get("status") or "") or None,
                )
            )

        records.sort(key=lambda item: self._score(item.url))
        selected = records[: self.max_urls_per_domain]
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return selected, f"Common Crawl {normalized_domain}: {len(selected)} URL utili"
