.PHONY: up down logs dry-run test lint config

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

dry-run:
	docker compose run --rm --no-deps -e DRY_RUN=true cleaner python /app/cleaner.py --once

test:
	python3 -m pytest -q cleaner/tests

lint:
	python3 -m ruff check cleaner

dev-install:
	python3 -m pip install -r requirements-dev.txt

config:
	docker compose --env-file .env.example config --quiet
