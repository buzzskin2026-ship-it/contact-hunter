from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field

try:
    import phonenumbers
except ImportError:
    phonenumbers = None

from bs4 import BeautifulSoup

from app.services.normalizer import domain_of, is_free_email, normalize_email

CONTACT_PATH_HINTS = (
    "contact", "contacts", "kontakt", "contatti", "contatto", "contato", "contacto",
    "impressum", "legal", "about", "chi-siamo", "uber-uns", "sobre", "nous-contacter",
    "reach-us", "find-us", "locations", "sedi", "clinics", "offices",
)

COUNTRY_REGIONS = {
    "italy": "IT", "italia": "IT", "france": "FR", "francia": "FR",
    "germany": "DE", "germania": "DE", "spain": "ES", "spagna": "ES",
    "portugal": "PT", "portogallo": "PT", "belgium": "BE", "belgio": "BE",
    "netherlands": "NL", "paesi bassi": "NL", "olanda": "NL", "austria": "AT",
    "switzerland": "CH", "svizzera": "CH", "ireland": "IE", "irlanda": "IE",
    "united kingdom": "GB", "poland": "PL", "polonia": "PL", "greece": "GR",
    "grecia": "GR", "denmark": "DK", "sweden": "SE", "norway": "NO",
    "finland": "FI", "romania": "RO", "czechia": "CZ", "slovakia": "SK",
    "croatia": "HR",
}

GENERIC_TITLES = {"home", "homepage", "contact", "contacts", "welcome", "index"}
EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}", flags=re.I)


@dataclass
class ExtractedPage:
    organization: str | None = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    whatsapp: list[str] = field(default_factory=list)
    address: str | None = None
    city: str | None = None
    specialties: list[str] = field(default_factory=list)
    relevant_links: list[str] = field(default_factory=list)
    visible_text: str = ""


def _iter_jsonld_objects(value: object):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _iter_jsonld_objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_jsonld_objects(nested)


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = " ".join(html_lib.unescape(value).split())
    return cleaned[:500] if cleaned else None


def _deobfuscate(value: str) -> str:
    value = html_lib.unescape(value)
    value = re.sub(r"\s*(?:\[at\]|\(at\)|\{at\}| at )\s*", "@", value, flags=re.I)
    value = re.sub(r"\s*(?:\[dot\]|\(dot\)|\{dot\}| dot )\s*", ".", value, flags=re.I)
    return value


def _decode_cfemail(value: str) -> str | None:
    try:
        data = bytes.fromhex(value)
        if len(data) < 2:
            return None
        key = data[0]
        decoded = "".join(chr(byte ^ key) for byte in data[1:])
        return decoded
    except (ValueError, UnicodeError):
        return None


def _organization_name(soup: BeautifulSoup, json_objects: list[dict]) -> str | None:
    for obj in json_objects:
        object_type = obj.get("@type")
        types = object_type if isinstance(object_type, list) else [object_type]
        if any(t in {"Organization", "LocalBusiness", "Dentist", "MedicalBusiness", "ProfessionalService"} for t in types):
            name = _clean_text(obj.get("name"))
            if name:
                return name
    og = soup.select_one('meta[property="og:site_name"]')
    if og and og.get("content"):
        return _clean_text(str(og.get("content")))
    h1 = soup.find("h1")
    if h1:
        value = _clean_text(h1.get_text(" ", strip=True))
        if value and value.lower() not in GENERIC_TITLES:
            return value
    if soup.title:
        title = re.split(r"[|–—-]", soup.title.get_text(" ", strip=True))[0]
        value = _clean_text(title)
        if value and value.lower() not in GENERIC_TITLES:
            return value
    return None


def _extract_address(json_objects: list[dict]) -> tuple[str | None, str | None]:
    for obj in json_objects:
        address = obj.get("address")
        if isinstance(address, dict):
            parts = [address.get(key) for key in ("streetAddress", "postalCode", "addressLocality", "addressRegion", "addressCountry")]
            text = _clean_text(", ".join(str(part) for part in parts if part))
            city = _clean_text(str(address.get("addressLocality") or ""))
            if text:
                return text, city
        elif isinstance(address, str):
            return _clean_text(address), None
    return None, None


def extract_page(html: str, url: str, country: str | None = None, keywords: list[str] | None = None) -> ExtractedPage:
    raw_source = _deobfuscate(html)
    soup = BeautifulSoup(html, "lxml")

    json_objects: list[dict] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            parsed = json.loads(script.get_text(strip=True))
            json_objects.extend(obj for obj in _iter_jsonld_objects(parsed) if isinstance(obj, dict))
        except (json.JSONDecodeError, TypeError):
            continue

    raw_emails: set[str] = set(EMAIL_PATTERN.findall(raw_source))
    for link in soup.select('a[href^="mailto:"]'):
        raw_emails.add(str(link.get("href", ""))[7:].split("?", 1)[0])
    for element in soup.select("[data-cfemail]"):
        decoded = _decode_cfemail(str(element.get("data-cfemail", "")))
        if decoded:
            raw_emails.add(decoded)
    for element in soup.select("[data-email], [data-mail]"):
        value = element.get("data-email") or element.get("data-mail")
        if value:
            raw_emails.update(EMAIL_PATTERN.findall(_deobfuscate(str(value))))
    for element in soup.select("[data-user][data-domain]"):
        raw_emails.add(f"{element.get('data-user')}@{element.get('data-domain')}")
    for obj in json_objects:
        email = obj.get("email")
        if isinstance(email, str):
            raw_emails.add(email.replace("mailto:", ""))

    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        if tag.name != "script" or tag.get("type") != "application/ld+json":
            tag.decompose()

    visible_text = " ".join(soup.get_text(" ", strip=True).split())
    raw_emails.update(EMAIL_PATTERN.findall(_deobfuscate(visible_text)))
    emails = sorted({email for value in raw_emails if (email := normalize_email(value))})

    region = COUNTRY_REGIONS.get((country or "").lower())
    phones: set[str] = set()
    whatsapp: set[str] = set()
    tel_values = [str(link.get("href", ""))[4:].split("?", 1)[0] for link in soup.select('a[href^="tel:"]')]
    for obj in json_objects:
        telephone = obj.get("telephone")
        if isinstance(telephone, str):
            tel_values.append(telephone)
        elif isinstance(telephone, list):
            tel_values.extend(str(value) for value in telephone if value)
    if phonenumbers is not None:
        for value in tel_values:
            try:
                number = phonenumbers.parse(value, region)
                if phonenumbers.is_possible_number(number):
                    phones.add(phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.INTERNATIONAL))
            except phonenumbers.NumberParseException:
                pass
        for match in phonenumbers.PhoneNumberMatcher(visible_text, region):
            if phonenumbers.is_possible_number(match.number):
                phones.add(phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.INTERNATIONAL))
    else:
        phone_pattern = re.compile(r"(?<!\d)(?:\+?\d[\d .()/-]{6,}\d)")
        for value in [*tel_values, *phone_pattern.findall(visible_text)]:
            cleaned = re.sub(r"[^+\d]", "", value)
            digits = re.sub(r"\D", "", cleaned)
            if 7 <= len(digits) <= 15:
                phones.add(cleaned)
    for link in soup.select('a[href*="wa.me"], a[href*="whatsapp.com"]'):
        digits = re.sub(r"\D", "", str(link.get("href", "")))
        if 7 <= len(digits) <= 15:
            whatsapp.add("+" + digits)

    relevant_links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href"))
        label = (anchor.get_text(" ", strip=True) + " " + href).lower()
        if any(hint in label for hint in CONTACT_PATH_HINTS):
            relevant_links.append(href)

    address, city = _extract_address(json_objects)
    requested_keywords = [item.strip() for item in (keywords or []) if item.strip()]
    specialties = sorted({item for item in requested_keywords if item.lower() in visible_text.lower()})

    return ExtractedPage(
        organization=_organization_name(soup, json_objects),
        emails=emails,
        phones=sorted(phones),
        whatsapp=sorted(whatsapp),
        address=address,
        city=city,
        specialties=specialties,
        relevant_links=relevant_links,
        visible_text=visible_text[:20_000],
    )


def reliability_for(emails: list[str], website: str) -> str:
    if not emails:
        return "low"
    website_domain = domain_of(website).removeprefix("www.")
    email_domains = [email.rsplit("@", 1)[1].removeprefix("www.") for email in emails]
    if any(domain == website_domain or domain.endswith("." + website_domain) for domain in email_domains):
        return "high"
    if all(is_free_email(email) for email in emails):
        return "medium"
    return "medium"
