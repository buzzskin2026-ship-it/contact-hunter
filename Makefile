.PHONY: install run test lint docker-up docker-down import-existing

install:
	python -m pip install -e '.[dev]'
	python -m playwright install chromium

run:
	uvicorn app.main:app --reload

test:
	pytest

lint:
	ruff check .

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

import-existing:
	python scripts/import_contacts.py data/imports/contatti.xlsx
