from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse, urlunparse

FREE_EMAIL_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "icloud.com", "gmx.com", "gmx.de", "aol.com", "proton.me", "protonmail.com",
    "libero.it", "virgilio.it", "t-online.de", "orange.fr", "wanadoo.fr",
}

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63})(?![\w.-])", re.I)


def normalize_email(value: str) -> str | None:
    value = value.strip().strip(".,;:()[]<>\"'").lower()
    if not EMAIL_RE.fullmatch(value):
        return None
    local, domain = value.rsplit("@", 1)
    if not local or domain.startswith("-") or ".." in value:
        return None
    blocked_suffixes = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
    if value.endswith(blocked_suffixes):
        return None
    return value


def canonical_url(value: str, base: str | None = None) -> str | None:
    raw = urljoin(base, value) if base else value
    parsed = urlparse(raw.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port
    netloc = host
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))


def root_url(value: str) -> str:
    parsed = urlparse(value)
    return f"{parsed.scheme}://{parsed.netloc}/"


def domain_of(value: str) -> str:
    return (urlparse(value).hostname or "").lower().rstrip(".")


def is_same_or_subdomain(host: str, parent: str) -> bool:
    return host == parent or host.endswith("." + parent)


def is_free_email(email: str) -> bool:
    return email.rsplit("@", 1)[-1].lower() in FREE_EMAIL_DOMAINS


def contact_fingerprint(domain: str, emails: list[str], phones: list[str]) -> str:
    stable = emails[0] if emails else (phones[0] if phones else domain)
    return hashlib.sha256(f"{domain}|{stable}".encode("utf-8")).hexdigest()


def host_is_public(host: str) -> bool:
    """Reject localhost/private/reserved targets to reduce SSRF risk."""
    lowered = host.lower().rstrip(".")
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".local"):
        return False
    try:
        addresses = socket.getaddrinfo(lowered, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if any((ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_reserved, ip.is_multicast, ip.is_unspecified)):
            return False
    return bool(addresses)


def url_is_allowed(url: str, blocked_domains: set[str]) -> tuple[bool, str | None]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        return False, "schema o host non valido"
    if any(is_same_or_subdomain(host, blocked) for blocked in blocked_domains):
        return False, "dominio in blacklist"
    if not host_is_public(host):
        return False, "host non pubblico o non risolvibile"
    return True, None
