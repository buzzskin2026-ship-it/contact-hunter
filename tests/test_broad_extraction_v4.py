from app.models import SearchJob
from app.api.routes import _clone_search
from app.services.extractor import extract_page


def test_extracts_obfuscated_cloudflare_and_structured_contacts():
    # Cloudflare encoding for info@example.com, generated with a fixed XOR key.
    email = "info@example.com"
    key = 0x42
    cfemail = f"{key:02x}" + "".join(f"{ord(char) ^ key:02x}" for char in email)
    html = f"""
    <html><head>
      <script type="application/ld+json">
        {{"@type":"Dentist","name":"Studio Test","telephone":"+39 06 1234567"}}
      </script>
    </head><body>
      <span data-cfemail="{cfemail}"></span>
      <div>segreteria [at] clinic-test [dot] it</div>
      <span data-user="reception" data-domain="clinic-test.it"></span>
    </body></html>
    """
    result = extract_page(html, "https://clinic-test.it", country="Italia")
    assert "info@example.com" in result.emails
    assert "segreteria@clinic-test.it" in result.emails
    assert "reception@clinic-test.it" in result.emails
    assert result.organization == "Studio Test"
    assert result.phones


def test_broad_retry_expands_fields_and_limits_without_private_sources():
    original = SearchJob(
        sector="studi dentistici",
        countries=["Italia"],
        requested_fields=["email"],
        max_results=100,
        official_sources_only=True,
        exclude_free_email_providers=True,
    )
    broad = _clone_search(original, broad=True)
    assert broad.max_results == 500
    assert broad.official_sources_only is False
    assert broad.exclude_free_email_providers is False
    assert {"email", "phone", "whatsapp", "address"}.issubset(broad.requested_fields)
