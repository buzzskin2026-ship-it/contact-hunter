from app.config import Settings
from app.services.geo_partitions import discovery_locations
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


def test_italian_national_search_is_partitioned_across_the_country():
    locations = discovery_locations(["Italia"], [])
    location_names = {location for _, location in locations}
    assert len(locations) > 100
    assert {"Roma", "Milano", "Napoli", "Palermo", "Cagliari"}.issubset(location_names)
    assert "Gallura Nord-Est Sardegna" in location_names
    assert "Lombardia" in location_names


def test_queries_cover_each_italian_partition_and_document_sources():
    queries = build_queries("studi dentistici", ["Italia"], [], [])
    locations = discovery_locations(["Italia"], [])
    assert len(queries) == len(locations) * 9
    assert "Agrigento" in queries[0]
    assert "official website" in queries[0]
    assert any("Roma" in query and "filetype:pdf" in query for query in queries)
    assert any("Milano" in query and "associazione dentisti" in query for query in queries)
    assert any("albo odontoiatri" in query for query in queries)


def test_overpass_query_targets_all_named_dentists_labs_and_specialist_clinics():
    provider = OpenStreetMapDentalProvider(Settings(playwright_enabled=False))
    query = provider._query("IT", 80)
    assert '"amenity"="dentist"' in query
    assert '"healthcare"="dentist"' in query
    assert '"craft"="dental_technician"' in query
    assert '"healthcare:speciality"' in query
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
