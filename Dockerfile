FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1

RUN pip install poetry==2.5.1

WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root


FROM python:3.12-slim

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --system --no-create-home app

WORKDIR /app
COPY --from=builder /app/.venv .venv
COPY hc_provider hc_provider

USER app
EXPOSE 8000

CMD ["uvicorn", "hc_provider.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
