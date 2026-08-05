from __future__ import annotations

import argparse
from pathlib import Path

from app.db import Base, SessionLocal, engine
from app.services.importer import import_xlsx


def main() -> None:
    parser = argparse.ArgumentParser(description="Importa contatti da un file XLSX nel database di Contact Hunter.")
    parser.add_argument("xlsx", type=Path)
    args = parser.parse_args()
    if not args.xlsx.exists():
        raise SystemExit(f"File non trovato: {args.xlsx}")
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        imported, duplicates = import_xlsx(db, args.xlsx)
    print(f"Importati: {imported}; duplicati ignorati: {duplicates}")


if __name__ == "__main__":
    main()
