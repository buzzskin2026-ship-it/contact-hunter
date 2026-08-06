from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.services.geo_partitions import build_place_queries
from app.services.normalizer import canonical_url
from app.services.search_osm import OsmContactRecord, country_code


@dataclass(frozen=True)
class GooglePlacesStats:
    queries_attempted: int
    pages_requested: int
    places_received: int
    places_unique: int
    websites_found: int
    phones_found: int


class GooglePlacesProvider:
    """Discover public business listings through the official Places API (New).

    Google Places does not return email addresses. Its role is to discover named
    activities, public phones and official websites; Contact Hunter then crawls the
    websites to locate publicly published professional email addresses.
    """

    endpoint = "https://places.googleapis.com/v1/places:searchText"
    field_mask = ",".join(
        (
            "places.id",
            "places.displayName",
            "places.formattedAddress",
            "places.addressComponents",
            "places.websiteUri",
            "places.internationalPhoneNumber",
            "places.nationalPhoneNumber",
            "places.googleMapsUri",
            "places.businessStatus",
            "places.types",
            "nextPageToken",
        )
    )

    def __init__(self, settings: Settings) -> None:
        self.api_key = settings.google_places_api_key
        self.enabled = settings.google_places_enabled
        self.timeout = max(settings.crawler_timeout_seconds, 30)
        self.max_queries = settings.google_places_max_queries
        self.max_pages_per_query = settings.google_places_max_pages_per_query
        self.max_places = settings.google_places_max_places
        self.concurrency = settings.google_places_concurrency
        self.delay = settings.google_places_delay_seconds
        self.user_agent = settings.crawler_user_agent

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.api_key)

    @staticmethod
    def _display_name(place: dict) -> str:
        value = place.get("displayName")
        if isinstance(value, dict):
            return str(value.get("text") or "").strip()
        return str(value or "").strip()

    @staticmethod
    def _city(place: dict) -> str | None:
        priorities = (
            "locality",
            "postal_town",
            "administrative_area_level_3",
            "administrative_area_level_2",
        )
        components = place.get("addressComponents") or []
        for wanted in priorities:
            for component in components:
                if wanted not in (component.get("types") or []):
                    continue
                value = component.get("longText") or component.get("shortText")
                if value:
                    return str(value).strip()
        return None

    @staticmethod
    def _phones(place: dict) -> list[str]:
        return list(
            dict.fromkeys(
                str(value).strip()
                for value in (
                    place.get("internationalPhoneNumber"),
                    place.get("nationalPhoneNumber"),
                )
                if value and str(value).strip()
            )
        )

    async def _query_pages(
        self,
        client: httpx.AsyncClient,
        text_query: str,
        country: str,
        location: str,
    ) -> tuple[list[OsmContactRecord], int]:
        records: list[OsmContactRecord] = []
        page_token: str | None = None
        pages = 0
        region = country_code(country)

        for _ in range(self.max_pages_per_query):
            body: dict[str, object] = {
                "textQuery": text_query,
                "pageSize": 20,
                "languageCode": "it" if region == "IT" else "en",
            }
            if region:
                body["regionCode"] = region
            if page_token:
                body["pageToken"] = page_token

            response = await client.post(self.endpoint, json=body)
            response.raise_for_status()
            payload = response.json()
            pages += 1

            for place in payload.get("places", []):
                if place.get("businessStatus") == "CLOSED_PERMANENTLY":
                    continue
                organization = self._display_name(place)
                if not organization:
                    continue
                website_raw = place.get("websiteUri")
                website = canonical_url(str(website_raw)) if website_raw else None
                maps_uri = str(place.get("googleMapsUri") or "").strip()
                place_id = str(place.get("id") or "").strip()
                source_url = maps_uri or (
                    f"https://places.googleapis.com/v1/places/{place_id}" if place_id else website or ""
                )
                if not source_url:
                    continue
                formatted_address = str(place.get("formattedAddress") or "").strip() or None
                types = set(place.get("types") or [])
                category = (
                    "Laboratorio odontotecnico"
                    if "dental_laboratory" in types or "laboratorio" in text_query.casefold()
                    else "Studio dentistico / clinica dentale"
                )
                records.append(
                    OsmContactRecord(
                        source_url=source_url,
                        organization=organization[:300],
                        category=category,
                        country=country or ("Italia" if region == "IT" else ""),
                        city=self._city(place) or location or None,
                        address=formatted_address,
                        website=website,
                        emails=[],
                        phones=self._phones(place),
                        source_type="google_places",
                        external_id=place_id or None,
                    )
                )

            page_token = payload.get("nextPageToken")
            if not page_token:
                break
            if self.delay > 0:
                await asyncio.sleep(self.delay)
        return records, pages

    async def search(
        self,
        sector: str,
        countries: list[str],
        cities: list[str],
        requested_max_places: int | None = None,
    ) -> tuple[list[OsmContactRecord], str]:
        if not self.configured:
            return [], "Google Places: chiave non configurata"

        queries = build_place_queries(
            sector,
            countries,
            cities,
            max_queries=self.max_queries,
        )
        max_places = min(requested_max_places or self.max_places, self.max_places)
        headers = {
            "X-Goog-Api-Key": str(self.api_key),
            "X-Goog-FieldMask": self.field_mask,
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        semaphore = asyncio.Semaphore(self.concurrency)
        records: list[OsmContactRecord] = []
        seen: set[str] = set()
        pages_requested = 0
        queries_attempted = 0
        batch_size = max(self.concurrency * 2, 4)

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:

            async def run_query(query: tuple[str, str, str]):
                async with semaphore:
                    return await self._query_pages(client, *query)

            for start in range(0, len(queries), batch_size):
                if len(records) >= max_places:
                    break
                batch = queries[start : start + batch_size]
                results = await asyncio.gather(
                    *(run_query(query) for query in batch),
                    return_exceptions=True,
                )
                queries_attempted += len(batch)
                for result in results:
                    if isinstance(result, Exception):
                        continue
                    found, pages = result
                    pages_requested += pages
                    for record in found:
                        key = record.external_id or "|".join(
                            (
                                record.organization.casefold(),
                                (record.address or "").casefold(),
                                record.website or "",
                            )
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        records.append(record)
                        if len(records) >= max_places:
                            break

        stats = GooglePlacesStats(
            queries_attempted=queries_attempted,
            pages_requested=pages_requested,
            places_received=len(seen),
            places_unique=len(records),
            websites_found=sum(bool(record.website) for record in records),
            phones_found=sum(bool(record.phones) for record in records),
        )
        detail = (
            "Google Places: "
            f"{stats.queries_attempted} query territoriali, "
            f"{stats.pages_requested} pagine API, "
            f"{stats.places_unique} attività uniche, "
            f"{stats.websites_found} siti e {stats.phones_found} telefoni"
        )
        return records, detail
