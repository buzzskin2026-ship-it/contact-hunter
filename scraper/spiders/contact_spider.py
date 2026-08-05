from __future__ import annotations

from urllib.parse import urlparse

import scrapy

from app.services.extractor import extract_page
from app.services.normalizer import canonical_url, domain_of, is_same_or_subdomain


class ContactSpider(scrapy.Spider):
    name = "contacts"

    def __init__(self, start_urls: str = "", country: str = "", keywords: str = "", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.start_urls = [item.strip() for item in start_urls.split(",") if item.strip()]
        if not self.start_urls:
            raise ValueError("Passa -a start_urls=https://sito1.it,https://sito2.eu")
        self.country = country or None
        self.keywords = [item.strip() for item in keywords.split(",") if item.strip()]
        self.allowed_roots = {domain_of(url).removeprefix("www.") for url in self.start_urls}

    def parse(self, response: scrapy.http.Response):
        page = extract_page(response.text, response.url, country=self.country, keywords=self.keywords)
        if page.emails or page.phones:
            yield {
                "organization": page.organization,
                "website": f"{urlparse(response.url).scheme}://{urlparse(response.url).netloc}/",
                "source_url": response.url,
                "emails": page.emails,
                "phones": page.phones,
                "whatsapp": page.whatsapp,
                "address": page.address,
                "city": page.city,
                "specialties": page.specialties,
            }
        for href in page.relevant_links:
            target = canonical_url(href, response.url)
            if not target:
                continue
            host = domain_of(target).removeprefix("www.")
            if any(is_same_or_subdomain(host, root) or is_same_or_subdomain(root, host) for root in self.allowed_roots):
                yield response.follow(target, callback=self.parse)
