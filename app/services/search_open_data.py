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
    """Discover public datasets through data.europa.eu's read API.

    A value of zero for max_queries or max_resources means no Contact Hunter total
    ceiling. Results are still requested in pages so memory and network pressure stay
    bounded during each individual request.
    """

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
    def _queries(
        sector: str,
        countries: list[str],
        keywords: list[str],
    ) -> list[str]:
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
        base.extend(keywords)
        queries: list[str] = []
        for term in base:
            cleaned = " ".join(str(term).split())
            if not cleaned:
                continue
            queries.append(cleaned)
            for country in countries:
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
        if any(
            token in hint
            for token in (
                "pdf",
                "csv",
                "tsv",
                "excel",
                "xlsx",
                "json",
                "xml",
                "html",
            )
        ):
            return url
        return None

    @staticmethod
    def _resources_from_dataset(dataset: dict) -> list[OpenDataResource]:
        dataset_id = str(dataset.get("id") or dataset.get("name") or "") or None
        title = dataset.get("title")
        if isinstance(title, dict):
            title = title.get("it") or title.get("en") or next(
                iter(title.values()),
                None,
            )
        resources = dataset.get("resources") or dataset.get("distributions") or []
        found: list[OpenDataResource] = []
        for resource in resources:
            raw_urls: list[str] = []
            for key in (
                "url",
                "access_url",
                "download_url",
                "accessURL",
                "downloadURL",
            ):
                value = resource.get(key)
                if isinstance(value, str):
                    raw_urls.append(value)
                elif isinstance(value, list):
                    raw_urls.extend(str(item) for item in value if item)
            format_hint = (
                resource.get("format")
                or resource.get("media_type")
                or resource.get("mimetype")
            )
            if isinstance(format_hint, dict):
                format_hint = format_hint.get("label") or format_hint.get("id")
            for raw in raw_urls:
                url = EuropeanOpenDataProvider._usable_url(
                    raw,
                    str(format_hint or ""),
                )
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

    def _resource_capacity_reached(self, count: int) -> bool:
        return self.max_resources > 0 and count >= self.max_resources

    async def search(
        self,
        sector: str,
        countries: list[str],
        keywords: list[str],
    ) -> tuple[list[OpenDataResource], str]:
        if not self.enabled:
            return [], "Open data europei disabilitati"

        all_queries = self._queries(sector, countries, keywords)
        queries = (
            all_queries
            if self.max_queries == 0
            else all_queries[: self.max_queries]
        )
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
                start = 0
                while not self._resource_capacity_reached(len(resources)):
                    try:
                        response = await client.get(
                            self.endpoint,
                            params={
                                "q": query,
                                "rows": self.datasets_per_query,
                                "start": start,
                            },
                        )
                        response.raise_for_status()
                        payload = response.json()
                    except Exception:
                        break
                    result = payload.get("result") or payload
                    datasets = (
                        result.get("results")
                        if isinstance(result, dict)
                        else []
                    )
                    if not isinstance(datasets, list) or not datasets:
                        break
                    datasets_seen += len(datasets)
                    for dataset in datasets:
                        if not isinstance(dataset, dict):
                            continue
                        for resource in self._resources_from_dataset(dataset):
                            if resource.url in seen:
                                continue
                            seen.add(resource.url)
                            resources.append(resource)
                            if self._resource_capacity_reached(len(resources)):
                                break
                        if self._resource_capacity_reached(len(resources)):
                            break
                    if len(datasets) < self.datasets_per_query:
                        break
                    start += len(datasets)
                    if self.delay > 0:
                        await asyncio.sleep(self.delay)
                if self._resource_capacity_reached(len(resources)):
                    break
                if self.delay > 0:
                    await asyncio.sleep(self.delay)

        ceiling = (
            str(self.max_resources)
            if self.max_resources > 0
            else "nessun tetto software"
        )
        return resources, (
            f"Open data UE: {len(queries)} query, {datasets_seen} dataset esaminati, "
            f"{len(resources)} risorse pubbliche utilizzabili su {ceiling}"
        )
