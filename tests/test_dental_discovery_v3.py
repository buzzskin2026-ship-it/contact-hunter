from app.config import Settings
from app.services.jobs import _distinctive_tokens, _round_robin_records, _usable_business_hit
from app.services.search import SearchHit, build_queries
from app.services.search_osm import (
    OpenStreetMapDentalProvider,
    OsmContactRecord,
    country_code,
    supports_dental_sector,
)


def _record(name: str, country: str, city: str | None = None) -> OsmContactRecord:
    return OsmContactRecord(
        source_url="https://www.openstreetmap.org/node/1",
        organization=name,
        category="Studio dentistico / clinica dentale",
        country=country,
        city=city,
        address=None,
        website=None,
        emails=[],
        phones=[],
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


def test_overpass_query_targets_all_named_dentists_and_labs():
    provider = OpenStreetMapDentalProvider(Settings(playwright_enabled=False))
    query = provider._query("IT", 80)
    assert '"amenity"="dentist"' in query
    assert '"healthcare"="dentist"' in query
    assert '"craft"="dental_technician"' in query
    assert '["name"]' in query
    assert "contact:website" not in query
    assert "out tags center 80;" in query


def test_round_robin_name_resolution_does_not_starve_later_countries():
    records = [
        _record("Studio Uno", "Italia"),
        _record("Studio Due", "Italia"),
        _record("Cabinet Trois", "Francia"),
        _record("Praxis Vier", "Germania"),
    ]
    selected = _round_robin_records(records, 3)
    assert [record.country for record in selected] == ["Italia", "Francia", "Germania"]


def test_official_site_candidate_requires_distinctive_business_token():
    record = _record("Clinica Dentale Rossi", "Italia", "Roma")
    assert "rossi" in _distinctive_tokens(record.organization)
    assert _usable_business_hit(
        SearchHit(url="https://www.clinicarossi.it", title="Clinica Rossi Roma"),
        record,
    )
    assert not _usable_business_hit(
        SearchHit(url="https://www.example-directory.test/dentisti", title="Elenco dentisti"),
        record,
    )
