from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.services.normalizer import canonical_url, normalize_email


COUNTRY_CODES = {
    "albania": "AL",
    "austria": "AT",
    "belgio": "BE",
    "belgium": "BE",
    "bosnia ed erzegovina": "BA",
    "bosnia and herzegovina": "BA",
    "bulgaria": "BG",
    "croazia": "HR",
    "croatia": "HR",
    "cipro": "CY",
    "cyprus": "CY",
    "repubblica ceca": "CZ",
    "czechia": "CZ",
    "danimarca": "DK",
    "denmark": "DK",
    "estonia": "EE",
    "finlandia": "FI",
    "finland": "FI",
    "francia": "FR",
    "france": "FR",
    "germania": "DE",
    "germany": "DE",
    "grecia": "GR",
    "greece": "GR",
    "ungheria": "HU",
    "hungary": "HU",
    "islanda": "IS",
    "iceland": "IS",
    "irlanda": "IE",
    "ireland": "IE",
    "italia": "IT",
    "italy": "IT",
    "kosovo": "XK",
    "lettonia": "LV",
    "latvia": "LV",
    "liechtenstein": "LI",
    "lituania": "LT",
    "lithuania": "LT",
    "lussemburgo": "LU",
    "luxembourg": "LU",
    "malta": "MT",
    "moldova": "MD",
    "moldavia": "MD",
    "montenegro": "ME",
    "paesi bassi": "NL",
    "olanda": "NL",
    "netherlands": "NL",
    "macedonia del nord": "MK",
    "north macedonia": "MK",
    "norvegia": "NO",
    "norway": "NO",
    "polonia": "PL",
    "poland": "PL",
    "portogallo": "PT",
    "portugal": "PT",
    "romania": "RO",
    "san marino": "SM",
    "serbia": "RS",
    "slovacchia": "SK",
    "slovakia": "SK",
    "slovenia": "SI",
    "spagna": "ES",
    "spain": "ES",
    "svezia": "SE",
    "sweden": "SE",
    "svizzera": "CH",
    "switzerland": "CH",
    "turchia": "TR",
    "turkey": "TR",
    "ucraina": "UA",
    "ukraine": "UA",
    "regno unito": "GB",
    "united kingdom": "GB",
}


@dataclass
class OsmContactRecord:
    source_url: str
    organization: str
    category: str
    country: str
    city: str | None
    address: str | None
    website: str | None
    emails: list[str]
    phones: list[str]


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).strip().lower()


def country_code(value: str) -> str | None:
    stripped = value.strip()
    if len(stripped) == 2 and stripped.isalpha():
        return stripped.upper()
    return COUNTRY_CODES.get(_fold(stripped))


def supports_dental_sector(sector: str) -> bool:
    folded = _fold(sector)
    return any(token in folded for token in ("dent", "odont", "zahnarzt", "stomatolog"))


def _split_values(value: str | None) -> list[str]:
    if not value:
        return []
    normalized = value.replace("|", ";").replace(",", ";")
    return [part.strip() for part in normalized.split(";") if part.strip()]


def _website(tags: dict[str, str]) -> str | None:
    raw = tags.get("contact:website") or tags.get("website") or tags.get("url")
    if not raw:
        return None
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return canonical_url(raw)


def _address(tags: dict[str, str]) -> str | None:
    street = " ".join(
        part for part in (tags.get("addr:street"), tags.get("addr:housenumber")) if part
    ).strip()
    locality = " ".join(
        part for part in (tags.get("addr:postcode"), tags.get("addr:city")) if part
    ).strip()
    parts = [part for part in (street, locality) if part]
    return ", ".join(parts) or None


class OpenStreetMapDentalProvider:
    """Discover dental practices and dental laboratories with public contact fields.

    OpenStreetMap is used only as a discovery source. Official websites discovered here
    are subsequently crawled by Contact Hunter and remain the preferred final source.
    """

    endpoints = (
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    )

    def __init__(self, settings: Settings) -> None:
        self.timeout = max(settings.crawler_timeout_seconds, 35)
        self.user_agent = settings.crawler_user_agent

    def configured_for(self, sector: str, countries: list[str]) -> bool:
        return supports_dental_sector(sector) and any(country_code(country) for country in countries)

    @staticmethod
    def _query(code: str, limit: int) -> str:
        contact_keys = "^(website|contact:website|url|email|contact:email|phone|contact:phone)$"
        return f'''[out:json][timeout:35];
area["ISO3166-1"="{code}"]->.searchArea;
(
  nwr["amenity"="dentist"][~"{contact_keys}"~"."](area.searchArea);
  nwr["healthcare"="dentist"][~"{contact_keys}"~"."](area.searchArea);
  nwr["craft"="dental_technician"][~"{contact_keys}"~"."](area.searchArea);
);
out tags center {limit};'''

    async def search_country(
        self,
        country: str,
        limit: int = 120,
    ) -> tuple[list[OsmContactRecord], str]:
        code = country_code(country)
        if not code:
            return [], f"OSM {country}: paese non riconosciuto"

        query = self._query(code, min(max(limit, 20), 200))
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        last_error: Exception | None = None
        payload: dict | None = None
        for endpoint in self.endpoints:
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    follow_redirects=True,
                    headers=headers,
                ) as client:
                    response = await client.post(endpoint, data={"data": query})
                    response.raise_for_status()
                    payload = response.json()
                break
            except Exception as exc:
                last_error = exc

        if payload is None:
            error_name = type(last_error).__name__ if last_error else "errore sconosciuto"
            return [], f"OSM {country}: {error_name}"

        records: list[OsmContactRecord] = []
        seen: set[tuple[str, str]] = set()
        for element in payload.get("elements", []):
            tags = element.get("tags") or {}
            website = _website(tags)
            emails = []
            for raw in _split_values(tags.get("contact:email") or tags.get("email")):
                email = normalize_email(raw)
                if email and email not in emails:
                    emails.append(email)
            phones = list(
                dict.fromkeys(
                    _split_values(tags.get("contact:phone") or tags.get("phone"))
                )
            )
            if not website and not emails and not phones:
                continue

            element_type = str(element.get("type", "node"))
            element_id = str(element.get("id", ""))
            source_url = f"https://www.openstreetmap.org/{element_type}/{element_id}"
            organization = (
                tags.get("name")
                or tags.get("operator")
                or tags.get("brand")
                or f"Struttura odontoiatrica OSM {element_id}"
            )
            category = (
                "Laboratorio odontotecnico"
                if tags.get("craft") == "dental_technician"
                else "Studio dentistico / clinica dentale"
            )
            city = tags.get("addr:city") or tags.get("addr:place") or tags.get("is_in:city")
            key = (website or "", organization.casefold())
            if key in seen:
                continue
            seen.add(key)
            records.append(
                OsmContactRecord(
                    source_url=source_url,
                    organization=organization[:300],
                    category=category,
                    country=country,
                    city=city,
                    address=_address(tags),
                    website=website,
                    emails=emails,
                    phones=phones,
                )
            )

        await asyncio.sleep(0.4)
        return records, f"OSM {country}: {len(records)} strutture con contatti"
