from app.services.search import _public_result_target


def test_public_result_target_decodes_redirect() -> None:
    value = (
        "https://duckduckgo.com/l/?uddg="
        "https%3A%2F%2Fexample.com%2Fcontacts%3Fref%3Dsearch"
    )
    assert _public_result_target(value) == "https://example.com/contacts?ref=search"


def test_public_result_target_keeps_direct_url() -> None:
    assert _public_result_target("https://example.org/contact") == "https://example.org/contact"


def test_public_result_target_rejects_empty_redirect() -> None:
    assert _public_result_target("https://duckduckgo.com/l/") is None
