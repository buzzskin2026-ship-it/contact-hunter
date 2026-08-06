from __future__ import annotations

import unicodedata

ITALY_REGIONS = (
    "Abruzzo",
    "Basilicata",
    "Calabria",
    "Campania",
    "Emilia-Romagna",
    "Friuli-Venezia Giulia",
    "Lazio",
    "Liguria",
    "Lombardia",
    "Marche",
    "Molise",
    "Piemonte",
    "Puglia",
    "Sardegna",
    "Sicilia",
    "Toscana",
    "Trentino-Alto Adige",
    "Umbria",
    "Valle d'Aosta",
    "Veneto",
)

# Province, città metropolitane e poli territoriali usati come partizioni di
# ricerca. I nomi della Sardegna includono il nuovo assetto valido dal 2026 e
# i principali capoluoghi, così le ricerche restano efficaci anche quando un
# motore indicizza ancora denominazioni precedenti.
ITALY_PROVINCES_AND_HUBS = (
    "Agrigento",
    "Alessandria",
    "Ancona",
    "Aosta",
    "Arezzo",
    "Ascoli Piceno",
    "Asti",
    "Avellino",
    "Bari",
    "Barletta",
    "Andria",
    "Trani",
    "Belluno",
    "Benevento",
    "Bergamo",
    "Biella",
    "Bologna",
    "Bolzano",
    "Brescia",
    "Brindisi",
    "Cagliari",
    "Caltanissetta",
    "Campobasso",
    "Carbonia",
    "Caserta",
    "Catania",
    "Catanzaro",
    "Chieti",
    "Como",
    "Cosenza",
    "Cremona",
    "Crotone",
    "Cuneo",
    "Enna",
    "Fermo",
    "Ferrara",
    "Firenze",
    "Foggia",
    "Forlì",
    "Cesena",
    "Frosinone",
    "Genova",
    "Gorizia",
    "Grosseto",
    "Iglesias",
    "Imperia",
    "Isernia",
    "L'Aquila",
    "La Spezia",
    "Lanusei",
    "Latina",
    "Lecce",
    "Lecco",
    "Livorno",
    "Lodi",
    "Lucca",
    "Macerata",
    "Mantova",
    "Massa",
    "Carrara",
    "Matera",
    "Messina",
    "Milano",
    "Modena",
    "Monza",
    "Napoli",
    "Novara",
    "Nuoro",
    "Ogliastra",
    "Olbia",
    "Oristano",
    "Padova",
    "Palermo",
    "Parma",
    "Pavia",
    "Perugia",
    "Pesaro",
    "Urbino",
    "Pescara",
    "Piacenza",
    "Pisa",
    "Pistoia",
    "Pordenone",
    "Potenza",
    "Prato",
    "Ragusa",
    "Ravenna",
    "Reggio Calabria",
    "Reggio Emilia",
    "Rieti",
    "Rimini",
    "Roma",
    "Rovigo",
    "Salerno",
    "Sanluri",
    "Sassari",
    "Savona",
    "Siena",
    "Siracusa",
    "Sondrio",
    "Sulcis Iglesiente",
    "Taranto",
    "Teramo",
    "Terni",
    "Torino",
    "Tortolì",
    "Trapani",
    "Trento",
    "Treviso",
    "Trieste",
    "Udine",
    "Varese",
    "Venezia",
    "Verbano-Cusio-Ossola",
    "Vercelli",
    "Verona",
    "Vibo Valentia",
    "Vicenza",
    "Villacidro",
    "Viterbo",
    "Gallura Nord-Est Sardegna",
    "Medio Campidano",
    "Città metropolitana di Cagliari",
    "Città metropolitana di Sassari",
)

DENTAL_PLACE_TERMS = (
    "studio dentistico",
    "dentista",
    "clinica dentale",
    "centro odontoiatrico",
    "laboratorio odontotecnico",
)


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def is_italy(value: str) -> bool:
    return _fold(value.strip()) in {"italia", "italy", "it"}


def discovery_locations(countries: list[str], cities: list[str]) -> list[tuple[str, str]]:
    """Return (country, location) pairs ordered for broad territorial coverage."""
    if cities:
        country = countries[0] if len(countries) == 1 else ""
        return [(country, city) for city in dict.fromkeys(cities)]

    pairs: list[tuple[str, str]] = []
    for country in countries or [""]:
        if is_italy(country):
            pairs.extend((country, location) for location in ITALY_PROVINCES_AND_HUBS)
            pairs.extend((country, region) for region in ITALY_REGIONS)
        else:
            pairs.append((country, country))
    return list(dict.fromkeys(pairs))


def place_terms(sector: str) -> tuple[str, ...]:
    folded = _fold(sector)
    if any(token in folded for token in ("dent", "odont", "stomatolog", "zahnarzt")):
        return DENTAL_PLACE_TERMS
    return (sector.strip(),)


def build_place_queries(
    sector: str,
    countries: list[str],
    cities: list[str],
    max_queries: int,
) -> list[tuple[str, str, str]]:
    """Build (query, country, location) tuples in location-first round-robin order."""
    locations = discovery_locations(countries, cities)
    terms = place_terms(sector)
    queries: list[tuple[str, str, str]] = []
    for term in terms:
        for country, location in locations:
            parts = [term]
            if location:
                parts.append(location)
            if country and _fold(country) not in _fold(location):
                parts.append(country)
            queries.append((" ".join(parts), country, location))
            if len(queries) >= max_queries:
                return queries
    return queries
