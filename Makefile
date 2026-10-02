.PHONY: up down migrate seed run worker beat test lint fmt

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

worker:
	uv run celery -A app.worker worker --loglevel=INFO

beat:
	uv run celery -A app.worker beat --loglevel=INFO --schedule=/tmp/eve-celerybeat-schedule

test:
	uv run pytest -q

lint:
	uv run ruff check .

fmt:
	uv run ruff format .
