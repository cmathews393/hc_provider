# hc-provider

A custom metadata provider for [Audiobookshelf](https://www.audiobookshelf.org/) that pulls book metadata from [Hardcover](https://hardcover.app/).

## Setup

```sh
poetry install
export HARDCOVER_TOKEN="Bearer <your token>"   # from https://hardcover.app/account/api
```

### Sentry (optional)

Error reporting and tracing are enabled when `SENTRY_DSN` is set. `SENTRY_ENVIRONMENT`, `SENTRY_RELEASE` and `SENTRY_TRACES_SAMPLE_RATE` (default `1.0`) are also read.

Run locally with `poetry run uvicorn hc_provider.main:app`.

### Docker

Images are published to `ghcr.io/cmathews393/hc_provider` for `linux/amd64` and `linux/arm64`:

```sh
docker run -p 8000:8000 -e HARDCOVER_TOKEN="Bearer <your token>" ghcr.io/cmathews393/hc_provider:latest
```

## API

`GET /search?query=<title>&author=<name>` returns `{"matches": [...]}` in Audiobookshelf's custom provider format. If filtering by author finds nothing, the search is retried on title alone.

## Development

```sh
poetry run ruff format . && poetry run ruff check . && poetry run ty check
```
