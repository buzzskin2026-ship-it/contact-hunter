from app.services.normalizer import canonical_url, contact_fingerprint, normalize_email


def test_normalize_email():
    assert normalize_email(" Info@Example.COM ") == "info@example.com"
    assert normalize_email("logo@example.com.png") is None
    assert normalize_email("not-an-email") is None


def test_canonical_url_removes_fragment():
    assert canonical_url("HTTPS://Example.COM/contact#team") == "https://example.com/contact"


def test_fingerprint_is_stable():
    first = contact_fingerprint("example.com", ["info@example.com"], [])
    second = contact_fingerprint("example.com", ["info@example.com"], [])
    assert first == second
