from __future__ import annotations

import io

from openpyxl import Workbook

from app.config import Settings
from app.services.crawler import ContactCrawler
from app.services.extractor import extract_page
from app.services.jobs import _select_urls
from app.services.search_open_data import EuropeanOpenDataProvider


def test_url_selection_keeps_multiple_documents_and_drops_redundant_pages():
    urls = [
        "https://ordine.example/albo-roma.pdf",
        "https://ordine.example/albo-milano.pdf",
        "https://ordine.example/albo-napoli.xlsx",
        "https://ordine.example/pagina-a",
        "https://ordine.example/pagina-b",
        "https://ordine.example/pagina-c",
    ]
    selected = _select_urls(urls, max_urls=20)
    assert "https://ordine.example/albo-roma.pdf" in selected
    assert "https://ordine.example/albo-milano.pdf" in selected
    assert "https://ordine.example/albo-napoli.xlsx" in selected
    assert len([url for url in selected if "/pagina-" in url]) == 0
    assert len(selected) == 3


def test_open_data_dataset_parser_accepts_supported_distributions_only():
    dataset = {
        "id": "dentisti-italia",
        "title": {"it": "Elenco strutture odontoiatriche"},
        "resources": [
            {"name": "CSV", "url": "https://data.example/dentisti.csv", "format": "CSV"},
            {"name": "XLSX", "download_url": "https://data.example/dentisti.xlsx", "format": "XLSX"},
            {"name": "PDF", "accessURL": ["https://data.example/albo.pdf"], "format": "PDF"},
            {"name": "ZIP", "url": "https://data.example/archive.zip", "format": "ZIP"},
        ],
    }
    resources = EuropeanOpenDataProvider._resources_from_dataset(dataset)
    assert {resource.url for resource in resources} == {
        "https://data.example/dentisti.csv",
        "https://data.example/dentisti.xlsx",
        "https://data.example/albo.pdf",
    }
    assert all(resource.dataset_id == "dentisti-italia" for resource in resources)


def test_csv_document_becomes_extractable_contact_text():
    crawler = ContactCrawler(Settings(playwright_enabled=False))
    try:
        content = (
            "struttura;email;telefono\n"
            "Studio Uno;info@studio-uno.it;+39 06 1234567\n"
            "Clinica Due;segreteria [at] clinica-due [dot] it;+39 02 7654321\n"
        ).encode()
        html = crawler._csv_to_html(content)
        assert html is not None
        extracted = extract_page(html, "https://data.example/dentisti.csv", country="Italia")
        assert "info@studio-uno.it" in extracted.emails
        assert "segreteria@clinica-due.it" in extracted.emails
    finally:
        import asyncio

        asyncio.run(crawler.close())


def test_xlsx_document_becomes_extractable_contact_text():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Struttura", "Email", "Telefono"])
    sheet.append(["Studio Alfa", "reception@studio-alfa.it", "+39 011 1234567"])
    sheet.append(["Centro Beta", "info@centro-beta.it", "+39 081 7654321"])
    stream = io.BytesIO()
    workbook.save(stream)

    crawler = ContactCrawler(Settings(playwright_enabled=False))
    try:
        html = crawler._xlsx_to_html(stream.getvalue())
        assert html is not None
        extracted = extract_page(html, "https://data.example/dentisti.xlsx", country="Italia")
        assert "reception@studio-alfa.it" in extracted.emails
        assert "info@centro-beta.it" in extracted.emails
    finally:
        import asyncio

        asyncio.run(crawler.close())
