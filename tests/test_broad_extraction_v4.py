from app.api.routes import _clone_search
from app.models import SearchJob
from app.services.extractor import extract_page
from app.services.jobs import _email_groups


def test_extracts_obfuscated_cloudflare_and_structured_contacts():
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
    assert broad.max_results == 50_000
    assert broad.official_sources_only is False
    assert broad.exclude_free_email_providers is False
    assert {"email", "phone", "whatsapp", "address"}.issubset(broad.requested_fields)


def test_broad_mode_creates_one_export_record_per_unique_email():
    emails = ["INFO@EXAMPLE.COM", "reception@example.com", "info@example.com"]
    assert _email_groups(emails, broad=True) == [
        ["info@example.com"],
        ["reception@example.com"],
    ]
    assert _email_groups(emails, broad=False) == [[
        "info@example.com",
        "reception@example.com",
    ]]
