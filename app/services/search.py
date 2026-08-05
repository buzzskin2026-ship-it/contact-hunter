from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import Settings
from app.services.normalizer import canonical_url


@dataclass
class SearchHit:
    url: str
    title: str | None = None
    description: str | None = None


class BraveSearchProvider:
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, settings: Settings) -> None:
        self.api_key = settings.brave_search_api_key
        self.timeout = settings.crawler_timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def search(self, query: str, count: int = 20, country_code: str | None = None) -> list[SearchHit]:
        if not self.api_key:
            return []
        params: dict[str, str | int] = {"q": query, "count": min(max(count, 1), 20), "safesearch": "moderate"}
        if country_code:
            params["country"] = country_code.lower()
        headers = {"X-Subscription-Token": self.api_key, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(self.endpoint, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        results = payload.get("web", {}).get("results", [])
        hits: list[SearchHit] = []
        for result in results:
            url = canonical_url(str(result.get("url", "")))
            if url:
                hits.append(SearchHit(url=url, title=result.get("title"), description=result.get("description")))
        return hits


def build_queries(sector: str, countries: list[str], cities: list[str], keywords: list[str]) -> list[str]:
    locations = cities or countries or [""]
    extra = " ".join(keywords[:5])
    queries: list[str] = []
    for location in locations:
        base = " ".join(part for part in (sector, location, extra) if part).strip()
        queries.extend([
            f'{base} contact email official website',
            f'{base} contacts phone',
            f'{base} "@"',
        ])
    return list(dict.fromkeys(queries))
