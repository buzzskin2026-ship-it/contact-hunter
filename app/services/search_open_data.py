from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.services.normalizer import canonical_url


@dataclass(frozen=True)
class OpenDataResource:
    url: str
    title: str | None = None
    format: str | None = None
    dataset_id: str | None = None


class EuropeanOpenDataProvider:
    """Discover public datasets and documents through data.europa.eu's read API."""

    endpoint = "https://data.europa.eu/api/hub/search/ckan/package_search"
    allowed_extensions = (
        ".pdf",
        ".csv",
        ".tsv",
        ".xlsx",
        ".json",
        ".xml",
        ".html",
        ".htm",
    )

    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.open_data_enabled
        self.timeout = max(settings.crawler_timeout_seconds, 30)
        self.max_queries = settings.open_data_max_queries
        self.datasets_per_query = settings.open_data_datasets_per_query
        self.max_resources = settings.open_data_max_resources
        self.delay = settings.open_data_delay_seconds
        self.user_agent = settings.crawler_user_agent

    @property
    def configured(self) -> bool:
        return self.enabled

    @staticmethod
    def _queries(sector: str, countries: list[str], keywords: list[str]) -> list[str]:
        base = [
            sector,
            "odontoiatri",
            "dentisti",
            "studi dentistici",
            "strutture odontoiatriche",
            "cliniche dentali",
            "laboratori odontotecnici",
            "albo odontoiatri",
            "dental clinics",
            "dentists register",
        ]
        base.extend(keywords[:10])
        queries: list[str] = []
        for term in base:
            cleaned = " ".join(str(term).split())
            if not cleaned:
                continue
            queries.append(cleaned)
            for country in countries[:10]:
                queries.append(f"{cleaned} {country}")
        return list(dict.fromkeys(queries))

    @classmethod
    def _usable_url(cls, raw: str, format_hint: str | None) -> str | None:
        url = canonical_url(raw)
        if not url:
            return None
        path = urlparse(url).path.casefold()
        hint = (format_hint or "").casefold()
        if path.endswith(cls.allowed_extensions):
            return url
        if any(token in hint for token in ("pdf", "csv", "tsv", "excel", "xlsx", "json", "xml", "html")):
            return url
        return None

    @staticmethod
    def _resources_from_dataset(dataset: dict) -> list[OpenDataResource]:
        dataset_id = str(dataset.get("id") or dataset.get("name") or "") or None
        title = dataset.get("title")
        if isinstance(title, dict):
            title = title.get("it") or title.get("en") or next(iter(title.values()), None)
        resources = dataset.get("resources") or dataset.get("distributions") or []
        found: list[OpenDataResource] = []
        for resource in resources:
            raw_urls: list[str] = []
            for key in ("url", "access_url", "download_url", "accessURL", "downloadURL"):
                value = resource.get(key)
                if isinstance(value, str):
                    raw_urls.append(value)
                elif isinstance(value, list):
                    raw_urls.extend(str(item) for item in value if item)
            format_hint = resource.get("format") or resource.get("media_type") or resource.get("mimetype")
            if isinstance(format_hint, dict):
                format_hint = format_hint.get("label") or format_hint.get("id")
            for raw in raw_urls:
                url = EuropeanOpenDataProvider._usable_url(raw, str(format_hint or ""))
                if url:
                    found.append(
                        OpenDataResource(
                            url=url,
                            title=str(resource.get("name") or title or "") or None,
                            format=str(format_hint or "") or None,
                            dataset_id=dataset_id,
                        )
                    )
        return found

    async def search(
        self,
        sector: str,
        countries: list[str],
        keywords: list[str],
    ) -> tuple[list[OpenDataResource], str]:
        if not self.enabled:
            return [], "Open data europei disabilitati"

        queries = self._queries(sector, countries, keywords)[: self.max_queries]
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        resources: list[OpenDataResource] = []
        seen: set[str] = set()
        datasets_seen = 0

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:
            for query in queries:
                try:
                    response = await client.get(
                        self.endpoint,
                        params={"q": query, "rows": self.datasets_per_query, "start": 0},
                    )
                    response.raise_for_status()
                    payload = response.json()
                except Exception:
                    continue
                result = payload.get("result") or payload
                datasets = result.get("results") if isinstance(result, dict) else []
                if not isinstance(datasets, list):
                    continue
                datasets_seen += len(datasets)
                for dataset in datasets:
                    if not isinstance(dataset, dict):
                        continue
                    for resource in self._resources_from_dataset(dataset):
                        if resource.url in seen:
                            continue
                        seen.add(resource.url)
                        resources.append(resource)
                        if len(resources) >= self.max_resources:
                            break
                    if len(resources) >= self.max_resources:
                        break
                if len(resources) >= self.max_resources:
                    break
                if self.delay > 0:
                    await asyncio.sleep(self.delay)

        return resources, (
            f"Open data UE: {len(queries)} query, {datasets_seen} dataset esaminati, "
            f"{len(resources)} risorse pubbliche utilizzabili"
        )
