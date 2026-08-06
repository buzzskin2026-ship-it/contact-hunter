from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import Settings
from app.services.geo_partitions import discovery_locations
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
        params: dict[str, str | int] = {
            "q": query,
            "count": min(max(count, 1), 20),
            "safesearch": "moderate",
        }
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
                hits.append(
                    SearchHit(
                        url=url,
                        title=result.get("title"),
                        description=result.get("description"),
                    )
                )
        return hits


def _public_result_target(raw_href: str) -> str | None:
    """Extract the destination URL from a DuckDuckGo HTML result link."""
    href = raw_href.strip()
    if href.startswith("//"):
        href = f"https:{href}"
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        if target:
            return canonical_url(unquote(target))
        return None
    return canonical_url(href)


class PublicSearchProvider:
    """Low-volume public fallback used when no commercial search API is configured."""

    endpoint = "https://html.duckduckgo.com/html/"

    def __init__(self, settings: Settings) -> None:
        self.timeout = settings.crawler_timeout_seconds
        self.user_agent = settings.crawler_user_agent
        self.enabled = settings.public_search_fallback_enabled
        self.delay = settings.public_search_delay_seconds

    @property
    def configured(self) -> bool:
        return self.enabled

    async def search(self, query: str, count: int = 10, country_code: str | None = None) -> list[SearchHit]:
        if not self.enabled:
            return []
        params: dict[str, str] = {"q": query}
        if country_code:
            params["kl"] = f"{country_code.lower()}-it"
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:
            response = await client.get(self.endpoint, params=params)
            response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for result in soup.select(".result"):
            anchor = result.select_one("a.result__a") or result.select_one("a.result-link")
            if not anchor:
                continue
            target = _public_result_target(str(anchor.get("href", "")))
            if not target or target in seen:
                continue
            seen.add(target)
            snippet_node = result.select_one(".result__snippet")
            hits.append(
                SearchHit(
                    url=target,
                    title=anchor.get_text(" ", strip=True) or None,
                    description=snippet_node.get_text(" ", strip=True) if snippet_node else None,
                )
            )
            if len(hits) >= min(max(count, 1), 15):
                break
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return hits


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).strip().lower()


LOCAL_DENTAL_TERMS = {
    "italia": "studio dentistico clinica dentale laboratorio odontotecnico",
    "italy": "studio dentistico clinica dentale laboratorio odontotecnico",
    "francia": "cabinet dentaire clinique dentaire laboratoire dentaire",
    "france": "cabinet dentaire clinique dentaire laboratoire dentaire",
    "germania": "Zahnarztpraxis Zahnklinik Dentallabor",
    "germany": "Zahnarztpraxis Zahnklinik Dentallabor",
    "austria": "Zahnarztpraxis Zahnklinik Dentallabor",
    "spagna": "clinica dental dentista laboratorio dental",
    "spain": "clinica dental dentista laboratorio dental",
    "olanda": "tandartspraktijk tandheelkundige kliniek tandtechnisch laboratorium",
    "paesi bassi": "tandartspraktijk tandheelkundige kliniek tandtechnisch laboratorium",
    "netherlands": "tandartspraktijk tandheelkundige kliniek tandtechnisch laboratorium",
    "polonia": "klinika stomatologiczna gabinet dentystyczny laboratorium protetyczne",
    "poland": "klinika stomatologiczna gabinet dentystyczny laboratorium protetyczne",
    "grecia": "οδοντιατρειο οδοντιατρικη κλινικη οδοντοτεχνικο εργαστηριο",
    "greece": "οδοντιατρειο οδοντιατρικη κλινικη οδοντοτεχνικο εργαστηριο",
    "portogallo": "clinica dentaria dentista laboratorio de protese dentaria",
    "portugal": "clinica dentaria dentista laboratorio de protese dentaria",
    "svizzera": "Zahnarztpraxis cabinet dentaire studio dentistico Dentallabor",
    "switzerland": "Zahnarztpraxis cabinet dentaire studio dentistico Dentallabor",
    "belgio": "cabinet dentaire tandartspraktijk clinique dentaire tandlaboratorium",
    "belgium": "cabinet dentaire tandartspraktijk clinique dentaire tandlaboratorium",
    "irlanda": "dental practice dental clinic dental laboratory",
    "ireland": "dental practice dental clinic dental laboratory",
    "danimarca": "tandlægeklinik tandlæge tandteknisk laboratorium",
    "denmark": "tandlægeklinik tandlæge tandteknisk laboratorium",
    "svezia": "tandläkarklinik tandläkare tandtekniskt laboratorium",
    "sweden": "tandläkarklinik tandläkare tandtekniskt laboratorium",
    "norvegia": "tannklinikk tannlege tannteknisk laboratorium",
    "norway": "tannklinikk tannlege tannteknisk laboratorium",
}


def _is_dental(sector: str) -> bool:
    folded = _fold(sector)
    return any(token in folded for token in ("dent", "odont", "zahnarzt", "stomatolog"))


def build_queries(sector: str, countries: list[str], cities: list[str], keywords: list[str]) -> list[str]:
    extra = " ".join(keywords[:5])
    dental = _is_dental(sector)

    bases: list[str] = []
    for country, location in discovery_locations(countries, cities):
        local_sector = LOCAL_DENTAL_TERMS.get(_fold(country), sector) if dental else sector
        parts = [local_sector, location]
        if country and _fold(country) not in _fold(location):
            parts.append(country)
        if extra:
            parts.append(extra)
        bases.append(" ".join(part for part in parts if part).strip())

    suffixes = (
        "contact email official website",
        "contatti email sito ufficiale",
        "telefono email reception segreteria",
        "filetype:pdf email telefono elenco dentisti",
        "filetype:pdf albo odontoiatri contatti",
        "associazione dentisti elenco soci contatti email",
        "ordine medici albo odontoiatri elenco pdf",
        "directory cliniche studi dentistici email",
        "sedi locations reception segreteria email",
    )
    queries: list[str] = []
    for suffix in suffixes:
        for base in bases:
            queries.append(f"{base} {suffix}".strip())
    return list(dict.fromkeys(queries))
