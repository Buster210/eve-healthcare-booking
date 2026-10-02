FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY alembic ./alembic
COPY scripts ./scripts
COPY alembic.ini ./
RUN useradd --create-home --uid 10001 eve
USER eve
EXPOSE 8000
CMD ["sh", "-c", "uv run --no-sync alembic upgrade head && exec uv run --no-sync uvicorn app.main:app --host 0.0.0.0 --port 8000"]
