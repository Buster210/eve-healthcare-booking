.PHONY: up down migrate seed run test lint fmt

up:
	docker compose up --build -d

down:
	docker compose down

migrate:
	uv run alembic upgrade head

seed:
	uv run python -m scripts.seed

run:
	uv run uvicorn app.main:app --reload

test:
	uv run pytest -q

lint:
	uv run ruff check .

fmt:
	uv run ruff format .
