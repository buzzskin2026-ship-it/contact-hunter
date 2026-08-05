from app.config import Settings
from app.services.search import build_queries
from app.services.search_osm import (
    OpenStreetMapDentalProvider,
    country_code,
    supports_dental_sector,
)


def test_country_codes_accept_italian_and_english_names():
    assert country_code("Italia") == "IT"
    assert country_code("Francia") == "FR"
    assert country_code("Paesi Bassi") == "NL"
    assert country_code("Switzerland") == "CH"
    assert country_code("DE") == "DE"


def test_dental_sector_detection():
    assert supports_dental_sector("studi dentistici, laboratorio odontoiatrico")
    assert supports_dental_sector("dental clinics")
    assert not supports_dental_sector("hotel e ristoranti")


def test_queries_are_distributed_across_countries_before_second_variation():
    countries = ["Italia", "Francia", "Germania", "Spagna"]
    queries = build_queries("studi dentistici", countries, [], [])
    assert len(queries) == 12
    assert "Italia" in queries[0]
    assert "Francia" in queries[1]
    assert "Germania" in queries[2]
    assert "Spagna" in queries[3]
    assert "official website" in queries[0]
    assert "contatti email" in queries[4]


def test_overpass_query_targets_dentists_labs_and_contact_fields():
    provider = OpenStreetMapDentalProvider(Settings(playwright_enabled=False))
    query = provider._query("IT", 80)
    assert '"amenity"="dentist"' in query
    assert '"healthcare"="dentist"' in query
    assert '"craft"="dental_technician"' in query
    assert "contact:website" in query
    assert "contact:email" in query
    assert "contact:phone" in query
    assert "out tags center 80;" in query
