from app.services.extractor import extract_page, reliability_for

HTML = """
<html><head><title>Studio Dentistico Aurora | Contatti</title>
<script type="application/ld+json">{
  "@type":"Dentist", "name":"Studio Dentistico Aurora",
  "email":"segreteria@auroradental.it", "telephone":"+39 06 1234567",
  "address":{"streetAddress":"Via Roma 1", "postalCode":"00100", "addressLocality":"Roma", "addressCountry":"IT"}
}</script></head>
<body><h1>Studio Dentistico Aurora</h1>
<a href="mailto:info@auroradental.it">Email</a>
<a href="tel:+39061234567">Telefono</a>
<a href="/contatti">Contatti</a><p>Implantologia e ortodonzia.</p></body></html>
"""


def test_extract_public_contacts():
    result = extract_page(HTML, "https://auroradental.it/", country="Italy", keywords=["implantologia", "ortodonzia"])
    assert result.organization == "Studio Dentistico Aurora"
    assert "info@auroradental.it" in result.emails
    assert "segreteria@auroradental.it" in result.emails
    assert result.city == "Roma"
    assert set(result.specialties) == {"implantologia", "ortodonzia"}
    assert "/contatti" in result.relevant_links


def test_reliability_uses_business_domain():
    assert reliability_for(["info@example.com"], "https://example.com/") == "high"
    assert reliability_for(["clinic@gmail.com"], "https://example.com/") == "medium"
